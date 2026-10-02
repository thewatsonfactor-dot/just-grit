# -*- coding: utf-8 -*-
"""Keep the dashboard clean: hide trades, pick new ones from a list.

Daniel: "i like to keep my dashboards clean. i need to be able to delete
trades i don't want to see" and "maybe make the add trades a drop down as
well as type in a trade".
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-hide-")
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
W.set_setting("default_city", "New Braunfels, TX")
W.google_key = lambda: "AIzaFAKE"

with closing(W.db()) as c:
    for i, (cat, vert) in enumerate([("Dental", "appointment"), ("Tattoo Shop", "generic"), ("Roofing", "contractor")]):
        c.execute("INSERT INTO prospects (place_id, company, status, stage, added_at, category, vertical) VALUES (?,?,?,?,?,?,?)",
                  ("pl:%d" % i, "Biz %d" % i, "new", "lead", W.now(), cat, vert))
    c.commit()

opts = lambda: [o["key"] for o in W.focus_get(Req())["options"]]
check("every trade on file shows in the picker", "Tattoo Shop" in opts(), True)

W.focus_set(Req(), W.FocusBody(trades=["Tattoo Shop", "Dental"]))
r = W.trades_hide(Req(), W.HideTradeBody(trade="tattoo shop"))
check("hiding a trade (any case) lists it as hidden", [h.lower() for h in r["hidden"]], ["tattoo shop"])
check("  it's gone from the picker", "Tattoo Shop" in opts(), False)
check("  and dropped from what you're working", W.focus_get(Req())["trades"], ["Dental"])
with closing(W.db()) as c:
    plan = W.find_more_plan(c)
check("  the find-more plan never suggests it", any(t.lower() == "tattoo shop" for t in plan["trades"]), False)
check("  nor the dropdown", any(t.lower() == "tattoo shop" for t in plan["menu"]), False)
check("  the plan tells the page what's hidden (for the restore link)", [h.lower() for h in plan["hidden"]], ["tattoo shop"])

W.focus_set(Req(), W.FocusBody(trades=[]))
W.trades_hide(Req(), W.HideTradeBody(trade="Dentist"))
with closing(W.db()) as c:
    plan = W.find_more_plan(c)
check("hiding a starter trade takes it out of the plan", "Dentist" in plan["trades"], False)
check("  the plan still has enough trades to search", len(plan["trades"]) >= 4, True)

check("the dropdown is a long list to pick from", len(plan["menu"]) > 40, True)
check("  sorted A-Z", plan["menu"] == sorted(plan["menu"], key=str.lower), True)
check("  includes trades already on file", "Roofing" in plan["menu"], True)
check("  and ones you've never searched", "Car Dealership" in plan["menu"], True)

r = W.trades_hide(Req(), W.HideTradeBody(trade="Tattoo Shop", undo=True))
check("restore brings a trade back", [h.lower() for h in r["hidden"]], ["dentist"])
check("  to the picker", "Tattoo Shop" in opts(), True)

# "not now" on the out-of-leads card
W.refill_state(ws.PRIMARY).update({"need_leads": True, "finished": W.now()})
check("out of leads: the card asks", W.refill_status(Req())["need_leads"], True)
W.find_more_dismiss(Req())
s = W.refill_status(Req())
check("'Not now' puts it away", (s["need_leads"], s.get("dismissed")), (False, True))
import time; time.sleep(1.1)
W.refill_state(ws.PRIMARY).update({"need_leads": True, "finished": W.now()})
check("  until the next time it runs out", W.refill_status(Req())["need_leads"], True)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
