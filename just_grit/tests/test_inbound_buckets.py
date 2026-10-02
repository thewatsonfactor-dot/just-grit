# -*- coding: utf-8 -*-
"""Inbound leads sorted into four buckets, each with a reply drafted.

From the Small Business plugin's speed-to-lead skill, 2026-09-25: hot ->
call now; qualified -> answer, confirm, two real times; unclear -> one
question; out of scope -> honest answer anyway. Under 100 words, no
automation disclaimer, nothing invented. No network: Telnyx is faked.
"""
import os, pathlib, sys, tempfile
from datetime import datetime
TMP = tempfile.mkdtemp(prefix="jg-inbound-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import inbound as I
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

H = dict(workspace="homerepair")
check("an AC out in September is hot", I.sort("My AC went out and it's 95 in here", **H)["bucket"], "hot")
check("  and the reason names their words", I.sort("My AC went out", **H)["why"], 'they said "ac went out"')
check("a requested time is hot", I.sort("Can you come by", requested_time="Thursday morning", **H)["bucket"], "hot")
check("the receptionist's own hot flag is hot", I.sort("wants a plan", urgency="hot", **H)["bucket"], "hot")
check("a repeat customer is hot", I.sort("hey, need another visit", repeat=True, **H)["bucket"], "hot")
check("an urgent pool leak is still hot - urgency beats fit for routing", I.sort("pool is leaking bad, urgent", **H)["bucket"], "hot")
check("a plan question with no rush is qualified", I.sort("How much is the essential plan for a rental in Schertz?", **H)["bucket"], "qualified")
check("'info' is unclear", I.sort("info", **H)["bucket"], "unclear")
check("  and gets the one question that changes the answer", "house you live in" in I.sort("info", **H)["question"])
check("'call me' is qualified - they told us what they want", I.sort("call me please", **H)["bucket"], "qualified")
check("a pool job is out of scope", (I.sort("Do you clean pools?", **H)["bucket"], I.sort("Do you clean pools?", **H)["topic"]), ("out_of_scope", "pool work"))
check("a remodel is out of scope", I.sort("Looking for a kitchen remodel quote", **H)["bucket"], "out_of_scope")
check("Watson Factor: a vendor pitch is out of scope", I.sort("Our agency offers SEO packages", workspace="watson")["bucket"], "out_of_scope")
check("  a website ask is qualified", I.sort("Do you build websites for restaurants?", workspace="watson")["bucket"], "qualified")
check("  'pricing?' is unclear with its own question", "calls, reviews, or orders" in I.sort("pricing?", workspace="watson")["question"])

fri = datetime(2026, 9, 25, 8, 15)
check("slots: Friday 8:15am offers today 10 and today 3", I.next_slots(fri), ["today 10:00 AM", "today 3:00 PM"])
check("  Friday 2pm offers today 3 and Monday 10 (no weekends)", I.next_slots(datetime(2026, 9, 25, 14)), ["today 3:00 PM", "Mon 10:00 AM"])
check("  Daniel's own times, in his own format", I.next_slots(fri, ["9am", "4:30 pm"]), ["today 9:30 AM" if False else "today 4:30 PM", "Mon 9:00 AM"])
check("  a bad setting falls back", I.next_slots(fri, ["whenever"]), ["today 10:00 AM", "today 3:00 PM"])

def d(text, **k):
    return I.draft(I.sort(text, **H), text=text, name="Ann Carter", business="HomeRepair Tech", sender="Daniel",
                   slots=["today 10:00 AM", "today 3:00 PM"], workspace="homerepair", **k)
hot = d("My AC went out and it's 95 in here")
check("hot reply: says you're calling in minutes, in their words", ("calling you in the next few minutes" in hot, '"My AC went out' in hot), (True, True))
q = d("How much is the essential plan for a rental in Schertz?")
check("qualified reply: confirms and offers the two times", ("yes, that's what we do" in q, "today 10:00 AM or today 3:00 PM" in q), (True, True))
u = d("info")
check("unclear reply: one question, no pitch", ("house you live in" in u, "plan" in u.lower()), (True, False))
o = d("Do you clean pools?")
check("out of scope: honest, and doesn't leave them hanging", ("pool work isn't something we take on" in o, "leave you hanging" in o), (True, True))
o2 = d("Do you clean pools?", referral="Blue Water Pools handles that - ask for Teri, tell her I sent you.")
check("  with a referral from Setup, it points them on", "ask for Teri" in o2)
check("every reply is under 100 words", max(I.word_count(x) for x in (hot, q, u, o, o2)) < 100)
check("  none says it's automated", any("automat" in x.lower() for x in (hot, q, u, o, o2)), False)
check("  opens with the person's first name and who's writing", hot.startswith("Hi Ann, Daniel with HomeRepair Tech."))
check("  no name: still opens plainly", I.draft(I.sort("info", **H), text="info", name="", business="HomeRepair Tech", sender="Daniel",
                                                slots=[], workspace="homerepair").startswith("Hi, Daniel with HomeRepair Tech."))
check("  email shape signs off", d("call me", channel="email").endswith("\n\nDaniel"))

# ── the routes: a text reply and a receptionist log both get sorted ──
W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("sender_name", "Daniel Watson"); W.set_setting("callback_times", "10:00, 15:00")
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
with W.closing(W.db()) as c:
    pid = W.inbound_lead(c, "+12105550111", name="Ann Carter", kind="sms", outcome="reply", note="Texted back", hot=True)
    srt = W.sort_inbound(c, pid, "How much is the essential plan for a rental in Schertz?")
    c.commit()
    p = dict(c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone())
check("a text reply is sorted and the draft stored on the lead",
      (p["inbound_bucket"], p["inbound_reply"].startswith("Hi Ann, Daniel with " + ws.info()["short"] + "."), "yes, that's what we do" in p["inbound_reply"]),
      ("qualified", True, True))
rows = W.queue(Req())["queue"]
check("  and Today carries it", [r["inbound_bucket"] for r in rows if r["id"] == pid], ["qualified"])

sent = []
W.tb._send = lambda **k: sent.append(k) or "msg_1"
W.set_setting("telnyx_api_key", "KEY"); W.set_setting("telnyx_from_number", "+18305550100")
r = W.text_prospect(Req(), pid, W.TextBody(text="Hi Ann, Daniel here. Calling you at 10."))
check("'Text it' sends through Telnyx to their number", (r["ok"], sent[0]["to_number"], sent[0]["text"]), (True, "+12105550111", "Hi Ann, Daniel here. Calling you at 10."))
with W.closing(W.db()) as c:
    check("  logged as an outbound text and a touch",
          (c.execute("SELECT COUNT(*) FROM sms_log WHERE direction='out' AND number='+12105550111'").fetchone()[0],
           c.execute("SELECT note FROM touches WHERE prospect_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()[0][:7]), (1, "Texted:"))
    check("  the draft is cleared once sent", c.execute("SELECT inbound_reply FROM prospects WHERE id=?", (pid,)).fetchone()[0], "")
    c.execute("INSERT INTO sms_optouts (number, at, source) VALUES ('+12105550111', ?, 'sms:stop')", (W.now(),)); c.commit()
try:
    W.text_prospect(Req(), pid, W.TextBody(text="again")); got = ""
except W.HTTPException as e:
    got = e.detail
check("  never to a number that said STOP", "STOP" in got)

# the receptionist path: a hot caller goes to the top of Today
import asyncio
class FakeReq:
    async def json(self): return {"caller_phone": "+12105550122", "caller_name": "Bob Reyes", "reason": "water heater is leaking into the garage",
                                  "urgency": "normal", "requested_time": "", "notes": "", "transferred": False}
W.telnyx_webhook_token = lambda: "tok"
r = asyncio.run(W.telnyx_assistant_webhook("tok", FakeReq()))
with W.closing(W.db()) as c:
    p = dict(c.execute("SELECT * FROM prospects WHERE id=?", (r["prospect_id"],)).fetchone())
check("the receptionist's notes are sorted too - a leak is hot even when the assistant said 'normal'",
      (r["bucket"], p["inbound_bucket"], p["status"], p["next_action"]), ("hot", "hot", "conversation", W.CALLBACK_ACTION))
check("  with the reply drafted", "calling you in the next few minutes" in p["inbound_reply"])
ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
