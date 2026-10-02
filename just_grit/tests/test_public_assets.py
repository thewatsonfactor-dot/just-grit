# -*- coding: utf-8 -*-
"""The /public/<name> route exists for one reason: link-preview crawlers
(iMessage's LinkPresentation, Slack, Facebook, X) fetch og:image over plain
HTTPS and can't get through Cloudflare Access, the same constraint /welcome
and /webhooks/* already work around. So this route must NEVER call
require_auth - that would silently break every rich link preview again,
the exact bug that prompted building it. It still has to be an allow-list,
not a directory listing, for the same reason /asset/<name> is one.
"""
import inspect, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-public-")
os.environ["JUST_GRIT_DATA"] = TMP
sys.path.insert(0, '.')
import webapp as W

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── no auth gate, ever ──────────────────────────────────────────────────
sig = inspect.signature(W.public_asset)
check("public_asset takes no Request - nothing to call require_auth() with",
      "request" not in sig.parameters, True)

src = inspect.getsource(W.public_asset)
check("the handler body never calls require_auth",
      "require_auth" not in src, True)

# ── allow-list, not a free-form path join ───────────────────────────────
check("og-image.png is on the allow-list", "og-image.png" in W.PUBLIC_ASSET_TYPES, True)

try:
    W.public_asset("../webapp.py")
    got = "no exception"
except Exception as e:
    got = getattr(e, "status_code", None)
check("path traversal outside the allow-list is rejected (404, not a file read)",
      got, 404)

try:
    W.public_asset("nonexistent-name.png")
    got = "no exception"
except Exception as e:
    got = getattr(e, "status_code", None)
check("a name not on the allow-list 404s", got, 404)

# ── the actual file this route exists to serve ──────────────────────────
og = W.PUBLIC / "og-image.png"
check("static/public/og-image.png is actually on disk", og.exists(), True)
if og.exists():
    check("it's a real PNG, not an empty placeholder", og.stat().st_size > 10_000, True)
    resp = W.public_asset("og-image.png")
    check("serving it returns image/png", resp.media_type, "image/png")
    check("it's cacheable for a week (crawlers hit this a lot)",
          "max-age=604800" in resp.headers.get("cache-control", ""), True)

# ── the same file under the /welcome prefix ──────────────────────────────
# Cloudflare Access bypasses match by prefix, and /welcome already has one.
# So the page points at /welcome/og-image.png and never waits on a second
# policy. Both spellings must be the SAME handler, not a copy.
routes = {r.path: r.endpoint for r in W.app.routes if hasattr(r, "path")}
check("/welcome/{name} is routed", "/welcome/{name}" in routes, True)
check("  to the same handler as /public/{name}",
      routes.get("/welcome/{name}") is routes.get("/public/{name}"), True)
html = (W.LANDING.read_text(encoding="utf-8") if W.LANDING.exists() else "")
check("the landing page's og:image lives under /welcome/",
      'og:image" content="https://justgrit.thewatsonfactor.dev/welcome/og-image.png?v=1"' in html, True)
check("  and so does twitter:image",
      'twitter:image" content="https://justgrit.thewatsonfactor.dev/welcome/og-image.png?v=1"' in html, True)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
