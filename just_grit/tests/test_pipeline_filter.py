# -*- coding: utf-8 -*-
"""Pipeline: searchable by trade, and a trade menu with counts.

Daniel: "this list should be collapsible and searchable by trade". The
folding is in the page; this pins what the page asks the server for.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-pipe-")
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
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
with closing(W.db()) as c:
    rows = [("The Joint Chiropractic", "Chiropractor", "chiro", "lead"), ("Huebner Chiropractic", "Chiropractor", "chiro", "lead"),
            ("Kirby Vet", "Veterinary", "appointment", "lead"), ("S&S Tire & Auto", "Tire / Auto Repair", "auto", "working"),
            ("Taco Haven", "Taqueria", "restaurant", "lead")]
    for i, (n, cat, v, stage) in enumerate(rows):
        c.execute("INSERT INTO prospects (place_id, company, category, vertical, stage, status, city, added_at) VALUES (?,?,?,?,?,?,?,?)",
                  ("p%d" % i, n, cat, v, stage, "new", "San Antonio, TX", W.now()))
    c.commit()

d = W.crm(Req())
check("everything by default", d["totals"]["businesses"], 5)
check("the trade menu lists every trade with a count, biggest first", d["trades"][0], {"trade": "Chiropractor", "n": 2})
check("  and the broad groups", {g["trade"] for g in d["groups"]}, {"chiro", "appointment", "auto", "restaurant"})
d = W.crm(Req(), trade="Chiropractor")
check("pick a trade: only that trade", sorted(r["company"] for r in d["board"]["lead"]), ["Huebner Chiropractic", "The Joint Chiropractic"])
check("  the menu still shows every trade", len(d["trades"]), 4)
check("a broad group works too", W.crm(Req(), trade="auto")["totals"]["businesses"], 1)
check("case doesn't matter", W.crm(Req(), trade="veterinary")["totals"]["businesses"], 1)
check("typing a trade in the search box finds it", [r["company"] for r in W.crm(Req(), q="taqueria")["board"]["lead"]], ["Taco Haven"])
check("  names still search", W.crm(Req(), q="Kirby")["totals"]["businesses"], 1)
check("search + trade together", W.crm(Req(), q="Joint", trade="Chiropractor")["totals"]["businesses"], 1)
W.trades_hide(Req(), W.HideTradeBody(trade="Taqueria"))
d = W.crm(Req())
check("a hidden trade leaves the menu", any(t["trade"] == "Taqueria" for t in d["trades"]), False)
check("  but its businesses are still in the pipeline", d["totals"]["businesses"], 5)

# leads with no trade: filled in from the name where it's obvious
with closing(W.db()) as c:
    for i, (n, v) in enumerate([("San Antonio Family Chiropractic", "chiro"), ("Grampie's Pizzeria", "generic"),
                                ("Fast Aid Urgent Care", "generic"), ("Some Holdings LLC", "generic")]):
        c.execute("INSERT INTO prospects (place_id, company, vertical, stage, status, added_at) VALUES (?,?,?,?,?,?)",
                  ("n%d" % i, n, v, "lead", "new", W.now()))
    c.commit()
    check("before: 4 with no trade", W.crm(Req())["untraded"], 4)
    check("backfill fills the obvious ones", W.backfill_categories(c), 3); c.commit()
check("  chiropractors now show under Chiropractor", W.crm(Req(), trade="Chiropractor")["totals"]["businesses"], 3)
check("  the one it can't tell stays blank, findable as 'No trade set'",
      [r["company"] for r in W.crm(Req(), trade="__none__")["board"]["lead"]], ["Some Holdings LLC"])

# Prospects → "Emailed": everyone something actually went out to
with closing(W.db()) as c:
    kirby = c.execute("SELECT id FROM prospects WHERE company='Kirby Vet'").fetchone()[0]
    for when in ("2026-09-20T15:00:00+00:00", "2026-09-24T18:00:00+00:00"):
        c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, created_at, sent_at) VALUES (?,?,?,?,?,?,?)",
                  (kirby, "a@kirby.com", "s", "b", "sent", when, when))
    taco = c.execute("SELECT id FROM prospects WHERE company='Taco Haven'").fetchone()[0]
    c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, created_at) VALUES (?,?,?,?,?,?)",
              (taco, "a@taco.com", "s", "b", "draft", W.now()))
    c.commit()
d = W.list_prospects(Req(), status="emailed")
check("Emailed lists only businesses an email actually went to (not drafts)", [p["company"] for p in d["prospects"]], ["Kirby Vet"])
check("  with how many and when", (d["prospects"][0]["emails_sent"], d["prospects"][0]["last_emailed"][:10]), (2, "2026-09-24"))
check("  and the chip counts", (d["counts"]["emailed"], d["counts"]["all"]), (1, 9))

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
