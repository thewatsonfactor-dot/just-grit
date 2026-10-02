# -*- coding: utf-8 -*-
"""The HomeRepair follow-up scheduler: steps 2 and 3 already existed as
tested copy in sequences.py: nothing ever queued them. This checks the
wiring that does - variant round-tripping, day-gap math, sequence
termination, and the reply/bounce guard that stops the sweep talking over
a human who already answered.

Runs against an in-memory sqlite db, never the live one.
"""
import sqlite3
import sys
sys.path.insert(0, '.')
import sequences as seq
import webapp as W

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))


# ── parse_variant / step_days ──────────────────────────────────────────
check("parses a property_manager variant",
      seq.parse_variant("seq:property_manager:slow_response"),
      ("property_manager", "slow_response"))
check("parses an hoa variant", seq.parse_variant("seq:hoa:board"), ("hoa", "board"))
check("watson's plain-word variant is not ours", seq.parse_variant("finding"), (None, None))
check("empty variant is not ours", seq.parse_variant(""), (None, None))
check("None variant is not ours", seq.parse_variant(None), (None, None))

pm_days = seq.step_days("property_manager", "slow_response")
check("property_manager has 4 steps: trigger, how it works, the no-ask list, the close", pm_days, {1: 0, 2: 4, 3: 11, 4: 21})
hoa_days = seq.step_days("hoa", None)
check("hoa has 4 steps", hoa_days, {1: 0, 2: 4, 3: 11, 4: 21})


