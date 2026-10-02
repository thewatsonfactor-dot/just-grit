# -*- coding: utf-8 -*-
"""Missed-call text-back — and the line it must not cross.

Half of these checks are about a text going out; the other half are about a
text NOT going out. The second half is the product. A text-back that could
be pointed at a prospect list is the thing Telnyx_Calling_and_SMS_Scope.md
forbids, so the gate is tested from every side: not inbound, answered, not
recent, cooldown, STOP, the business's own number, switched off.

No real Telnyx call or SMS is ever placed: telnyx_calls._post is replaced.
Temp data dir, never touches the live databases.
"""
import json, os, pathlib, sqlite3, sys, tempfile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-tb-")
os.environ["JUST_GRIT_DATA"] = TMP
import webapp as W
import workspaces as ws
import telnyx_calls as telnyx
import textback as tb
W.DATA = pathlib.Path(TMP)
W.init_db()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

class Req:
    headers = {}; cookies = {}
W.require_auth = lambda r: "test@example.com"

SENT = []          # every POST that would have reached Telnyx
def fake_post(path, api_key, body):
    SENT.append((path, body))
    if path == "/messages":
        return {"id": "msg_%d" % len(SENT)}
    return {"call_control_id": "cc_x"}
telnyx._post = fake_post

NOW = datetime.now(timezone.utc)
def iso(dt): return dt.isoformat(timespec="seconds")

def call(**kw):
    base = {"direction": "incoming", "from_number": "+12105550123", "to_number": "+18307152300",
            "started_at": iso(NOW - timedelta(minutes=1)), "answered_at": None,
            "hung_up_at": iso(NOW), "texted_at": None}
    base.update(kw); return base

# ── the gate, condition by condition ────────────────────────────────────
ok, why = tb.should_text(call(), now=NOW)
check("a fresh, unanswered, ended inbound call earns a text", ok, True)
check("  an answered call does not", tb.should_text(call(answered_at=iso(NOW)), now=NOW)[0], False)
check("  a call still ringing does not", tb.should_text(call(hung_up_at=None), now=NOW)[0], False)
check("  an OUTBOUND call never does", tb.should_text(call(direction="outgoing"), now=NOW)[0], False)
check("  a call already texted does not", tb.should_text(call(texted_at=iso(NOW)), now=NOW)[0], False)
check("  an anonymous caller does not", tb.should_text(call(from_number="anonymous"), now=NOW)[0], False)
check("  a non-US caller does not", tb.should_text(call(from_number="+4420123456789"), now=NOW)[0], False)
check("  the business calling itself does not",
      tb.should_text(call(from_number="(830) 715-2300"), now=NOW, business_numbers=["+18307152300"])[0], False)
check("  a number that said STOP never does", tb.should_text(call(), now=NOW, opted_out=True)[0], False)
old = tb.should_text(call(started_at=iso(NOW - timedelta(hours=3)), hung_up_at=iso(NOW - timedelta(hours=3))), now=NOW)
check("  a call from three hours ago does not (a replayed webhook must not text)", old[0], False)
check("    and says why", "recent" in old[1], True)
check("  a caller texted 2 hours ago is inside the 24h cooldown",
      tb.should_text(call(), now=NOW, last_texted_at=iso(NOW - timedelta(hours=2)))[0], False)
check("  but one texted 2 days ago is not",
      tb.should_text(call(), now=NOW, last_texted_at=iso(NOW - timedelta(days=2)))[0], True)

check("STOP is an opt-out", tb.is_stop("STOP"), True)
check("  so is 'Stop texting me'", tb.is_stop("Stop texting me"), True)
check("  and 'unsubscribe'", tb.is_stop("unsubscribe"), True)
check("  'please stop by tomorrow' is not - first word rules", tb.is_stop("please stop by tomorrow"), False)
check("  'Yes call me' is not", tb.is_stop("Yes call me"), False)
check("cooldown floors at 1h", tb.clean_cooldown("0"), 24)
check("  and caps at two weeks", tb.clean_cooldown("9999"), 24)
check("the template names the business",
      tb.render("", "Blanco Cafe").startswith("Sorry we missed your call at Blanco Cafe"), True)
check("  a blank business name still reads", "{business}" in tb.render("", ""), False)
check("  a brochure is cut to two segments", len(tb.render("x " * 400, "B")) <= tb.MAX_MESSAGE_CHARS, True)

