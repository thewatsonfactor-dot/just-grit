# -*- coding: utf-8 -*-
"""Things the system does on its own when something happens.

Three events it already detected and then threw away: a reply, a bounce, a
lead going cold. Each one used to end with the prospect leaving the queue.

The sharpest of the three is the reply. A reply cleared `next_due`, and the
queue only lists rows where `next_due IS NOT NULL` - so answering the email
was what made a lead invisible. The test named "a reply does not delete the
lead" is the one that matters.

Temp data dir, no network, no mailbox, no clock.
"""
import json, os, pathlib, sqlite3, sys, tempfile
from datetime import date, timedelta
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-trig-")
os.environ["JUST_GRIT_DATA"] = TMP
import webapp as W
import workspaces as ws
import triggers as trig
W.DATA = pathlib.Path(TMP)
W.init_db()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

class Req:
    headers = {}
    cookies = {}
W.require_auth = lambda r: "test@example.com"

def fresh():
    c = W.db(); c.row_factory = sqlite3.Row
    return c

def mkprospect(c, company, email="", status="working", touched=None, step=1):
    c.execute("INSERT INTO prospects (company, email, status, vertical, step, "
              "touched_at, next_action, next_due) VALUES (?,?,?,'generic',?,?,'call',date('now'))",
              (company, email, status, step, touched or W.now()))
    return c.execute("SELECT last_insert_rowid()").fetchone()[0]

def mkcontact(c, pid, name, email):
    c.execute("INSERT INTO contacts (prospect_id, name, email, added_at) VALUES (?,?,?,?)",
              (pid, name, email, W.now()))

def mksend(c, pid, to_addr, reply=""):
    c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, "
              "sent_at, step, reply) VALUES (?,?,'s','b','sent',?,1,?)",
              (pid, to_addr, W.now(), reply))
    return c.execute("SELECT last_insert_rowid()").fetchone()[0]

# ── the pure decisions ──────────────────────────────────────────────────
check("a human reply earns a call back", trig.wants_callback("human"), True)
check("so does a flat no - it still proves the email was read",
      trig.wants_callback("negative"), True)
check("an out-of-office does not", trig.wants_callback("out_of_office"), False)
check("nor does an autoresponder", trig.wants_callback("auto_reply"), False)
check("nor does a bounce - that is a different trigger",
      trig.wants_callback("bounce"), False)

people = [{"name": "", "email": "info@x.com"}, {"name": "Maria", "email": "maria@x.com"}]
check("a named human still beats a shared inbox",
      trig.usable_addresses(people, "hello@x.com")[0], ("maria@x.com", "Maria"))
check("  a dead address drops out and the next one is used",
      trig.usable_addresses(people, "hello@x.com", {"maria@x.com"})[0],
      ("info@x.com", ""))
check("  when every contact is dead it falls back to the business address",
      trig.usable_addresses(people, "hello@x.com",
                            {"maria@x.com", "info@x.com"}), [("hello@x.com", "")])
check("  and with nothing left it is empty, not a dead address again",
      trig.usable_addresses(people, "hello@x.com",
                            {"maria@x.com", "info@x.com", "hello@x.com"}), [])
check("case does not let a dead address back in",
      trig.usable_addresses([{"name": "M", "email": "Maria@X.com"}], "", {"maria@x.com"}), [])
check("the same address twice is only offered once",
      len(trig.usable_addresses([{"name": "A", "email": "a@x.com"},
                                 {"name": "B", "email": "A@x.com"}], "")), 1)

old = (date.today() - timedelta(days=120)).isoformat()
recent = (date.today() - timedelta(days=10)).isoformat()
check("a lead cold for 120 days is due one more try",
      trig.due_for_reawaken("cold", old, "", 90), True)
check("  cold for 10 days is not", trig.due_for_reawaken("cold", recent, "", 90), False)
check("  a lead that already had its second chance never gets a third",
      trig.due_for_reawaken("cold", old, "2026-01-01", 90), False)
check("  a lead that is not cold is left alone",
      trig.due_for_reawaken("working", old, "", 90), False)
check("  a cold lead with no history is NOT reawakened",
      trig.due_for_reawaken("cold", "", "", 90), False)
check("  (re-approaching somebody never approached would open with a lie)",
      trig.due_for_reawaken("cold", None, "", 90), False)
check("an unreadable date is refused rather than guessed",
      trig.due_for_reawaken("cold", "sometime last year", "", 90), False)
check("a full timestamp reads the same as a bare date",
      trig.due_for_reawaken("cold", old + "T09:15:00+00:00", "", 90), True)

# ── trigger 1: a reply does not delete the lead ─────────────────────────
c = fresh()
pid = mkprospect(c, "Curley Chiropractic", "front@curley.example")
c.execute("UPDATE prospects SET next_due=date('now','+6 days') WHERE id=?", (pid,))
c.commit()

