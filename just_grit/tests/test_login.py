# -*- coding: utf-8 -*-
"""Sign in by email code - the door a stranger can open.

Commercial plan, 26 Sep 2026: there was no sign-in page; the wrong email
got a raw 403 string. Now /login → /auth/login emails a six-digit code
through the owner's mailbox → /auth/verify sets a session cookie.
Which workspace the address may open is still require_auth's decision.
"""
import os, pathlib, sys, tempfile, json
TMP = tempfile.mkdtemp(prefix="jg-login-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import auth
import webapp as W, workspaces as ws
from fastapi.testclient import TestClient

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.set_setting("allowed_emails", "daniel@example.com, ann@example.com")
W.set_setting("imap_host", "imap.example.com"); W.set_setting("imap_user", "daniel@example.com"); W.set_setting("imap_password", "x")
ws.CURRENT.reset(tok)

SENT = []
class FakeSMTP:
    def __init__(self, h, p): pass
    def ehlo(self): pass
    def starttls(self): pass
    def login(self, u, p): pass
    def send_message(self, msg, *a, **k): SENT.append(msg)
    def quit(self): pass
    def close(self): pass
W._login_smtp_factory = lambda h, p: FakeSMTP(h, p)

# pure module
with W.closing(W.auth_db()) as c:
    code, why = auth.issue_code(c, "Daniel@Example.com")
    check("a code is six digits", (len(code), code.isdigit(), why), (6, True, None))
    t, why = auth.verify_code(c, "daniel@example.com", "000000" if code != "000000" else "111111")
    check("a wrong code is refused with tries left", (t, why), (None, "wrong"))
    t, why = auth.verify_code(c, "daniel@example.com", code)
    check("the right code returns a session token", (bool(t), why), (True, None))
    check("  and the cookie resolves to the email", auth.session_email(c, t), "daniel@example.com")
    t2, why = auth.verify_code(c, "daniel@example.com", code)
    check("  a code is one use", (t2, why), (None, "expired"))
    for i in range(5):
        auth.issue_code(c, "daniel@example.com")
    check("more than five codes in a window is refused", auth.issue_code(c, "daniel@example.com")[1], "too_many")
    auth.end_session(c, t)
    check("sign-out kills the session", auth.session_email(c, t), None)

# through the app, as a stranger off the Mac would (fake a Cloudflare header so loopback doesn't wave us in)
app = TestClient(W.app, base_url="https://justgrit.example")
H = {"cf-ray": "abc", "accept": "text/html"}
r = app.get("/", headers=H, follow_redirects=False)
check("nobody signed in is sent to /login, not a 403 string", (r.status_code, r.headers.get("location", "")[:6]), (302, "/login"))
r = app.get("/login", headers=H)
check("the sign-in page renders", (r.status_code, "Send my code" in r.text), (200, True))
r = app.post("/auth/login", json={"email": "nobody@example.com"}, headers={"cf-ray": "abc"})
check("an unknown address is told plainly", (r.status_code, "isn't on this account" in r.json()["detail"]), (403, True))
r = app.post("/auth/login", json={"email": "not an email"}, headers={"cf-ray": "abc"})
check("a non-address is caught", r.status_code, 400)
SENT.clear()
r = app.post("/auth/login", json={"email": "Ann@example.com"}, headers={"cf-ray": "abc"})
check("a known address gets a code emailed", (r.status_code, len(SENT)), (200, 1))
subj = SENT[0]["Subject"]
code = subj.split(" ")[0]
check("  subject leads with the code", (len(code), code.isdigit(), "sign-in code" in subj), (6, True, True))
check("  the code is not in the JSON reply", "code" in r.json(), False)
r = app.post("/auth/verify", json={"email": "ann@example.com", "code": "123"}, headers={"cf-ray": "abc"})
check("wrong code is a sentence, not a log line", (r.status_code, r.json()["detail"]), (400, "That code doesn't match. Check the email and try again."))
r = app.post("/auth/verify", json={"email": "ann@example.com", "code": code}, headers={"cf-ray": "abc"})
check("right code signs in and sets the cookie", (r.status_code, auth.COOKIE in r.cookies, "HttpOnly" in r.headers.get("set-cookie", "")), (200, True, True))
check("  secure flag on when it came through Cloudflare", "Secure" in r.headers.get("set-cookie", ""))
r = app.get("/api/me", headers={"cf-ray": "abc"})
check("the cookie is an identity", (r.status_code, r.json()["email"], r.json()["signed_in"]), (200, "ann@example.com", True))
r = app.get("/logout", headers=H, follow_redirects=False)
check("sign out clears it", r.status_code, 302)
r = app.get("/api/me", headers={"cf-ray": "abc"})
check("  and the old cookie no longer opens anything", r.status_code, 401)

# a guest of one workspace gets a page, not a string, on the other
r = app.get("/", headers={ws_hdr: v for ws_hdr, v in H.items()} | {W.ACCESS_HEADER: "guest@other.com"}, follow_redirects=False)
check("a signed-in stranger gets a friendly page", (r.status_code, "isn't on the account" in r.text, "Sign in with another email" in r.text), (403, True, True))
r = app.get("/api/prospects", headers={W.ACCESS_HEADER: "guest@other.com"})
check("  the API still answers JSON", (r.status_code, "detail" in r.json()), (403, True))

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
