"""Orchestration, scoring, and the outreach opener.

Scoring model (weights from the v1 spec):
    Speed 25 · Search visibility 25 · Turning visitors into calls 25 ·
    Local presence 15 · Trust & basics 10
Each category starts at 100 and loses points per finding. Overall = weighted
average.

The outreach opener never leads with jargon: findings are ranked separately
for the email via PLAIN_SPOKEN below. "The phone number isn't tap-to-call"
opens a conversation; "no LocalBusiness structured data" does not.
"""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from . import USER_AGENT, __version__
from .checks import (check_conversion, check_local, check_search, check_speed,
                     check_trust, declared_subresources, detect_stack, finding,
                     parse, render_blocking_count)
from .render import looks_like_shell, render_html
from .fetch import (FetchResult, _client, check_sitemap, fetch_page,
                    fetch_secondary_page, normalize_url, probe_subresources)

WEIGHTS = {"speed": 0.25, "search": 0.25, "conversion": 0.25,
           "local": 0.15, "trust": 0.10}

LABELS = {"speed": "Speed", "search": "Search visibility",
          "conversion": "Turning visitors into calls",
          "local": "Local presence", "trust": "Trust & basics"}

# Ranked for the cold email — most human-relatable problems first.
# A key earlier in this list wins the opener even if a later key lost more
# points. Jargon-y findings (schema, canonical, OG) never open.
PLAIN_SPOKEN = [
    ("no_https", "your site shows customers a 'Not Secure' warning right in the address bar"),
    ("no_phone", "there's no phone number on your homepage — on a phone, there's no way to call you"),
    ("no_tap_to_call", "your phone number isn't tap-to-call — on a phone, tapping it does nothing"),
    ("slow_load", "your homepage takes {load:.0f}+ seconds to load, and about half of visitors quit at 3"),
    ("no_viewport", "your site shows phone visitors a shrunken desktop page they have to pinch-zoom"),
    ("no_form", "there's no way for after-hours visitors to request a quote — the 9 PM researcher can't reach you"),
    ("form_only_contact", "requesting a quote takes an extra click that most visitors never make"),
    ("no_reviews_shown", "your Google reviews don't show anywhere on your homepage — you've earned stars nobody sees"),
    ("cta_buried", "the first screen of your site never asks the visitor to call or get an estimate"),
    ("slow_ttfb", "your server takes over a second to even start responding — visitors feel it as lag"),
    ("no_retargeting", "visitors who leave your site are gone for good — there's no retargeting pixel to bring them back"),
    ("no_trust", "nothing on the page says licensed or insured — for a homeowner that's a tiebreaker"),
    ("no_analytics", "the site has no analytics at all, so nobody can tell you what your marketing did last month"),
    ("no_meta_desc", "Google is writing your ad copy for you — the description under your name in search is random page text"),
]
PLAIN_MAP = dict(PLAIN_SPOKEN)
PLAIN_ORDER = {k: i for i, (k, _) in enumerate(PLAIN_SPOKEN)}


def grade_for(score: int) -> str:
    return ("A" if score >= 90 else "B" if score >= 80 else
            "C" if score >= 70 else "D" if score >= 60 else "F")


# ── Scoring calibration ──────────────────────────────────────────────────────
# v2.1 raw scores clustered in the high 80s–90s: most sites pass the binary
# checks, and the deductions that DO fire are diluted by category weighting and
# by trust/speed sitting near 100 on almost every site. A median local business
# raw-scores ~85, which makes "your site scored 92" open no conversations.
#
# This power curve stretches the scale: it leaves a perfect 100 at 100 and a 0
# at 0, but pushes the crowded middle down — a median site (~85 raw) lands ~70
# (an honest "C"), genuinely good sites stay ~90+, and weak ones fall to D/F.
# It is monotonic, so it never re-ranks two sites. Tune with one number:
# raise GAMMA to grade harder, set it to 1.0 to disable calibration entirely.
CALIBRATION_GAMMA = 2.3


def calibrate(raw: int) -> int:
    """Map a raw 0–100 category score onto the calibrated scale."""
    x = max(0, min(100, raw)) / 100
    return round(100 * (x ** CALIBRATION_GAMMA))


SEV_RANK = {"critical": 0, "serious": 1, "warning": 2, "notice": 3}