W.put_on_the_phone(c, pid, "Replied: Re: your after-hours line")
c.commit()
row = c.execute("SELECT status, next_action, next_due FROM prospects WHERE id=?",
                (pid,)).fetchone()
check("a reply puts them on today's call list", row["next_due"], date.today().isoformat())
check("  as a call back, not another email", row["next_action"], "call_back")
check("  and marks them as a conversation", row["status"], "conversation")
check("  next_due is NOT cleared - that is what used to hide them",
      row["next_due"] is not None, True)
q = W.queue(Req())
check("  so they actually appear in the queue",
      pid in [p["id"] for p in q["queue"]], True)
check("  at the very top of it", q["queue"][0]["id"], pid)
check("  and the history says why",
      c.execute("SELECT outcome FROM touches WHERE prospect_id=? ORDER BY id DESC",
                (pid,)).fetchone()["outcome"], "queued:call_back")

# the draft sweep must not talk over them
drafted = W.queue_drafts(Req(), W.DraftBody(limit=50))
check("  the machine stops writing to them",
      c.execute("SELECT COUNT(*) FROM outreach WHERE prospect_id=? AND state='draft'",
                (pid,)).fetchone()[0], 0)

booked = mkprospect(c, "Already Booked", "x@booked.example", status="booked")
c.commit()
check("a reply does not drag a booked customer back into the queue",
      W.put_on_the_phone(c, booked, "Replied"), False)

# ── the same reply seen twice does not undo Daniel's own scheduling ─────
# The UI always asks the scan for the last 60 days, so the message that
# earned the call back is still in the mailbox tomorrow. A human reply is
# deliberately left untapped (reply='') until somebody marks it in the
# Outbox - which meant the old "already has an outcome" guard never fired
# for it, and every morning's scan reset the prospect to today's call list
# and wrote another queued:call_back touch over whatever Daniel had logged.
c.close()
c = fresh()
twice = mkprospect(c, "Tuesday Tacos", "owner@tuesdaytacos.example")
mksend(c, twice, "owner@tuesdaytacos.example")
c.commit()
c.close()

_msg = {"from": "owner@tuesdaytacos.example", "subject": "Re: two-page check",
        "body": "Sure, call me Tuesday.", "headers": {}, "raw": "", "date": "2026-09-19"}
_orig_setting, _orig_fetch = W.setting, W.inbox.fetch
W.setting = lambda k, default="": {"imap_host": "mail.example", "imap_user": "me@ours.example",
                                   "imap_password": "x"}.get(k, _orig_setting(k, default))
W.inbox.fetch = lambda *a, **kw: [dict(_msg)]
try:
    r1 = W.inbox_scan(Req(), W.InboxScanBody(days=60))
    check("scan 1: the reply queues a call back", r1["call_backs_queued"], 1)
    check("  and asks for a tap", len(r1["needs_a_tap"]), 1)
    c = fresh()
    check("  Daniel then rings them and logs 'back in 5 days'",
          W.advance_queue(c, twice, "called", days=5), "back to you in 5 days")
    c.commit()
    c.close()
    r2 = W.inbox_scan(Req(), W.InboxScanBody(days=60))
    c = fresh()
    row = c.execute("SELECT status, next_action, next_due FROM prospects WHERE id=?",
                    (twice,)).fetchone()
    check("scan 2: the same message does not queue a second call back",
          r2["call_backs_queued"], 0)
    check("  the untapped reply is still reported as needing a tap",
          len(r2["needs_a_tap"]), 1)
    check("  Daniel's 5-day schedule survives", row["next_due"],
          (date.today() + timedelta(days=5)).isoformat())
    check("  as a plain call, not a call back", row["next_action"], "call")
    check("  and he stays 'working', not dragged back to 'conversation'",
          row["status"], "working")
    check("  exactly one queued:call_back in the history",
          c.execute("SELECT COUNT(*) FROM touches WHERE prospect_id=? AND outcome='queued:call_back'",
                    (twice,)).fetchone()[0], 1)
    check("  and the reply is still waiting for its tap (never scored by a machine)",
          c.execute("SELECT reply FROM outreach WHERE prospect_id=?",
                    (twice,)).fetchone()["reply"], "")
finally:
    W.setting, W.inbox.fetch = _orig_setting, _orig_fetch

# ── trigger 2: a bounce moves to the next person ────────────────────────
c = fresh()
p2 = mkprospect(c, "Stamps Chiropractic", "info@stamps.example")
mkcontact(c, p2, "", "info@stamps.example")
mkcontact(c, p2, "Dana Stamps", "dana@stamps.example")
c.commit()
check("before the bounce we write to the named owner",
      W.best_recipient(W.contact_rows(c, p2), {"email": "info@stamps.example"},
                       W.dead_addresses(c))[0], "dana@stamps.example")