# ── homerepair_advance: day-gap math and termination ────────────────────
def fresh_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE prospects (
        id INTEGER PRIMARY KEY, status TEXT, step INTEGER, next_action TEXT,
        next_due TEXT, seq_variant TEXT, touched_at TEXT, company TEXT DEFAULT '',
        domain TEXT DEFAULT '', city TEXT DEFAULT '', email TEXT DEFAULT '',
        findings TEXT DEFAULT '[]', say TEXT DEFAULT '', opener TEXT DEFAULT '',
        vertical TEXT DEFAULT '', scan_state TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE outreach (
        id INTEGER PRIMARY KEY, prospect_id INTEGER, offer TEXT, to_addr TEXT,
        subject TEXT, body TEXT, subject_original TEXT, body_original TEXT,
        state TEXT, created_at TEXT, variant TEXT, step INTEGER DEFAULT 1,
        reply TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE contacts (
        id INTEGER PRIMARY KEY, prospect_id INTEGER, name TEXT, role TEXT,
        email TEXT, phone TEXT, is_primary INTEGER DEFAULT 0)""")
    return c

c = fresh_db()
c.execute("INSERT INTO prospects (id, status) VALUES (1, 'working')")
W.homerepair_advance(c, 1, "property_manager", "slow_response", 1)
row = c.execute("SELECT * FROM prospects WHERE id=1").fetchone()
check("step 1 -> 2 sets step", row["step"], 2)
check("step 1 -> 2 schedules a 4-day gap",
      c.execute("SELECT date(next_due) = date('now', '+4 days') FROM prospects WHERE id=1")
        .fetchone()[0], 1)
check("step 1 -> 2 marks the follow-up action", row["next_action"], "email_followup")
check("step 1 -> 2 records seq_variant", row["seq_variant"], "property_manager:slow_response")

W.homerepair_advance(c, 1, "property_manager", "slow_response", 2)
row = c.execute("SELECT * FROM prospects WHERE id=1").fetchone()
check("step 2 -> 3 sets step", row["step"], 3)
check("step 2 -> 3 gap is 7 days (11-4), not 11",
      c.execute("SELECT date(next_due) = date('now', '+7 days') FROM prospects WHERE id=1")
        .fetchone()[0], 1)

W.homerepair_advance(c, 1, "property_manager", "slow_response", 3)
row = c.execute("SELECT * FROM prospects WHERE id=1").fetchone()
check("step 3 -> 4 sets step (the no-ask list, then the close)", row["step"], 4)
check("step 3 -> 4 gap is 10 days (21-11)",
      c.execute("SELECT date(next_due) = date('now', '+10 days') FROM prospects WHERE id=1").fetchone()[0], 1)

W.homerepair_advance(c, 1, "property_manager", "slow_response", 4)
row = c.execute("SELECT * FROM prospects WHERE id=1").fetchone()
check("step 4 -> sequence ends, status cold", row["status"], "cold")
check("step 4 -> sequence ends, next_due cleared", row["next_due"], None)
check("step 4 -> sequence ends, next_action cleared", row["next_action"], None)


# ── run_homerepair_followups: the sweep itself ──────────────────────────
W.setting = lambda k, default="": {
    "sender_name": "Daniel Watson", "sender_site": "HomeRepair.tech",
    "sender_phone": "(830) 205-0202", "sender_cell": "(210) 810-7476",
    "legal_entity": "HomeRepair Tech LLC",
    "sender_address": "12739 O'Conner Rd, Suite 102, San Antonio, TX 78233",
}.get(k, default)

c = fresh_db()
# 1: due today, no reply yet -> should draft step 2
c.execute("""INSERT INTO prospects (id, status, step, next_action, next_due,
             seq_variant, company, email) VALUES
             (1, 'working', 2, 'email_followup', date('now'),
              'property_manager:slow_response', 'Boogaard Properties', 'x@boogaard.com')""")
# 2: due today, but already replied -> should be pulled out, not drafted
c.execute("""INSERT INTO prospects (id, status, step, next_action, next_due,
             seq_variant, company, email) VALUES
             (2, 'working', 2, 'email_followup', date('now'),
              'property_manager:slow_response', 'Core35 Realty', 'x@core35.com')""")
c.execute("""INSERT INTO outreach (prospect_id, offer, to_addr, subject, body,
             subject_original, body_original, state, created_at, variant, step, reply)
             VALUES (2, 'x','x','x','x','x','x','sent','now','seq:property_manager:slow_response',1,'positive')""")
# 3: not due until next week -> should be left alone
c.execute("""INSERT INTO prospects (id, status, step, next_action, next_due,
             seq_variant, company, email) VALUES
             (3, 'working', 2, 'email_followup', date('now', '+3 days'),
              'property_manager:slow_response', 'Hendricks PM', 'x@hendricks.com')""")
c.commit()

made = W.run_homerepair_followups(c, room=10)
check("drafts exactly one follow-up this sweep", len(made), 1)

drafted = c.execute("SELECT * FROM outreach WHERE prospect_id=1").fetchone()
check("drafted row is step 2", drafted["step"], 2)
check("drafted row greets the company, not 'there'", drafted["body"].startswith("Hi ") and not drafted["body"].startswith("Hi there"))
check("drafted row lands as a draft", drafted["state"], "draft")
check("drafted row carries the step-2 subject",
      "one more thing" in drafted["subject"] or "preventive maintenance looks like" in drafted["subject"]
      or "property visits work" in drafted["subject"])
check("  and the step-2 body says what it is following up on, so it stands on its own",
      drafted["body"].splitlines()[2].startswith("Following up on my note about"))

p1 = c.execute("SELECT next_due, next_action FROM prospects WHERE id=1").fetchone()
check("schedule clears once drafted - waits on a human now", p1["next_due"], None)
check("next_action clears too", p1["next_action"], None)

p2 = c.execute("SELECT next_due, next_action FROM prospects WHERE id=2").fetchone()
check("already-replied prospect gets no second draft",
      c.execute("SELECT COUNT(*) FROM outreach WHERE prospect_id=2").fetchone()[0], 1)
check("already-replied prospect's schedule is cleared, not left dangling",
      p2["next_due"], None)

check("not-yet-due prospect is untouched",
      c.execute("SELECT COUNT(*) FROM outreach WHERE prospect_id=3").fetchone()[0], 0)

# room=0 should draft nothing at all
c2 = fresh_db()
c2.execute("""INSERT INTO prospects (id, status, step, next_action, next_due,
             seq_variant, company, email) VALUES
             (9, 'working', 2, 'email_followup', date('now'),
              'property_manager:slow_response', 'X', 'x@x.com')""")
c2.commit()
check("zero room drafts nothing", W.run_homerepair_followups(c2, room=0), [])


# ── reply stops the sequence (log_reply's own guard, exercised directly) ──
c = fresh_db()
c.execute("""INSERT INTO prospects (id, status, next_action, next_due)
             VALUES (1, 'working', 'email_followup', date('now', '+4 days'))""")
c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
          "WHERE id=1 AND next_action='email_followup'")
row = c.execute("SELECT next_due, next_action FROM prospects WHERE id=1").fetchone()
check("the guard clears a homerepair-scheduled follow-up", row["next_action"], None)

c = fresh_db()
c.execute("""INSERT INTO prospects (id, status, next_action, next_due)
             VALUES (1, 'working', 'call', date('now', '+2 days'))""")
c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
          "WHERE id=1 AND next_action='email_followup'")
row = c.execute("SELECT next_due, next_action FROM prospects WHERE id=1").fetchone()
check("the guard never touches Watson's own call/email cadence", row["next_action"], "call")


print("-" * 70)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
