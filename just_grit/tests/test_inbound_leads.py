# -*- coding: utf-8 -*-
"""A person who calls the business is a lead. Somewhere you can work.

This is the hole Daniel found by asking the obvious question - "when
someone calls the number, where is that lead stored?" - and the honest
answer was: in `inbound_calls`, a log table, with no name, no queue entry
and no way to act on it. The one number the whole product exists to
protect was the only number whose callers were invisible. Worse, a caller
who got the text-back and WROTE BACK - the warmest signal this system can
receive - stopped at a row in `sms_log`.

Every inbound path now goes through `inbound_lead()`: the AI receptionist,
a missed call, and a text-back reply. One caller is one record however
they arrive, and a reply lands at the top of Today.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-inbound-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws, textback as tb
from contextlib import closing
W.DATA = pathlib.Path(TMP)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

CALLER = "+12105551234"
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("telnyx_api_key", "KEY_test")
W.set_setting("telnyx_from_number", "+18307152300")
W.set_setting("telnyx_rep_number", "+12108107476")
W.set_setting("telnyx_connection_id", "111")

def rows(sql, *a):
    with closing(W.db()) as c:
        return [dict(r) for r in c.execute(sql, a)]

def one(sql, *a):
    r = rows(sql, *a)
    return r[0] if r else None

# ── a missed call, with text-back off ────────────────────────────────────
W.set_setting("textback_enabled", "")
W.handle_inbound_call_event("call.initiated", {
    "call_session_id": "s1", "call_control_id": "c1", "direction": "incoming",
    "from": CALLER, "to": "+18307152300"})
out = W.handle_inbound_call_event("call.hangup", {
    "call_session_id": "s1", "call_control_id": "c1", "hangup_cause": "normal_clearing"})
check("a missed call creates a lead even with text-back off", bool(out["prospect_id"]), True)
p = one("SELECT * FROM prospects WHERE id=?", out["prospect_id"])
check("  named so it is findable", p["company"], "Inbound caller " + CALLER)
check("  with the number on the record", p["phone"], CALLER)
check("  status 'inbound', never 'new' - it must not join the cold-call queue",
      p["status"], "inbound")
check("  and it shows in the Pipeline as a lead", p["stage"], "lead")
check("  due for a callback today, which is the whole point",
      (p["next_due"] is not None, p["next_action"]), (True, "call"))
t = one("SELECT * FROM touches WHERE prospect_id=? ORDER BY id DESC", p["id"])
check("  the miss is in the history", (t["kind"], t["outcome"]), ("call", "missed"))

# ── the same caller rings again: one lead, not two ───────────────────────
W.handle_inbound_call_event("call.initiated", {
    "call_session_id": "s2", "call_control_id": "c2", "direction": "incoming",
    "from": "(210) 555-1234", "to": "+18307152300"})
out2 = W.handle_inbound_call_event("call.hangup", {
    "call_session_id": "s2", "call_control_id": "c2", "hangup_cause": "normal_clearing"})
check("calling back reuses the same lead", out2["prospect_id"], out["prospect_id"])
check("  even written the way a human types it", len(rows("SELECT id FROM prospects")), 1)
check("  and both calls are in the history",
      len(rows("SELECT id FROM touches WHERE prospect_id=? AND kind='call'", p["id"])), 2)

# ── an answered call is not a missed one ─────────────────────────────────
W.handle_inbound_call_event("call.initiated", {
    "call_session_id": "s3", "call_control_id": "c3", "direction": "incoming",
    "from": "+12105559999", "to": "+18307152300"})
W.handle_inbound_call_event("call.answered", {"call_session_id": "s3", "call_control_id": "c3"})
out3 = W.handle_inbound_call_event("call.hangup", {
    "call_session_id": "s3", "call_control_id": "c3", "hangup_cause": "normal_clearing"})
check("a call somebody ANSWERED makes no missed-call lead", out3["prospect_id"], None)

# ── a replayed hangup does not make a second lead ────────────────────────
before = len(rows("SELECT id FROM touches WHERE prospect_id=?", p["id"]))
W.handle_inbound_call_event("call.hangup", {
    "call_session_id": "s1", "call_control_id": "c1", "hangup_cause": "normal_clearing"})
check("Telnyx replaying a hangup does not log the same call twice",
      len(rows("SELECT id FROM touches WHERE prospect_id=?", p["id"])), before)

# ── the text-back reply: the warmest thing that happens here ─────────────
import asyncio, json

class FakeReq:
    def __init__(self, payload): self._p = payload
    async def json(self): return self._p

def sms(frm, text):
    return asyncio.get_event_loop().run_until_complete(
        W.telnyx_sms_webhook(W.telnyx_webhook_token(), FakeReq(
            {"data": {"event_type": "message.received",
                      "payload": {"from": {"phone_number": frm}, "text": text, "id": "m1"}}})))

r = sms(CALLER, "yes please, can someone come look at it tomorrow?")
check("a reply attaches to the caller's existing lead", r["prospect_id"], p["id"])
p2 = one("SELECT * FROM prospects WHERE id=?", p["id"])
check("  they go to the TOP of today's list", p2["next_due"] is not None, True)
check("  status 'conversation' - a human is talking, the machine stops",
      p2["status"], "conversation")
check("  next action is a call back", p2["next_action"], W.CALLBACK_ACTION)
t = one("SELECT * FROM touches WHERE prospect_id=? AND kind='sms' ORDER BY id DESC", p["id"])
check("  their actual words are in the history",
      "come look at it tomorrow" in (t["note"] or ""), True)
check("  logged as a reply", t["outcome"], "reply")

# ── STOP is recorded, never escalated ────────────────────────────────────
r = sms("+12105558888", "STOP")
check("STOP from a stranger still creates the record", bool(r["prospect_id"]), True)
ps = one("SELECT * FROM prospects WHERE id=?", r["prospect_id"])
check("  but is NOT put on the call list - they said leave me alone",
      ps["status"], "inbound")
check("  and the opt-out is honoured forever",
      bool(one("SELECT 1 AS x FROM sms_optouts WHERE number=?", "+12105558888")), True)
ts = one("SELECT * FROM touches WHERE prospect_id=? ORDER BY id DESC", r["prospect_id"])
check("  with a note nobody can misread", ts["outcome"], "opted_out")

# ── a prospect we already know calls US back ─────────────────────────────
with closing(W.db()) as c:
    c.execute("INSERT INTO prospects (place_id, company, phone, status, stage, added_at) "
              "VALUES ('place_abc','Blanco Cafe','(830) 214-9000','new','lead',?)", (W.now(),))
    known = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
W.handle_inbound_call_event("call.initiated", {
    "call_session_id": "s9", "call_control_id": "c9", "direction": "incoming",
    "from": "+18302149000", "to": "+18307152300"})
out9 = W.handle_inbound_call_event("call.hangup", {
    "call_session_id": "s9", "call_control_id": "c9", "hangup_cause": "normal_clearing"})
check("a business we already email is recognised when it calls back",
      out9["prospect_id"], known)
pk = one("SELECT * FROM prospects WHERE id=?", known)
check("  it stays Blanco Cafe, not 'Inbound caller +1830…'", pk["company"], "Blanco Cafe")
check("  and it is due a call today", pk["next_due"] is not None, True)

# ── a contact's mobile, not the business's listed line ───────────────────
with closing(W.db()) as c:
    c.execute("INSERT INTO prospects (place_id, company, phone, status, stage, added_at) "
              "VALUES ('place_xyz','Moctezuma Masonry','','new','lead',?)", (W.now(),))
    biz = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO contacts (prospect_id, name, phone, is_primary, added_at) "
              "VALUES (?,'Luis','210-555-7777',1,?)", (biz, W.now()))
    c.commit()
pid = None
with closing(W.db()) as c:
    pid = W.inbound_lead(c, "+12105557777", kind="call", outcome="missed", note="x")
    c.commit()
check("a named contact's own mobile resolves to their business", pid, biz)

# ── the queue actually shows them ────────────────────────────────────────
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
q = W.queue(Req())
ids = {r["id"] for r in q.get("rows", q.get("queue", []))} if isinstance(q, dict) else set()
check("the replying caller is on the Today queue", p["id"] in ids, True)
check("the missed-call business is on it too", known in ids, True)
check("the STOP number is not", r["prospect_id"] not in ids, True)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
