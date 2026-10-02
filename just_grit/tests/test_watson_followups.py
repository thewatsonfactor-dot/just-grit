# -*- coding: utf-8 -*-
"""Watson Factor's email follow-ups.

Forty-five cold emails, every one of them step 1, because the draft query
excluded anyone already written to. The playbook this system came from says
most replies come from touches 2 and 3. These are the rules that engine has
to obey - above all, that it never writes to someone who already answered.

In-memory db, no network.
"""
import pathlib, sqlite3, sys, tempfile
sys.path.insert(0, '.')
import webapp as W

W.DATA = pathlib.Path(tempfile.mkdtemp(prefix="jg-fu-"))
W.init_db()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

_open = []
def fresh():
    while _open:                      # one connection at a time, or sqlite locks
        try: _open.pop().close()
        except Exception: pass
    c = W.db(); c.row_factory = sqlite3.Row
    c.execute("DELETE FROM outreach"); c.execute("DELETE FROM prospects")
    c.execute("DELETE FROM contacts"); c.commit()
    _open.append(c)
    return c

def add_prospect(c, name="Blanco Cafe", email="owner@blanco.com", status="working"):
    cur = c.execute("INSERT INTO prospects (company, email, domain, status, vertical)"
                    " VALUES (?,?,?,?,'restaurant')", (name, email, name.lower().replace(" ","")+".com", status))
    return cur.lastrowid

def add_sent(c, pid, days_ago, step=1, subject="Blanco Cafe - something in your reviews", reply=""):
    c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, sent_at, step, reply)"
              " VALUES (?,?,?,?,'sent', datetime('now', ?), ?, ?)",
              (pid, "owner@blanco.com", subject, "body", "-%d days" % days_ago, step, reply))
    c.commit()

# ── the gap is respected ────────────────────────────────────────────────
c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=1)
check("1 day after the first email: too early, nothing drafted",
      len(W.run_watson_followups(c, 50)), 0)

c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=4)
made = W.run_watson_followups(c, 50)
check("4 days after: follow-up drafted", len(made), 1)
row = c.execute("SELECT * FROM outreach WHERE id=?", (made[0],)).fetchone()
check("  it's step 2", row["step"], 2)
check("  it threads with Re: on the original subject",
      row["subject"], "Re: Blanco Cafe - something in your reviews")
check("  it does NOT re-pitch the SAME product (the first was the camera pitch)",
      "camera" not in row["body"].split("I wrote:")[0].lower())
check("  it offers a different one", row["offer"] not in ("", "AI Vision"))
check("  it keeps the legal footer", "Reply \"stop\"" in row["body"])
check("  it waits as a draft, never auto-sends", row["state"], "draft")

# ── the rule that matters most: never talk over a human ────────────────
c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=9, reply="positive")
check("somebody who replied is never followed up", len(W.run_watson_followups(c, 50)), 0)

c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=9, reply="bounce")
check("a bounced address is never followed up", len(W.run_watson_followups(c, 50)), 0)

c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=9, reply="unsubscribe")
check("an unsubscribe is never followed up", len(W.run_watson_followups(c, 50)), 0)

# ── a bounce is the end of an ADDRESS, not of the sequence ──────────────
# Step 1 to dana@ bounced; retarget_after_bounce moved the lead to info@ and
# Daniel sent step 1 there. The dana@ row keeps reply='bounce' forever - it
# is the record of who we tried - and that one row used to block every
# follow-up to info@, so the retargeted address got exactly one email and
# the sequence silently stopped. Only the LAST send bouncing ends it.
c = fresh(); pid = add_prospect(c, email="info@blanco.com")
c.execute("DELETE FROM dead_addresses")
c.execute("INSERT INTO contacts (prospect_id, name, email) VALUES (?,?,?)",
          (pid, "Dana", "dana@blanco.com"))
c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, sent_at, step, reply)"
          " VALUES (?,?,?,?,'sent', datetime('now','-12 days'), 1, 'bounce')",
          (pid, "dana@blanco.com", "Blanco Cafe - something in your reviews", "body"))
