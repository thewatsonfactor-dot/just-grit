"""Network layer — timing, redirect chain, robots.txt, sitemap, subresources.

Everything here is a plain GET of public pages, the way a browser would load
them, with an honest User-Agent. robots.txt is honored: if we're disallowed,
we do not fetch, full stop.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from . import USER_AGENT

TIMEOUT = httpx.Timeout(15.0, connect=8.0)
MAX_SUBRESOURCE_PROBES = 22  # sample; weight is extrapolated and labeled as such
MAX_HTML_BYTES = 3_000_000


@dataclass
class FetchResult:
    url: str
    ok: bool = False
    error: str = ""
    final_url: str = ""
    host: str = ""
    status: int = 0
    html: str = ""
    html_bytes: int = 0
    ttfb_ms: int = 0
    load_seconds: float = 0.0
    redirect_chain: list = field(default_factory=list)
    https: bool = False
    http_redirects_to_https: bool | None = None
    content_encoding: str = ""
    robots_txt: str | None = None       # None = no robots.txt found
    robots_allowed: bool = True
    sitemap_ok: bool | None = None      # None = not declared/checked
    headers: dict = field(default_factory=dict)
    # filled by probe_subresources()
    subresources: list = field(default_factory=list)   # (url, kind, bytes|None, status|None)
    probed_bytes: int = 0
    probed_count: int = 0
    mixed_content: list = field(default_factory=list)  # real subresource loads only


def normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw:
        raise ValueError("empty url")
    if "://" not in raw:
        raw = "https://" + raw
    return raw


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            # NOTE: never set Accept-Encoding manually. httpx advertises only
            # the codings it can actually decode (br requires the brotli
            # package — see requirements.txt). Advertising "br" by hand made
            # real servers send brotli that arrived as undecodable bytes, and
            # the analyzer scored compressed garbage. Found on the first
            # real-world run; tests/test_analyzer.py now covers compression.
        },
    )


def check_robots(client: httpx.Client, base: str) -> tuple[str | None, bool]:
    """Return (robots_txt_text_or_None, allowed_for_us)."""
    robots_url = urljoin(base, "/robots.txt")
    try:
        r = client.get(robots_url)
        if r.status_code != 200 or not r.text.strip():
            return None, True
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return r.text, rp.can_fetch(USER_AGENT, base)
    except Exception:
        return None, True


def fetch_page(url: str, client: httpx.Client | None = None) -> FetchResult:
    """Fetch one page with timing. Measures TTFB (headers received) and total load."""
    res = FetchResult(url=url)
    own_client = client is None
    client = client or _client()
    try:
        target = normalize_url(url)
        res.host = urlparse(target).hostname or target

        base = f"{urlparse(target).scheme}://{urlparse(target).netloc}/"
        res.robots_txt, res.robots_allowed = check_robots(client, base)
        if not res.robots_allowed:
            res.error = "robots.txt disallows automated access to this site — we honor that."
            return res

        t0 = time.perf_counter()
        with client.stream("GET", target) as r:
            res.ttfb_ms = int((time.perf_counter() - t0) * 1000)
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_HTML_BYTES:
                    break
            res.load_seconds = round(time.perf_counter() - t0, 2)
            res.status = r.status_code
            res.final_url = str(r.url)
            res.host = urlparse(res.final_url).hostname or res.host
            res.redirect_chain = [str(h.url) for h in r.history] + [str(r.url)]
            res.https = urlparse(res.final_url).scheme == "https"
            res.content_encoding = r.headers.get("content-encoding", "")
            res.headers = {k.lower(): v for k, v in r.headers.items()}
            res.html_bytes = len(body)
            res.html = body.decode(r.charset_encoding or "utf-8", errors="replace")

        if res.status >= 400:
            res.error = f"The site answered with HTTP {res.status}."
            return res
        if not res.html.strip():
            res.error = "The site returned an empty page."
            return res
        res.ok = True

        # http -> https redirect behavior (only test if we landed on https)
        if res.https:
            try:
                http_url = "http://" + urlparse(res.final_url).netloc + "/"
                rr = client.get(http_url)
                res.http_redirects_to_https = str(rr.url).startswith("https://")
            except Exception:
                res.http_redirects_to_https = None
        return res
    except httpx.ConnectTimeout:
        res.error = "The site took too long to respond and the connection timed out."
    except httpx.ConnectError as e:
        res.error = f"Could not connect: {str(e)[:120]}"
    except Exception as e:
        res.error = f"{type(e).__name__}: {str(e)[:140]}"
    finally:
        if own_client:
            client.close()
    return res


def check_sitemap(client: httpx.Client, base: str, robots_txt: str | None) -> bool:
    """True if a sitemap is reachable (declared in robots.txt or at /sitemap.xml)."""
    candidates = []
    if robots_txt:
        for line in robots_txt.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())
    candidates.append(urljoin(base, "/sitemap.xml"))
    for c in candidates[:3]:
        try:
            r = client.head(c, follow_redirects=True)
            if r.status_code == 405:
                r = client.get(c, follow_redirects=True)
            if r.status_code == 200:
                return True
        except Exception:
            continue
    return False


def probe_subresources(res: FetchResult, declared: list[tuple[str, str]],
                       client: httpx.Client | None = None) -> None:
    """HEAD a sample of declared subresources to estimate real page weight and
    find genuinely-loaded mixed content. `declared` = [(abs_url, kind)].

    Only counts real subresource loads (script/css/img), never ordinary
    outbound links — that distinction killed a whole false-positive class in v1.
    """
    own_client = client is None
    client = client or _client()
    try:
        sample = declared[:MAX_SUBRESOURCE_PROBES]
        for sub_url, kind in sample:
            size, status = None, None
            try:
                r = client.head(sub_url, follow_redirects=True)
                if r.status_code == 405:
                    r = client.get(sub_url, follow_redirects=True)
                status = r.status_code
                cl = r.headers.get("content-length")
                size = int(cl) if cl and cl.isdigit() else None
            except Exception:
                pass
            res.subresources.append((sub_url, kind, size, status))
            if size:
                res.probed_bytes += size
            res.probed_count += 1
            if res.https and sub_url.startswith("http://"):
                res.mixed_content.append(sub_url)
    finally:
        if own_client:
            client.close()


def fetch_secondary_page(client: httpx.Client, url: str) -> FetchResult:
    """Lighter fetch for contact/service pages — no robots re-check needed
    beyond the homepage's (same host), no timing emphasis."""
    res = FetchResult(url=url)
    try:
        t0 = time.perf_counter()
        r = client.get(url)
        res.load_seconds = round(time.perf_counter() - t0, 2)
        res.status = r.status_code
        res.final_url = str(r.url)
        res.host = urlparse(res.final_url).hostname or ""
        res.https = urlparse(res.final_url).scheme == "https"
        if r.status_code < 400 and r.text.strip():
            res.ok = True
            res.html = r.text[:MAX_HTML_BYTES]
            res.html_bytes = len(r.content)
    except Exception as e:
        res.error = f"{type(e).__name__}: {str(e)[:100]}"
    return res