# ── there is no generic sender ──────────────────────────────────────────
check("the module exposes no public send function",
      [n for n in dir(tb) if n.startswith("send")], [])
check("  the private one is not something a prospect path could reach by name",
      hasattr(tb, "_send") and not hasattr(tb, "send_sms"), True)

# ── the webhook, end to end ─────────────────────────────────────────────
for k, v in {"telnyx_api_key": "KEY", "telnyx_connection_id": "1", "telnyx_from_number": "+18307152300",
             "telnyx_rep_number": "+12108107476", "public_base_url": "https://x.example",
             "textback_enabled": "1"}.items():
    W.set_setting(k, v)

def ev(event_type, **payload):
    return event_type, payload

# a caller rings, nobody answers, caller gives up
SENT.clear()
r1 = W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s1", call_control_id="a1",
                                     direction="incoming", **{"from": "+12105550123", "to": "+18307152300"}))
check("an inbound call is recorded", r1["did"], "recorded")
r2 = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s1", call_control_id="a1",
                                     direction="incoming", hangup_cause="originator_cancel",
                                     **{"from": "+12105550123", "to": "+18307152300"}))
check("  when the caller gives up unanswered, a text goes out", r2["texted"], True)
check("  to the caller", SENT[-1][1]["to"], "+12105550123")
check("  from the business's number", SENT[-1][1]["from"], "+18307152300")
check("  naming the business", "Watson Factor" in SENT[-1][1]["text"], True)
check("  exactly one message", len([x for x in SENT if x[0] == "/messages"]), 1)

c = W.db(); c.row_factory = sqlite3.Row
row = c.execute("SELECT * FROM inbound_calls WHERE call_session_id='s1'").fetchone()
check("  the call row records it", row["text_status"], "sent")
check("  and the log has it", c.execute("SELECT COUNT(*) FROM sms_log WHERE direction='out'").fetchone()[0], 1)

# Telnyx retries webhooks: a replayed hangup must not rewrite history
SENT.clear()
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s1", call_control_id="a1",
                                    direction="incoming", hangup_cause="originator_cancel"))
check("a replayed hangup does not text twice", r["texted"], False)
check("  and does not overwrite the row's 'sent'",
      c.execute("SELECT text_status FROM inbound_calls WHERE call_session_id='s1'").fetchone()["text_status"], "sent")
check("  nothing reached Telnyx", SENT, [])

# same caller, same day: no second text
SENT.clear()
W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s2", call_control_id="a2", direction="incoming",
                                **{"from": "+12105550123", "to": "+18307152300"}))
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s2", call_control_id="a2", direction="incoming",
                                    hangup_cause="originator_cancel", **{"from": "+12105550123", "to": "+18307152300"}))
check("the same caller ten minutes later is NOT texted again", r["texted"], False)
check("  because of the cooldown", "cooldown" in r["why"], True)
check("  and nothing reached Telnyx", SENT, [])

# an answered call: no text
SENT.clear()
W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s3", call_control_id="a3", direction="incoming",
                                **{"from": "+12105550999", "to": "+18307152300"}))
W.handle_inbound_call_event(*ev("call.answered", call_session_id="s3", call_control_id="a3", direction="incoming"))
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s3", call_control_id="a3", direction="incoming",
                                    hangup_cause="normal_clearing"))
check("an answered call is never texted", r["texted"], False)
check("  and it counts as answered", c.execute("SELECT answered_at FROM inbound_calls WHERE call_session_id='s3'").fetchone()[0] is not None, True)

# forwarding: business's real line rings; if it doesn't pick up, the caller is dropped, then texted
W.set_setting("textback_forward_to", "(210) 555-0400")
SENT.clear()
r = W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s4", call_control_id="a4", direction="incoming",
                                    **{"from": "+12105550777", "to": "+18307152300"}))
check("with a forward number set, the call is transferred to the business's line", r["did"], "forwarded")
check("  via Telnyx transfer", SENT[-1][0].endswith("/actions/transfer"), True)
check("  to that line", SENT[-1][1]["to"], "+12105550400")
# the forwarded leg times out (different call_control_id, same session, leg=forward)
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s4", call_control_id="b4", direction="outgoing",
                                    hangup_cause="timeout",
                                    client_state=telnyx.encode_state(v=1, leg="forward", sid="s4")))
