# -*- coding: utf-8 -*-
"""Out of leads: ask, or find more on its own.

"If it runs out of leads or leads with emails, it should prompt you to
search for more leads, or do it automatically, or ask 'you're out of leads,
would you like us to populate additional leads?'"
"""
import json, os, pathlib, sys, tempfile, time
TMP = tempfile.mkdtemp(prefix="jg-more-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws
from contextlib import closing

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
W.set_setting("sender_name", "Daniel"); W.set_setting("sender_email", "d@x.dev")
W.set_setting("sender_address", "1 Main St"); W.set_setting("default_city", "New Braunfels, TX")
W.google_key = lambda: "AIzaFAKE"
W.qualify_one = lambda d: (["AI Receptionist"], {"AI Receptionist": "x"})
W.queue_scans = lambda ids: None

SEARCHED = []
CALLS = [0]
def fake_places(query, key, pages=1, region="us"):
    SEARCHED.append(query); CALLS[0] += 1
    t = query.lower().replace(" in ", "-").replace(",", "").replace(" ", "")
    return [{"id": "g-%s-%d" % (t, i), "displayName": {"text": "%s %d" % (query.split(" in ")[0], i)},
             "websiteUri": "https://%s%d.com" % (t, i), "nationalPhoneNumber": "(830) 555-%04d" % (CALLS[0] * 100 + i),
             "businessStatus": "OPERATIONAL", "rating": 4.4, "userRatingCount": 30} for i in range(5)]
W.places_search = fake_places

class Res:
    def __init__(self, html): self.html = html; self.status = 200; self.robots_allowed = True; self.error = ""
fake_fetch = lambda url: Res('<a href="mailto:owner@%s">x</a>' % url.replace("https://", "").split("/")[0])
W.fetch_page = fake_fetch          # the background refill reads with this

# nothing on file: the refill runs out straight away and ASKS
st = W.refill_outbox(ws.PRIMARY, fetch=fake_fetch)
check("with no leads the refill stops and says it's out", (st["step"], st["need_leads"]), ("out of leads", True))
check("  and searched nothing on its own (auto is off by default)", SEARCHED, [])
s2 = W.refill_status(Req())
check("the status carries the question and a plan", (s2["need_leads"], bool(s2["plan"]["searches"])), (True, True))
check("  the plan uses a starter list of trades for what you sell", "Dentist" in s2["plan"]["trades"], True)
check("  in the towns you work, default city first", s2["plan"]["cities"][0], "New Braunfels, TX")
check("  and says what it'll cost", s2["plan"]["requests_left"] > 0, True)

# focus steers the plan
W.focus_set(Req(), W.FocusBody(trades=["Veterinary"]))
with closing(W.db()) as c:
    check("working a trade, the plan searches that trade", W.find_more_plan(c)["trades"], ["Veterinary"])
W.focus_set(Req(), W.FocusBody(trades=[]))

# "yes, find more"
r = W.leads_find_more(Req(), W.FindMoreBody(trades=["Dentist", "Chiropractor"], cities=["New Braunfels, TX"]))
check("yes → it searches exactly what was ticked", SEARCHED, ["Dentist in New Braunfels, TX", "Chiropractor in New Braunfels, TX"])
check("  and adds the businesses", r["added"], 10)
with closing(W.db()) as c:
    p = c.execute("SELECT * FROM prospects WHERE company='Dentist 0'").fetchone()
check("  each tagged with its trade (so focus can pick it)", p["category"], "Dentist")
check("  the right broad group", p["vertical"], "appointment")
check("  and due a call today", (p["next_action"], p["next_due"] is not None), ("call", True))
for _ in range(50):
    if not W.refill_state(ws.PRIMARY)["running"]: break
    time.sleep(0.1)
with closing(W.db()) as c:
    check("  then the refill reads their sites for addresses, on its own", c.execute("SELECT COUNT(*) FROM prospects WHERE email<>''").fetchone()[0], 10)
    check("  and writes to them", c.execute("SELECT COUNT(*) FROM outreach WHERE state='draft'").fetchone()[0], 10)
with closing(W.db()) as c:
    plan = W.find_more_plan(c)
check("the same trade+town isn't planned again for a month",
      any(x["trade"] == "Dentist" and x["city"] == "New Braunfels, TX" for x in plan["searches"]), False)

# "do this on its own"
W.leads_auto_find(Req(), W.FindMoreBody(auto=True))
check("the auto switch sticks", W.auto_find_on(), True)
with closing(W.db()) as c:
    c.execute("UPDATE prospects SET email_checked_at=?", (W.now(),)); c.commit()   # everyone read
SEARCHED.clear()
for _ in range(50):
    if not W.refill_state(ws.PRIMARY)["running"]: break
    time.sleep(0.1)
st = W.refill_outbox(ws.PRIMARY, fetch=fake_fetch)
check("auto on: running out triggers a search with no question", len(SEARCHED) > 0, True)
check("  within the per-run budget", len(SEARCHED) <= W.FIND_MORE_PER_RUN, True)
check("  and says what it found", st["step"].startswith("found "), True)
check("  so the next pass reads them straight away", W.refill_state(ws.PRIMARY)["need_leads"], False)

# the daily ceiling
W.refill_state(ws.PRIMARY)["auto_found"] = W.FIND_MORE_AUTO_PER_DAY
W.refill_state(ws.PRIMARY)["auto_found_day"] = W.datetime.now(W.timezone.utc).date().isoformat()
with closing(W.db()) as c:
    c.execute("UPDATE prospects SET email_checked_at=?", (W.now(),)); c.commit()
SEARCHED.clear()
st = W.refill_outbox(ws.PRIMARY, fetch=fake_fetch)
check("past 10 automatic searches a day it stops and asks instead", (SEARCHED, st["need_leads"]), ([], True))

# no key: a clear message, not a crash
W.google_key = lambda: ""
with closing(W.db()) as c:
    check("no Google key: the plan says so", W.find_more_plan(c)["has_key"], False)

check("trade → group", [W.vertical_for_trade(t) for t in ("Med Spa", "HVAC", "Car Wash", "Taqueria", "Accountant")],
      ["appointment", "contractor", "auto", "restaurant", "generic"])

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
