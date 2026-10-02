# -*- coding: utf-8 -*-
"""The spam test, and the sign-off that names the right company.

"I'm pretty sure all the emails are going to spam." The DNS check said
'passing', which only means the records exist. The answer came from sending
one real message through Mail to a scoring service: 10/10, SPF pass, DKIM
valid from the author's domain, DMARC pass, not blocklisted. That is now a
button. Along the way: every workspace signed its email "The Watson Factor
Development" - a string literal - so a customer's outreach carried Daniel's
company name. These pin both.
"""
import os, pathlib, sys, tempfile
from urllib.parse import unquote
TMP = tempfile.mkdtemp(prefix="jg-spam-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("sender_name", "Daniel Watson")
W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130")

# ── nothing run yet ────────────────────────────────────────────────────
last = W.spamtest_last(Req())
check("before any test there is no report link", last["results"], "")

# ── start one ──────────────────────────────────────────────────────────
t = W.spamtest_start(Req())
check("the address is a mail-tester one-time address",
      t["address"].startswith("test-jg") and t["address"].endswith("@srv1.mail-tester.com"), True)
slug = t["address"].split("@")[0][len("test-"):]
check("  and the report link is that same slug", t["results"], "https://www.mail-tester.com/test-" + slug)
check("  the mailto goes to that address", t["mailto"].startswith("mailto:" + t["address"] + "?"), True)
body = unquote(t["mailto"].split("&body=", 1)[1])
check("  the test carries the real sign-off, so the score is about real mail",
      "Daniel Watson" in body and "3217 Wild Iris" in body, True)
check("  and no links - a test with a link in it scores the link, not the mailbox",
      "http" in body.lower(), False)
check("  the app remembers the slug for the report link",
      W.spamtest_last(Req())["results"], t["results"])
check("  with a timestamp", bool(W.spamtest_last(Req())["started_at"]), True)

t2 = W.spamtest_start(Req())
check("a second test gets a fresh address - the old report would otherwise be reused",
      t2["address"] != t["address"], True)

# ── the company line in the signature ──────────────────────────────────
W.set_setting("sender_company", "")
sig, _ = W._signature()
check("the original workspace keeps its long-standing company line",
      "The Watson Factor Development" in sig, True)
W.set_setting("sender_company", "Watson Factor Dev")
sig, _ = W._signature()
check("  a typed company name wins", "Watson Factor Dev" in sig and "Development\n" not in sig, True)
W.set_setting("sender_company", "")
ws.CURRENT.reset(tok)

tok = ws.CURRENT.set("homerepair")
W.init_db()
W.set_setting("sender_name", "Daniel Watson")
W.set_setting("sender_address", "1 Main St")
W.set_setting("sender_company", "")
sig, _ = W._signature()
check("another workspace signs with ITS name, never Daniel's company",
      ("HomeRepair Tech" in sig, "Watson Factor" in sig), (True, False))
check("  and the Outbox brand line agrees", W.sender_brand(), "HomeRepair Tech")
ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
