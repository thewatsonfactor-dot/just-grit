# -*- coding: utf-8 -*-
"""Media hosting and the Facebook publisher.

The media route is the one piece of this app that is deliberately open to the
public internet, because Meta's fetcher has no session. So its input handling
gets tested harder than anything else here.
"""
import sys, hashlib, tempfile, os
sys.path.insert(0, '.')
import media as M
import facebook as FB

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %-60s %s" % ("ok" if ok else "FAIL", name, "" if ok else repr(got)))

def raises(exc, fn, *a, **k):
    try: fn(*a, **k); return False
    except exc: return True

ROOT = tempfile.mkdtemp()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300
PNG  = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
MP4  = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 300

# ── the bytes decide, not the filename ───────────────────────────────
check("JPEG magic is detected", M.sniff(JPEG), "image/jpeg")
check("PNG magic is detected", M.sniff(PNG), "image/png")
check("MP4 ftyp is detected", M.sniff(MP4), "video/mp4")
check("a renamed text file is refused", raises(M.MediaError, M.sniff, b"hello world"), True)
check("an empty upload is refused", raises(M.MediaError, M.save, b"", ROOT), True)

# ── content addressing ───────────────────────────────────────────────
a = M.save(JPEG, ROOT)
b = M.save(JPEG, ROOT)
check("same bytes give the same id", a["id"], b["id"])
check("id is the sha256 of the content",
      a["id"], hashlib.sha256(JPEG).hexdigest() + ".jpg")
check("stored once, not twice",
      len([f for f in os.listdir(os.path.join(ROOT, "media"))]), 1)
check("a different file gets a different id", M.save(MP4, ROOT)["id"] != a["id"], True)

# ── path traversal: the whole reason this route is safe ──────────────
for evil in ["../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd",
             "a.jpg", "....//etc/passwd.jpg", a["id"] + "/../../etc/passwd",
             "", None, "deadbeef.jpg", a["id"].upper()]:
    check("resolve refuses %r" % (evil,), raises(M.MediaError, M.resolve, evil, ROOT), True)
p, ct = M.resolve(a["id"], ROOT)
check("resolve accepts what we generated", ct, "image/jpeg")

# ── the hosting check: the thing that breaks everything silently ─────
def access_page(url): return 200, "<title>Sign in ・ Cloudflare Access</title>"
def not_found(url):   return 404, "not found"
def forbidden(url):   return 403, "nope"
check("Cloudflare Access is detected and named",
      "Cloudflare Access" in M.hosting_problem("https://x.dev", access_page), True)
check("and it says what to do about it",
      "Bypass" in M.hosting_problem("https://x.dev", access_page), True)
check("a 404 from our own host is the CORRECT answer",
      M.hosting_problem("https://x.dev", not_found), "")
check("a 403 is reported", "anonymous" in M.hosting_problem("https://x.dev", forbidden), True)
check("no base url is reported",
      "no URL to hand" not in M.hosting_problem("", not_found)
      and M.hosting_problem("", not_found) != "", True)

# ── facebook: refuse before spending a round trip ────────────────────
check("publish needs a page id", raises(FB.FacebookError, FB.publish, "", "t", "hi"), True)
check("publish needs a token", raises(FB.FacebookError, FB.publish, "1", "", "hi"), True)
check("an empty post is refused", raises(FB.FacebookError, FB.publish, "1", "t", ""), True)
check("a local file as image_url is refused",
      raises(FB.FacebookError, FB.publish, "1", "t", "hi", "/tmp/a.jpg"), True)

# ── routing: photos vs feed. Getting this wrong drops the image ──────
seen = {}
FB._post = lambda path, params: seen.update(path=path, params=params) or {"id": "1_2"}
FB.publish("PAGE", "TOK", message="hello")
check("text-only goes to /feed", seen["path"], "PAGE/feed")
FB.publish("PAGE", "TOK", message="cap", image_url="https://x/a.jpg")
check("an image goes to /photos, not /feed", seen["path"], "PAGE/photos")
check("and the image is sent as `url`", seen["params"]["url"], "https://x/a.jpg")
check("with the text as `caption`", seen["params"]["caption"], "cap")

# ── unpublished by default ───────────────────────────────────────────
check("a post is created UNPUBLISHED unless asked", seen["params"]["published"], "false")
FB.publish("PAGE", "TOK", message="go", published=True)
check("published=True is honoured", seen["params"]["published"], "true")

# ── Meta's errors, translated ────────────────────────────────────────
check("expired token explains the long-lived exchange",
      "long-lived" in FB._explain(400, {"code": 190, "message": "x"}, ""), True)
check("permission error names the PAGE token trap",
      "PAGE token" in FB._explain(400, {"code": 200, "message": "x"}, ""), True)
check("unfetchable url points at Access/localhost",
      "anonymous" in FB._explain(400, {"code": 100, "message": "bad url"}, ""), True)

# ── the Instagram id lives on the Page, not on Instagram ─────────────
got = {}
FB._get = lambda path, params: got.update(path=path, params=params) or {
    "instagram_business_account": {"id": "17841400000000000", "username": "homerepair.tech"}}
ig = FB.instagram_account("1311864045336201", "PAGETOK")
check("looks the ig account up on the PAGE", got["path"], "1311864045336201")
check("asks for the nested id and username",
      got["params"]["fields"], "instagram_business_account{id,username}")
check("returns the numeric id", ig["id"], "17841400000000000")
check("and the handle", ig["username"], "homerepair.tech")

FB._get = lambda path, params: {}
check("no linked account is an empty answer, not a crash",
      FB.instagram_account("1", "T"), {"id": "", "username": ""})

print("-" * 74)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