moved = W.retarget_after_bounce(c, p2, "dana@stamps.example")
c.commit()
check("a bounce moves to the next address at that business", moved, "info@stamps.example")
check("  the dead address is recorded, not erased from the contact",
      c.execute("SELECT COUNT(*) FROM contacts WHERE prospect_id=? AND email=?",
                (p2, "dana@stamps.example")).fetchone()[0], 1)
check("  and it will never be chosen again",
      W.best_recipient(W.contact_rows(c, p2), {"email": "info@stamps.example"},
                       W.dead_addresses(c))[0], "info@stamps.example")
check("  the lead stays live", c.execute("SELECT status FROM prospects WHERE id=?",
                                         (p2,)).fetchone()["status"], "working")
check("  and the history names both addresses",
      "dana@stamps.example" in c.execute(
          "SELECT note FROM touches WHERE prospect_id=? ORDER BY id DESC",
          (p2,)).fetchone()["note"], True)

p3 = mkprospect(c, "One Address Only", "only@solo.example")
c.commit()
check("with nothing else to try it returns None rather than inventing one",
      W.retarget_after_bounce(c, p3, "only@solo.example"), None)
c.commit()
check("  and that prospect leaves the email queue",
      c.execute("SELECT next_due FROM prospects WHERE id=?", (p3,)).fetchone()["next_due"], None)
check("  with the reason on the card, not a mystery gap",
      c.execute("SELECT outcome FROM touches WHERE prospect_id=? ORDER BY id DESC",
                (p3,)).fetchone()["outcome"], "bounce:no_other_address")

# the whole point: a bounced business becomes draftable again, to someone else
c.execute("UPDATE outreach SET reply='bounce' WHERE prospect_id=?", (p2,))
mksend(c, p2, "dana@stamps.example", reply="bounce")
c.execute("UPDATE prospects SET status='working', next_due=date('now') WHERE id=?", (p2,))
c.commit()
W.set_setting(W.CATALOG_SETTING, "")
out = W.queue_drafts(Req(), W.DraftBody(limit=50))
row = c.execute("SELECT to_addr FROM outreach WHERE prospect_id=? AND state='draft' "
                "ORDER BY id DESC LIMIT 1", (p2,)).fetchone()
check("a business whose only send bounced can be written to again",
      row is not None, True)
check("  and the new draft goes to the OTHER person",
      row["to_addr"] if row else None, "info@stamps.example")

# ── trigger 3: one second chance, exactly one ───────────────────────────
c = fresh()
long_cold = mkprospect(c, "Long Cold Diner", "a@cold.example", status="cold", touched=old)
just_cold = mkprospect(c, "Recently Cold", "b@cold.example", status="cold", touched=recent)
never = mkprospect(c, "Never Worked", "c@cold.example", status="cold", touched="")
c.commit()

preview = W.reawaken(Req(), W.ReawakenBody(apply=False))
names = [p["company"] for p in preview["prospects"]]
check("the preview names who would come back", names, ["Long Cold Diner"])
check("  and changes nothing",
      c.execute("SELECT status FROM prospects WHERE id=?", (long_cold,)).fetchone()["status"],
      "cold")

done = W.reawaken(Req(), W.ReawakenBody(apply=True))
check("applying wakes only the one that is due", done["woke"], 1)
r = c.execute("SELECT status, next_action, next_due, reawakened_at FROM prospects "
              "WHERE id=?", (long_cold,)).fetchone()
check("  it comes back as a CALL, not another email", r["next_action"], "call")
check("  due today", r["next_due"], date.today().isoformat())
check("  and is marked so it cannot happen twice", bool(r["reawakened_at"]), True)
check("the recently-cold one is untouched",
      c.execute("SELECT status FROM prospects WHERE id=?", (just_cold,)).fetchone()["status"],
      "cold")
check("the never-worked one is untouched",
      c.execute("SELECT status FROM prospects WHERE id=?", (never,)).fetchone()["status"],
      "cold")

c.execute("UPDATE prospects SET status='cold', touched_at=? WHERE id=?", (old, long_cold))
c.commit()
again = W.reawaken(Req(), W.ReawakenBody(apply=True))
check("going cold a second time does NOT wake them a second time", again["woke"], 0)
check("  and the preview says so out loud",
      W.reawaken(Req(), W.ReawakenBody(apply=False))["already_had_their_second_chance"], 1)

# ── the cadence is per-workspace and travels in a snapshot ──────────────
import snapshots as S
check("the default second-chance wait is 90 days", W.reawaken_days(), 90)
W.set_setting(S.SETTING_REAWAKEN, "120")
check("  a workspace can set its own", W.reawaken_days(), 120)
W.set_setting(S.SETTING_REAWAKEN, "3")
check("  three days is refused - that is the same campaign with a pause in it",
      W.reawaken_days(), 90)
check("  and it rides along in a snapshot",
      S.build(name="x", catalog={"offers": {"O": {"tail":"t","cost":"c","fix":"f"}}},
              reawaken=150)[S.SETTING_REAWAKEN], 150)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