check("  the forwarded leg timing out is NOT treated as the missed call", r["did"], "forward_leg_hung_up")
check("  it hangs up the caller instead", SENT[-1][0], "/calls/a4/actions/hangup")
check("  and no text yet", [x for x in SENT if x[0] == "/messages"], [])
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s4", call_control_id="a4", direction="incoming",
                                    hangup_cause="normal_clearing"))
check("  the caller's own hangup is what sends the text", r["texted"], True)

# the business answering on the forwarded leg counts as answered
SENT.clear()
W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s5", call_control_id="a5", direction="incoming",
                                **{"from": "+12105550888", "to": "+18307152300"}))
W.handle_inbound_call_event(*ev("call.answered", call_session_id="s5", call_control_id="b5", direction="outgoing",
                                client_state=telnyx.encode_state(v=1, leg="forward", sid="s5")))
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s5", call_control_id="a5", direction="incoming",
                                    hangup_cause="normal_clearing"))
check("the business picking up the forwarded leg means no text", r["texted"], False)

# STOP
SENT.clear()
import asyncio
class FakeReq:
    def __init__(self, body): self._b = body
    async def json(self): return self._b
tok = W.telnyx_webhook_token()
asyncio.run(W.telnyx_sms_webhook(tok, FakeReq({"data": {"event_type": "message.received",
    "payload": {"id": "m1", "from": {"phone_number": "+12105550777"}, "text": "STOP"}}})))
check("STOP is recorded", c.execute("SELECT COUNT(*) FROM sms_optouts WHERE number='+12105550777'").fetchone()[0], 1)
W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s6", call_control_id="a6", direction="incoming",
                                **{"from": "+12105550777", "to": "+18307152300"}))
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s6", call_control_id="a6", direction="incoming",
                                    hangup_cause="originator_cancel"))
check("  and that number is never texted again, even days later", r["texted"], False)
check("    for the stated reason", "STOP" in r["why"], True)
asyncio.run(W.telnyx_sms_webhook(tok, FakeReq({"data": {"event_type": "message.received",
    "payload": {"id": "m2", "from": {"phone_number": "+12105550123"}, "text": "Yes please call me"}}})))
check("a real reply is logged", c.execute("SELECT text FROM sms_log WHERE direction='in' AND number='+12105550123'").fetchone()[0], "Yes please call me")
check("  and is not an opt-out", c.execute("SELECT COUNT(*) FROM sms_optouts WHERE number='+12105550123'").fetchone()[0], 0)
bad = False
try: asyncio.run(W.telnyx_sms_webhook("wrong-token", FakeReq({})))
except Exception as e: bad = "404" in str(e) or "not found" in str(e).lower()
check("the SMS webhook refuses a bad token", bad, True)

# switched off
W.set_setting("textback_enabled", "")
SENT.clear()
W.handle_inbound_call_event(*ev("call.initiated", call_session_id="s7", call_control_id="a7", direction="incoming",
                                **{"from": "+12105550555", "to": "+18307152300"}))
r = W.handle_inbound_call_event(*ev("call.hangup", call_session_id="s7", call_control_id="a7", direction="incoming",
                                    hangup_cause="originator_cancel"))
check("with text-back OFF the call is still recorded", c.execute("SELECT COUNT(*) FROM inbound_calls WHERE call_session_id='s7'").fetchone()[0], 1)
check("  but nothing is sent", r["texted"], False)
check("  and it is off by default for a new workspace", W.textback_status()["enabled"], False)

# the rep-first outbound flow is untouched by any of this
SENT.clear()
W.handle_inbound_call_event(*ev("call.answered", call_session_id="out1", call_control_id="r1", direction="outgoing",
                                client_state=telnyx.encode_state(v=1, leg="rep", pid=9, prospect="+12105551111")))
check("an outbound rep leg is ignored by the inbound handler", SENT, [])

# the report numbers
rep = W.textback_recent(Req(), days=30)
check("the recent report counts calls", rep["calls"] >= 7, True)
check("  missed vs answered", rep["answered"], 2)
check("  and texted", rep["texted"], 2)
check("  and replies to those texts", rep["replied"], 1)
check("  and opt-outs", rep["optouts"], 1)
c.close()

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
