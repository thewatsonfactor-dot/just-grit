# -*- coding: utf-8 -*-
"""A customer texting back must reach the owner, once.

Until 17 Nov 2026 every non-STOP inbound text crashed the SMS webhook after
it had saved the row: the alert line had been copied from the AI-receptionist
handler and named variables that don't exist here (phone, name, note). The
reply was logged, but no alert went out, Telnyx got a 500, and Telnyx's
retries logged the same text again. These checks hold all three shut.

No email or Telnyx call leaves the machine: the alert sender and the pool
are replaced. Temp data dir, never the live databases.
"""
import asyncio, os, pathlib, sys, tempfile
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-smsalert-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
import webapp as W
W.DATA = pathlib.Path(TMP)
W.init_db()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

ALERTS = []
W._send_call_alert = lambda to, subject, body: ALERTS.append((to, subject, body))
class NowPool:                       # run the alert inline so the test can see it
    def submit(self, fn, *a, **kw): return fn(*a, **kw)
W.POOL = NowPool()
W.set_setting("call_alert_email", "owner@example.com")

class FakeReq:
    def __init__(self, body): self._b = body
    async def json(self): return self._b

def text_in(mid, frm, text):
    return asyncio.run(W.telnyx_sms_webhook(W.telnyx_webhook_token(), FakeReq(
        {"data": {"event_type": "message.received",
                  "payload": {"id": mid, "from": {"phone_number": frm}, "text": text}}})))

def count(sql, *args):
    with W.closing(W.db()) as c:
        return c.execute(sql, args).fetchone()[0]

# ── a real reply ────────────────────────────────────────────────────────
r = None
try:
    r = text_in("m-100", "+12105550123", "Yes please call me after 3")
except Exception as e:
    print("     webhook raised: %r" % e)
check("a customer's text reply does not crash the webhook", isinstance(r, dict), True)
r = r or {}
check("  it answers ok, so Telnyx doesn't send it again", r.get("ok"), True)
check("  it lands on a lead", bool(r.get("prospect_id")), True)
check("  the owner gets exactly one alert", len(ALERTS), 1)
to, subject, body = ALERTS[0] if ALERTS else ("", "", "")
check("    to the alert address", to, "owner@example.com")
check("    worded as a text, not a call", subject.startswith("Text reply"), True)
check("    with what they wrote", "Yes please call me after 3" in body, True)
check("    and who sent it", "(210) 555-0123" in body, True)
check("    and it never says they called", "called" in body.lower(), False)

# ── Telnyx retries the same event ───────────────────────────────────────
r2 = text_in("m-100", "+12105550123", "Yes please call me after 3")
check("the same message id a second time is recognised", r2.get("duplicate"), True)
check("  one row in the text log, not two",
      count("SELECT COUNT(*) FROM sms_log WHERE direction='in' AND message_id='m-100'"), 1)
check("  and still one alert", len(ALERTS), 1)

# ── STOP stays quiet ────────────────────────────────────────────────────
ALERTS.clear()
r3 = text_in("m-101", "+12105550999", "STOP")
check("STOP is still honoured", r3.get("stop"), True)
check("  and nobody is alerted about it", ALERTS, [])

# ── alerts switched off ────────────────────────────────────────────────
W.set_setting("call_alert_off", "1")
r4 = text_in("m-102", "+12105550444", "Do you do HOA inspections?")
check("with alerts off the reply is still logged", bool(r4.get("prospect_id")), True)
check("  and no alert goes out", ALERTS, [])
W.set_setting("call_alert_off", "")

# ── a broken mailbox never breaks the webhook ──────────────────────────
def boom(*a, **kw): raise RuntimeError("smtp down")
W._send_call_alert = boom
try:
    r5 = text_in("m-103", "+12105550555", "Call me")
    check("a failing alert still answers Telnyx ok", r5.get("ok"), True)
except Exception as e:
    check("a failing alert still answers Telnyx ok", repr(e), "no exception")

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