c.commit()
check("retarget moves the lead to the shared inbox",
      W.retarget_after_bounce(c, pid, "dana@blanco.com"), "info@blanco.com")
c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, sent_at, step, reply)"
          " VALUES (?,?,?,?,'sent', datetime('now','-6 days'), 1, '')",
          (pid, "info@blanco.com", "Blanco Cafe - something in your reviews", "body"))
c.commit()
made = W.run_watson_followups(c, 50)
check("an old bounce to A does not block step 2 to the newer clean send B", len(made), 1)
if made:
    row = c.execute("SELECT * FROM outreach WHERE id=?", (made[0],)).fetchone()
    check("  the follow-up goes to the replacement address, not the dead one",
          row["to_addr"], "info@blanco.com")
    check("  and it is step 2", row["step"], 2)
c.execute("DELETE FROM dead_addresses"); c.commit()

# a reply the inbox scan found but nobody has classified yet still counts
c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=9)
c.execute("UPDATE outreach SET reply_text='sure, send it over' WHERE prospect_id=?", (pid,)); c.commit()
check("an unclassified reply found by the inbox scan also stops it",
      len(W.run_watson_followups(c, 50)), 0)

# ── it stops after two follow-ups ──────────────────────────────────────
c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=20, step=3)
check("after the third touch the sequence is over", len(W.run_watson_followups(c, 50)), 0)

c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=20, step=2)
made = W.run_watson_followups(c, 50)
check("step 2 -> step 3 is drafted", len(made), 1)
row = c.execute("SELECT * FROM outreach WHERE id=?", (made[0],)).fetchone()
check("  and the last one honestly says it's the last",
      "Last note from me" in row["body"])

# ── never double up, never chase the closed out ────────────────────────
c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=9)
c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state)"
          " VALUES (?,?,?,?,'draft')", (pid, "owner@blanco.com", "x", "y")); c.commit()
check("someone already holding a draft is skipped", len(W.run_watson_followups(c, 50)), 0)

for st in ("booked", "dead", "cold", "conversation"):
    c = fresh(); pid = add_prospect(c, status=st); add_sent(c, pid, days_ago=9)
    check("status=%s is left alone" % st, len(W.run_watson_followups(c, 50)), 0)

# ── the daily cap still governs ────────────────────────────────────────
c = fresh()
for i in range(5):
    p = add_prospect(c, name="Cafe %d" % i, email="o%d@x.com" % i)
    add_sent(c, p, days_ago=9)
check("room=2 drafts exactly 2", len(W.run_watson_followups(c, 2)), 2)
check("room=0 drafts nothing", len(W.run_watson_followups(c, 0)), 0)

# ── a follow-up must NOT advance the call cadence ───────────────────────
# parse_variant() returns (None, None) for "followup:N", so before this was
# handled every follow-up marked sent fell through to advance_queue() and
# pushed the prospect a step down the CALL sequence - two follow-ups took
# someone from call-step 1 to step 4 with nobody having phoned them, and a
# couple more marked them cold and dropped them out of the queue.
c = fresh(); pid = add_prospect(c); add_sent(c, pid, days_ago=4)
c.execute("UPDATE prospects SET step=1, next_action='call' WHERE id=?", (pid,)); c.commit()
made = W.run_watson_followups(c, 50)
oid = made[0]
import json
class _Body:
    state = "sent"; subject = None; body = None; note = ""
before = c.execute("SELECT step, status FROM prospects WHERE id=?", (pid,)).fetchone()
# simulate what update_outreach does for a followup-variant row
row = c.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
is_followup = (row["variant"] or "").startswith("followup:")
check("a follow-up row is recognised as its own channel", is_followup)
if not is_followup:
    W.advance_queue(c, pid, "sent", None)
after = c.execute("SELECT step, status FROM prospects WHERE id=?", (pid,)).fetchone()
check("the call step is untouched by a follow-up", after["step"], before["step"])
check("and the prospect is not pushed toward cold", after["status"], before["status"])

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
