# -*- coding: utf-8 -*-
"""The calendar: one week, everything the app is doing.

Daniel, 2026-09-26, looking at GoHighLevel's calendar: "I need this to show
posts and everything else with the app." No network.
"""
import os, pathlib, sys, tempfile, json
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-cal-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws, socialweek as SW

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
tz = W.workspace_tz()
mon = SW.monday_of("2026-W40")                       # Sep 28 - Oct 4, 2026
at = lambda d, h, m=0: datetime(mon.year, mon.month, mon.day, h, m, tzinfo=tz) + timedelta(days=d)
with W.closing(W.db()) as c:
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, kind, created_at) VALUES (?,?,?,?,?,?)",
              ('["facebook", "instagram"]', "Spring AC, summer water heater\nmore", at(2, 18, 30).astimezone(timezone.utc).isoformat(), "scheduled", "offer", W.now()))
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, kind, created_at, published_at) VALUES (?,?,?,?,?,?,?)",
              ('["facebook"]', "Fall tip", at(1, 12).astimezone(timezone.utc).isoformat(), "published", "tip", W.now(), W.now()))
    c.execute("INSERT INTO prospects (company, category, email, phone, status, stage, next_due, next_action, step, added_at) VALUES "
              "('Edwards PM','property_manager','x@x.com','+12105550100','working','lead',?, 'email_followup', 2, ?)", (at(3, 0).strftime("%Y-%m-%d"), W.now()))
    c.execute("INSERT INTO prospects (company, category, email, status, stage, next_due, next_action, added_at) VALUES "
              "('Misty Oaks HOA','hoa_board','y@y.com','working','lead',?, 'call', ?)", (at(4, 0).strftime("%Y-%m-%d"), W.now()))
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, sent_at, variant, step) VALUES "
              "(1,'x','x@x.com','before the work order exists','b','sent',?,?,'seq:property_manager:slow_response',1)",
              (W.now(), at(0, 9, 5).astimezone(timezone.utc).isoformat()))
    c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at) VALUES (2,'sms','reply','Texted back: call me',?)",
              (at(2, 15, 40).astimezone(timezone.utc).isoformat(),))
    c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at) VALUES (1,'email','sent','Sent: x',?)",
              (at(0, 9, 5).astimezone(timezone.utc).isoformat(),))
    c.commit()

d = W.calendar_week(Req(), week="2026-W40")
check("the week runs Monday to Sunday", (d["days"][0], d["days"][6], d["week"]), ("2026-09-28", "2026-10-04", "2026-W40"))
by = {(i["kind"], i["date"]): i for i in d["items"]}
check("a scheduled post is on its day at its local time",
      (by[("post", "2026-09-30")]["time"], by[("post", "2026-09-30")]["label"], by[("post", "2026-09-30")]["sub"], by[("post", "2026-09-30")]["state"]),
      ("18:30", "Spring AC, summer water heater", "facebook, instagram · offer", "scheduled"))
check("  a published one shows as done", by[("post", "2026-09-29")]["state"], "published")
# Daniel, 2026-09-27: the board was a wall of cards. Sent emails are counted
# in the day's 5 PM recap; what's due is one "to work" card per day; only
# posts and real conversations are drawn one by one.
check("a sent email is not its own card any more", sum(1 for i in d["items"] if i["kind"] == "email"), 0)
rc = d["recaps"]["2026-09-28"]
check("  it's counted in that day's recap, once (the touch isn't double-counted)", rc["sent"], 1)
check("  and listed with the company in the recap's detail", any("Edwards PM" in l["text"] for l in rc["log"]), True)
check("a follow-up due is one 'to work' card for its day",
      (d["due"]["2026-10-01"]["total"], d["due"]["2026-10-01"]["by"]), (1, {"follow-up emails": 1}))
check("a call due counts as a call", d["due"]["2026-10-02"]["by"], {"calls": 1})
check("a text back is on the board", (by[("text", "2026-09-30")]["time"], by[("text", "2026-09-30")]["label"]), ("15:40", "Misty Oaks HOA"))
check("future days get no recap", "2026-10-04" in d["recaps"], False)
keys = [(i["date"], i["time"] or "00:00") for i in d["items"]]
check("items are in date/time order", keys == sorted(keys))
check("prev/next weeks are given", (d["prev"], d["next"]), ("2026-W39", "2026-W41"))
check("a bad week falls back to this week", W.calendar_week(Req(), week="nonsense")["week"], SW.week_of(datetime.now(tz).date()))
html = open("static/dashboard.html").read()
check("the sidebar has Calendar and the page has the board", ('data-tab="calendar"' in html, 'id="calBoard"' in html), (True, True))
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