def _stack_findings(stack: dict) -> list[dict]:
    """Marketing-stack gaps. These do NOT move the five category scores —
    they're pitch material — but they compete for the fix list and opener."""
    out = []
    if not stack["has_analytics"]:
        out.append(finding(
            "no_analytics", "No analytics installed",
            "The site runs no measurement at all — no Google Analytics, no "
            "alternative.",
            "Nobody at this company can say what their website or ads produced "
            "last month. Every marketing dollar is being spent blind.",
            "Install Google Analytics 4 (free) or a lightweight alternative. "
            "Ten minutes with a tag manager.",
            "serious", 10))
    if not stack["has_retargeting"]:
        out.append(finding(
            "no_retargeting", "No retargeting pixel installed",
            "No Meta pixel, Google Ads tag, or any other retargeting tag is on "
            "the site.",
            "97% of first-time visitors leave without calling. With no pixel, "
            "they're gone forever; with one, following them costs pennies and "
            "is usually the cheapest lead a local business can buy.",
            "Install the Meta pixel and Google Ads tag now, even before running "
            "ads — the audience only builds from the day the pixel goes in.",
            "warning", 9))
    return out


def _verdict(host: str, overall: int, top: dict | None) -> str:
    g = grade_for(overall)
    lead = {
        "A": f"{host} is in strong shape — the fundamentals are done right.",
        "B": f"{host} is solid but leaving customers on the table.",
        "C": f"{host} looks fine to a person and mediocre to Google — fixable, "
             "and worth fixing.",
        "D": f"{host} is actively losing business to better-tuned competitors.",
        "F": f"{host} has problems serious enough to cost customers today.",
    }[g]
    if top:
        lead += f" Biggest single win: {top['title'].lower()}."
    return lead


def _opening_line(host: str, findings_all: list[dict],
                  metrics: dict) -> str:
    candidates = [f for f in findings_all if f["key"] in PLAIN_MAP]
    if not candidates:
        return (f"I ran {host} through our site check and it came back "
                "cleaner than almost anything we scan — nice work. If you ever "
                "want the full report anyway, it's yours.")
    best = min(candidates, key=lambda f: PLAIN_ORDER[f["key"]])
    plain = PLAIN_MAP[best["key"]].format(load=metrics.get("load_seconds", 0))
    return (f"I ran {host} through the site check we use before we take on "
            f"any marketing work, and one thing jumped out: {plain}. "
            "It's a quick fix — I put the full report together for you, no "
            "strings. Want me to send it over?")


