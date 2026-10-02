"""End-to-end tests against local fixture sites.

Run:  python -m pytest tests/ -q   (or just: python tests/test_analyzer.py)

Covers:
- scoring direction (good site > bad site)
- dashboard API contract fields
- the hostile-HTML regression: a page with <script>/onerror payloads in its
  title and meta description must render as inert text in the report
- suppression + rate limiting behavior at the API layer
- contact-page crawl adjusting the no-form finding
"""

import http.server
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GOOD = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roof Repair in San Antonio | Alamo Roofing Co</title>
<meta name="description" content="Licensed San Antonio roofers. Free estimates, 20 years experience, 4.9 stars on Google. Call today.">
<link rel="canonical" href="http://localhost:{port}/good">
<meta property="og:title" content="Alamo Roofing Co">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"RoofingContractor","name":"Alamo Roofing","telephone":"+12105551234","address":{{"@type":"PostalAddress","streetAddress":"100 Alamo Plaza","addressLocality":"San Antonio"}}}}</script>
</head><body>
<h1>Roof Repair in San Antonio</h1>
<p>Licensed and insured. Serving San Antonio and New Braunfels since 2004.</p>
<a href="tel:+12105551234">Call (210) 555-1234</a>
<a href="https://google.com/maps/place/x">Find us on Google</a>
<p>Free estimate — book now. 4.9 stars from 200+ reviews.</p>
<form action="/quote"><input name="name"><input name="phone"><button>Get a free quote</button></form>
<img src="/a.jpg" alt="roof replacement san antonio" width="10" height="10" loading="lazy">
</body></html>"""

BAD = """<html><head></head><body>
<p>We do roofs. Email us maybe. Call 210 555 9999 sometime.</p>
<img src="/x.jpg"><img src="/y.jpg">
</body></html>"""

HOSTILE = """<!DOCTYPE html><html><head>
<title>&lt;danger&gt;<script>alert('title-xss')</script>Roofing</title>
<meta name="description" content="<img src=x onerror=alert('meta-xss')>cheap roofs">
</head><body>
<h1><script>alert('h1-xss')</script>Totally Normal Roofing</h1>
<p>Call us</p>
</body></html>"""

CONTACT_HUB = """<!DOCTYPE html><html><head><title>Hub Roofing — San Antonio</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>
<h1>Hub Roofing</h1><a href="tel:+12105550000">Call (210) 555-0000</a>
<a href="/contact">Contact us</a><p>Licensed and insured, serving San Antonio. Reviews: 4.8 stars.</p>
<p>Free estimate — call now.</p></body></html>"""

CONTACT_PAGE = """<!DOCTYPE html><html><head><title>Contact</title></head><body>
<form action="/send"><input name="n"><button>Send</button></form></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    ROUTES = {}
    COMPRESSED = set()   # paths served gzip- or brotli-encoded

    def do_GET(self):
        path = self.path.split("?")[0]
        body = self.ROUTES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        encoding = None
        if path in self.COMPRESSED:
            accepted = self.headers.get("accept-encoding", "")
            if "br" in accepted:
                try:
                    import brotli
                    data, encoding = brotli.compress(data), "br"
                except ImportError:
                    pass
            if encoding is None and "gzip" in accepted:
                import gzip
                data, encoding = gzip.compress(data), "gzip"
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        if encoding:
            self.send_header("content-encoding", encoding)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, *a):
        pass


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    Handler.ROUTES = {
        "/good": GOOD.format(port=port), "/bad": BAD, "/hostile": HOSTILE,
        "/hub": CONTACT_HUB, "/contact": CONTACT_PAGE,
        "/gz": GOOD.format(port=port),
        "/a.jpg": "x", "/x.jpg": "x", "/y.jpg": "x",
    }
    Handler.COMPRESSED = {"/gz"}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


SRV, PORT = start_server()
BASE = f"http://127.0.0.1:{PORT}"

from grit_analyzer.analyzer import analyze  # noqa: E402
from grit_analyzer.report import render_report  # noqa: E402
from grit_analyzer import guard  # noqa: E402


def test_scoring_direction():
    good = analyze(f"{BASE}/good", deep=True)
    bad = analyze(f"{BASE}/bad", deep=True)
    assert good["ok"] and bad["ok"]
    assert good["overall"] > bad["overall"] + 20, \
        f"good={good['overall']} bad={bad['overall']}"
    assert bad["grade"] in ("D", "F")


def test_dashboard_contract():
    d = analyze(f"{BASE}/good", deep=True)
    for k in ("ok", "host", "final_url", "overall", "grade", "verdict",
              "scores", "priorities", "metrics", "stack", "opening_line"):
        assert k in d, f"missing {k}"
    for cat in ("speed", "search", "conversion", "local", "trust"):
        assert "label" in d["scores"][cat] and "score" in d["scores"][cat]
    m = d["metrics"]
    for k in ("load_seconds", "ttfb_ms", "page_weight_kb",
              "requests_declared", "render_blocking", "tel_links"):
        assert k in m, f"missing metric {k}"
    assert isinstance(d["stack"]["detected"], dict)
    assert len(d["priorities"]) <= 3


def test_hostile_page_renders_inert():
    d = analyze(f"{BASE}/hostile", deep=True)
    html = render_report(d)
    assert "<script>alert" not in html
    assert "onerror=alert" not in html
    # the payloads should still be VISIBLE as text (escaped), if quoted anywhere
    assert d["ok"]


def test_opening_line_is_plain_spoken():
    d = analyze(f"{BASE}/bad", deep=True)
    line = d["opening_line"].lower()
    assert "structured data" not in line and "schema" not in line \
        and "canonical" not in line, line
    assert d["host"] in d["opening_line"]


def test_contact_page_softens_form_finding():
    d = analyze(f"{BASE}/hub", deep=True)
    keys = [f["key"] for f in d["findings"]]
    assert "form_only_contact" in keys, keys
    assert "no_form" not in keys


def test_suppression():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"blocked-example.com\n")
    os.close(fd)
    old = guard.SUPPRESSION_FILE
    guard.SUPPRESSION_FILE = path
    guard._supp_cache.clear()   # per-file cache since QT-3
    try:
        assert guard.is_suppressed("https://www.blocked-example.com/page")
        assert guard.is_suppressed("sub.blocked-example.com")
        assert not guard.is_suppressed("fine-example.com")
    finally:
        guard.SUPPRESSION_FILE = old
        guard._supp_cache.clear()   # per-file cache since QT-3
        os.unlink(path)


def test_compressed_page_decodes():
    """Regression: real servers send gzip/brotli. The analyzer must score the
    decoded HTML, not compressed bytes. (Found on the first real-world run —
    rhinoroofers.com came back 'no phone number' because the body was brotli.)"""
    d = analyze(f"{BASE}/gz", deep=True)
    assert d["ok"]
    assert d["metrics"]["tel_links"] >= 1, d["metrics"]
    keys = [f["key"] for f in d["findings"]]
    assert "no_phone" not in keys, keys


def test_rate_limit_bucket():
    b = guard.TokenBucket(3, 60)
    assert all(b.allow("ip1") for _ in range(3))
    assert not b.allow("ip1")
    assert b.allow("ip2")
    assert b.retry_after("ip1") > 0


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:
                fails += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print("PASS" if not fails else f"{fails} FAILURES")
    sys.exit(1 if fails else 0)
