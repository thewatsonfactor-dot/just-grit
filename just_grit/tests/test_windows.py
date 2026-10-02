# -*- coding: utf-8 -*-
"""Sending windows by audience, and the 48-hour rule.

From the Small Business plugin's outreach playbook, 2026-09-25: send when
they read. Offices Tue-Thu (never Monday morning, never Friday
afternoon); HOA boards in the evening; shops mid-morning; nobody on a
weekend. And never two messages to one person inside 48 hours, on any
channel. No network.
"""
import os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-windows-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import windows as WIN
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

D = lambda *a: datetime(*a)
check("offices: Tuesday 9am is open", WIN.is_open(D(2026, 9, 22, 9), "office"))
check("  Monday 9am is not (nobody reads Monday morning)", WIN.is_open(D(2026, 9, 21, 9), "office"), False)
check("  Monday 2pm is", WIN.is_open(D(2026, 9, 21, 14), "office"))
check("  Friday 3pm is not (nobody reads Friday afternoon)", WIN.is_open(D(2026, 9, 25, 15), "office"), False)
check("  Saturday is not", WIN.is_open(D(2026, 9, 26, 10), "office"), False)
check("HOA boards: Wednesday 6:30pm is open", WIN.is_open(D(2026, 9, 23, 18), "hoa"))
check("  Wednesday 2pm is not", WIN.is_open(D(2026, 9, 23, 14), "hoa"), False)
check("shops: Tuesday 10am is open, 2pm is not", (WIN.is_open(D(2026, 9, 22, 10), "retail"), WIN.is_open(D(2026, 9, 22, 14), "retail")), (True, False))
check("trades: 7am is open", WIN.is_open(D(2026, 9, 22, 7), "trades"))
check("Friday 6:30pm, offices: next open is Monday 1pm", WIN.next_open(D(2026, 9, 25, 18, 30), "office"), D(2026, 9, 28, 13))
check("Monday 9am, boards: next open is Monday 5pm", WIN.next_open(D(2026, 9, 21, 9), "hoa"), D(2026, 9, 21, 17))
check("an open window returns now", WIN.next_open(D(2026, 9, 22, 9, 15), "office"), D(2026, 9, 22, 9, 15))
check("categories map to audiences", [WIN.audience_for(c, "homerepair") for c in ("hoa_board", "property_manager", "commercial", "realtor", "medical", "")],
      ["hoa", "office", "office", "office", "office", "office"])
check("  Watson Factor's local businesses are the shop row", (WIN.audience_for("restaurant", "watson"), WIN.audience_for("", "watson"), WIN.audience_for("plumber", "watson")),
      ("retail", "retail", "trades"))
check("every audience has a description", all(WIN.describe(a) for a in WIN.WINDOWS))

# ── the autopilot honours them ──
W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
with W.closing(W.db()) as c:
    for i, (name, cat, phone) in enumerate([("Edwards PM", "property_manager", ""), ("Misty Oaks HOA", "hoa_board", ""),
                                             ("Core35 Realty", "realtor", "+12105550100")]):
        c.execute("INSERT INTO prospects (company, category, email, phone, status, stage, added_at) VALUES (?,?,?,?,'working','lead',?)",
                  (name, cat, "x%d@x.com" % i, phone, W.now()))
        c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
                  "VALUES (?, 'x', ?, 's', 'Hi\\n\\nbody\\n\\nDaniel', 'draft', ?, 'seq:x:y', 1)", (i + 1, "x%d@x.com" % i, W.now()))
    c.commit()

real_dt = W.datetime
def at(y, m, d, h, mi=0):
    class Fake(real_dt):
        @classmethod
        def now(cls, tz=None):
            local = real_dt(y, m, d, h, mi, tzinfo=W.workspace_tz())
            return local.astimezone(tz) if tz else local.replace(tzinfo=None)
    W.datetime = Fake

with W.closing(W.db()) as c:
    at(2026, 9, 22, 13)                     # Tuesday 1pm
    check("Tuesday 1pm: the office drafts go, the board's waits for the evening",
          sorted(r["to_addr"] for r in W.autopilot_sendable(c, "all", now_only=True)), ["x0@x.com", "x2@x.com"])
    at(2026, 9, 22, 18)                     # Tuesday 6pm
    check("Tuesday 6pm: only the board's", [r["to_addr"] for r in W.autopilot_sendable(c, "all", now_only=True)], ["x1@x.com"])
    at(2026, 9, 21, 9)                      # Monday 9am
    check("Monday 9am: nobody", W.autopilot_sendable(c, "all", now_only=True), [])
    check("  and the panel says when", W.autopilot_window_note(c, "all").startswith("Outside sending hours for everyone waiting"))
    check("  naming the audiences", "HOA boards" in W.autopilot_window_note(c, "all") and "offices" in W.autopilot_window_note(c, "all"))
    check("  all three still count as waiting", len(W.autopilot_sendable(c, "all")), 3)

    # the 48-hour rule, across channels
    at(2026, 9, 22, 13)
    c.execute("INSERT INTO sms_log (direction, number, text, at) VALUES ('out', '+12105550100', 'hi', ?)",
              ((W.datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),))
    c.commit()
    check("texted 5 hours ago: the realtor's email waits", [r["to_addr"] for r in W.autopilot_sendable(c, "all", now_only=True)], ["x0@x.com"])
    check("  and the Outbox says so", W.recent_touch(c, 3, "+12105550100"), "texted 5 hours ago")
    c.execute("UPDATE sms_log SET at=?", ((W.datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(),)); c.commit()
    check("  49 hours on, it goes", W.recent_touch(c, 3, "+12105550100"), "")
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, sent_at, variant, step) "
              "VALUES (1, 'x', 'x0@x.com', 's', 'b', 'sent', ?, ?, 'seq:x:y', 1)", (W.now(), (W.datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()))
    c.commit()
    check("emailed yesterday: the follow-up waits", W.recent_touch(c, 1), "emailed yesterday")
    c.execute("UPDATE sms_log SET at=?", ((W.datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),)); c.commit()
    check("  the window note explains the hold", W.autopilot_window_note(c, "all"), "Everyone whose window is open was reached in the last 48 hours - holding.")
W.datetime = real_dt
with W.closing(W.db()) as c:
    c.execute("UPDATE outreach SET sent_at=? WHERE state='sent'", ((real_dt.now(timezone.utc) - timedelta(hours=30)).isoformat(),)); c.commit()

class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
rows = W.list_outreach(Req())["outreach"]
check("the Outbox card carries the recent touch and the audience", (rows[0]["recent_touch"], rows[0]["audience"]), ("emailed yesterday", "office"))
ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