def analyze(url: str, deep: bool = True,
            client: httpx.Client | None = None) -> dict:
    """Analyze one site. deep=True adds subresource probing, sitemap check,
    contact-page crawl, and the http->https test; batch fast mode skips them.
    """
    own = client is None
    client = client or _client()
    try:
        res = fetch_page(url, client=client)
        if not res.ok:
            return {
                "ok": False, "url": url, "host": res.host or url,
                "error": res.error or "The site could not be loaded.",
                "verdict": "A site that doesn't load isn't a lost cause — "
                           "it's the easiest pitch on the list.",
            }

        raw_html = res.html
        rendered = False
        if looks_like_shell(raw_html):
            dom = render_html(res.final_url or url)
            if dom and not looks_like_shell(dom):
                res.html = dom          # grade what a visitor actually sees
                rendered = True
            else:
                return {
                    "ok": False, "url": url, "host": res.host or url,
                    "error": "This site builds its pages with JavaScript and "
                             "we couldn't render it, so we can't grade it "
                             "honestly. Check it by hand.",
                    "verdict": "Skipped — reading the empty shell would "
                               "produce false findings.",
                }

        soup = parse(res.html)
        base = res.final_url

        sitemap_ok = None
        contact_page = None
        if deep:
            sitemap_ok = check_sitemap(client, base, res.robots_txt)
            probe_subresources(res, declared_subresources(soup, base),
                               client=client)
            contact_page = _find_contact_page(client, soup, base)

        cat_scores, findings_all = {}, []
        for key, fn in (("speed", lambda: check_speed(res, soup)),
                        ("search", lambda: check_search(res, soup, sitemap_ok)),
                        ("conversion", lambda: check_conversion(res, soup,
                                                                contact_page)),
                        ("local", lambda: check_local(res, soup)),
                        ("trust", lambda: check_trust(res, soup))):
            score, fs = fn()
            cat_scores[key] = score
            for f in fs:
                f["category"] = key
            findings_all.extend(fs)

        stack = detect_stack(res.html)
        for f in _stack_findings(stack):
            f["category"] = "marketing"
            findings_all.append(f)

        cal_scores = {k: calibrate(v) for k, v in cat_scores.items()}
        overall = round(sum(cal_scores[k] * w for k, w in WEIGHTS.items()))

        ranked = sorted(findings_all,
                        key=lambda f: (SEV_RANK[f["severity"]], -f["points"]))
        top = ranked[0] if ranked else None

        declared = declared_subresources(soup, base)
        est_weight_kb = round((res.html_bytes + res.probed_bytes) / 1024)
        if res.probed_count and len(declared) > res.probed_count:
            est_weight_kb = round((res.html_bytes + res.probed_bytes *
                                   (len(declared) / res.probed_count)) / 1024)

        metrics = {
            "load_seconds": res.load_seconds,
            "ttfb_ms": res.ttfb_ms,
            "page_weight_kb": est_weight_kb,
            "requests_declared": len(declared),
            "render_blocking": render_blocking_count(soup),
            "tel_links": len(soup.select('a[href^="tel:"]')),
        }

        return {
            "ok": True,
            "analyzer_version": __version__,
            "url": url,
            "final_url": res.final_url,
            "host": res.host,
            "overall": overall,
            "grade": grade_for(overall),
            "verdict": _verdict(res.host, overall, top),
            "scores": {k: {"label": LABELS[k], "score": cal_scores[k]}
                       for k in WEIGHTS},
            "priorities": ranked[:3],
            "findings": ranked,
            "metrics": metrics,
            "stack": stack,
            "opening_line": _opening_line(res.host, findings_all, metrics),
            "rendered_js": rendered,
            "pages_checked": 1 + (1 if contact_page and contact_page.ok else 0),
            "measurement_note": (
                "Speed measured server-side from a fast connection; real "
                "phones on cell service are usually slower, so treat these "
                "numbers as the optimistic case."),
        }
    finally:
        if own:
            client.close()


def _find_contact_page(client: httpx.Client, soup, base: str):
    """Find and fetch the contact page, if the homepage links one."""
    import re
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True).lower()
        if re.search(r"contact|get.?in.?touch|request", href, re.I) or \
           "contact" in label:
            target = urljoin(base, href)
            if target.startswith(("http://", "https://")) and \
               _same_host(target, base):
                page = fetch_secondary_page(client, target)
                return page if page.ok else None
    return None


def _same_host(a: str, b: str) -> bool:
    from urllib.parse import urlparse
    return urlparse(a).netloc.replace("www.", "") == \
        urlparse(b).netloc.replace("www.", "")


def summarize_for_batch(d: dict) -> dict:
    """The compact row the market-scan table needs."""
    if not d.get("ok"):
        return {"ok": False, "host": d.get("host") or d.get("url"),
                "error": d.get("error", "could not load")}
    top = d["priorities"][0] if d["priorities"] else None
    return {
        "ok": True,
        "host": d["host"],
        "score": d["overall"],
        "grade": d["grade"],
        "cms": d["stack"]["cms"],
        "top_issue": top["title"] if top else None,
        "has_analytics": d["stack"]["has_analytics"],
        "has_retargeting": d["stack"]["has_retargeting"],
        "opening_line": d["opening_line"],
    }


def analyze_batch(urls: list[str], deep: bool = False,
                  max_workers: int = 6) -> list[dict]:
    """Score a prospect list. Returns compact rows, ranked worst-first —
    on a prospect list the low scores are the ones to call."""
    from concurrent.futures import ThreadPoolExecutor

    def one(u):
        try:
            return summarize_for_batch(analyze(u, deep=deep))
        except Exception as e:
            return {"ok": False, "host": u, "error": str(e)[:120]}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        rows = list(ex.map(one, urls))
    # Worst first. Failures sort to the very top — a site that doesn't load
    # is the easiest pitch on the list.
    rows.sort(key=lambda r: r.get("score", -1))
    return rows
