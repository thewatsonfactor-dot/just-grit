# -*- coding: utf-8 -*-
"""The tabs talk to each other.

"Once I've sent the second round of emails through the Outbox, does it
update the leads in Today? I feel like I'm seeing all the same leads over
and over again." They were: 37 of the 38 overdue EMAIL cards had already
had that email sent. Then: "all the tabs need to talk to each other in
some way so I'm not doing double the work."

Every check here is one tab changing a lead and another tab being told.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-tabs-")
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
W.suppress = lambda d: None
W.set_setting("sender_name", "Daniel"); W.set_setting("sender_email", "d@x.dev")
W.set_setting("sender_address", "1 Main St"); W.set_setting("daily_email_cap", "10")

def prospect(company, email, domain, **kw):
    with closing(W.db()) as c:
        cols = {"place_id": "pl:" + domain, "company": company, "email": email, "domain": domain,
                "status": "new", "stage": "lead", "added_at": W.now(), "offer_evidence": "x"}
        cols.update(kw)
        c.execute("INSERT INTO prospects (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                  list(cols.values()))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    return pid

def one(sql, *a):
    with closing(W.db()) as c:
        r = c.execute(sql, a).fetchone()
        return dict(r) if r else None

def draft(pid, to, subject="s", step=1, variant="finding", state="draft"):
    with closing(W.db()) as c:
        c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (pid, "o", to, subject, "Hi\n\nbody\n\nDaniel\n1 Main St", state, W.now(), variant, step))
        oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    return oid

# ── 1. a follow-up sent from the Outbox moves Today's EMAIL step ────────
pid = prospect("Blanco Cafe", "info@blancocafe.com", "blancocafe.com",
               step=2, next_action="email", next_due="2026-09-01", status="working", touched_at="2026-09-10T00:00:00")
oid = draft(pid, "info@blancocafe.com", "Re: Blanco", step=2, variant="followup:2")
W.update_outreach(Req(), oid, W.OutreachBody(state="sent"))
p = one("SELECT * FROM prospects WHERE id=?", pid)
check("a sent follow-up satisfies Today's pending EMAIL step", p["next_action"], "call")
check("  and moves the lead to the next step with a fresh due date", (p["step"], p["next_due"] >= "2026-09-22"), (3, True))
check("  the Pipeline moved a fresh lead to Working", p["stage"], "working")
W.update_outreach(Req(), oid, W.OutreachBody(state="sent"))
check("tapping Mark sent twice does not advance the queue twice",
      (one("SELECT step FROM prospects WHERE id=?", pid)["step"],
       one("SELECT COUNT(*) AS n FROM touches WHERE prospect_id=? AND outcome='sent'", pid)["n"]), (3, 1))

# a follow-up sent while Today waits on a CALL leaves the call owed
pid_c = prospect("Gristmill", "info@gristmill.com", "gristmill.com", step=3, next_action="call",
                 next_due="2026-09-01", status="working")
oid = draft(pid_c, "info@gristmill.com", "Re: G", step=3, variant="followup:3")
W.update_outreach(Req(), oid, W.OutreachBody(state="sent"))
check("a follow-up does not cancel a call Today is waiting on",
      one("SELECT next_action, step FROM prospects WHERE id=?", pid_c), {"next_action": "call", "step": 3})

# the startup repair for the 37 stuck leads
pid_s = prospect("Stuck", "a@stuck.com", "stuck.com", step=2, next_action="email", next_due="2026-09-01",
                 status="working", touched_at="2026-09-10T00:00:00")
draft(pid_s, "a@stuck.com", "Re: S", step=2, variant="followup:2", state="sent")
with closing(W.db()) as c:
    c.execute("UPDATE outreach SET sent_at='2026-09-21T10:00:00' WHERE prospect_id=?", (pid_s,))
    n = W.reconcile_email_steps(c); c.commit()
check("the startup repair moves leads whose email step was already sent", n, 1)
check("  to a call", one("SELECT next_action FROM prospects WHERE id=?", pid_s)["next_action"], "call")
with closing(W.db()) as c:
    check("  and running it again does nothing", W.reconcile_email_steps(c), 0)

# ── 2. closing a lead retires its waiting drafts everywhere ─────────────
pid = prospect("Booked Co", "b@booked.com", "booked.com", next_action="call", next_due="2026-09-22")
oid = draft(pid, "b@booked.com")
W.log_touch(Req(), pid, W.TouchBody(outcome="booked"))
check("Booked on Today retires the Outbox draft", one("SELECT state FROM outreach WHERE id=?", oid)["state"], "skipped")
check("  and moves the Pipeline to Review booked", one("SELECT stage FROM prospects WHERE id=?", pid)["stage"], "review")

pid = prospect("Pass Co", "p@pass.com", "pass.com", next_action="call", next_due="2026-09-22")
oid = draft(pid, "p@pass.com")
with closing(W.db()) as c:
    c.execute("INSERT INTO deals (prospect_id, title, amount, state) VALUES (?,?,?,?)", (pid, "Site", 2500, "open"))
    c.commit()
W.log_touch(Req(), pid, W.TouchBody(outcome="dead"))
check("Pass on Today retires the draft", one("SELECT state FROM outreach WHERE id=?", oid)["state"], "skipped")
check("  marks the Pipeline lost", one("SELECT stage FROM prospects WHERE id=?", pid)["stage"], "lost")
check("  and closes the open deal", one("SELECT state FROM deals WHERE prospect_id=?", pid)["state"], "lost")

pid = prospect("Lost In Pipeline", "l@lost.com", "lost.com", next_action="call", next_due="2026-09-22")
oid = draft(pid, "l@lost.com")
W.set_stage(Req(), pid, W.StageBody(stage="lost"))
check("Lost in the Pipeline retires the draft", one("SELECT state FROM outreach WHERE id=?", oid)["state"], "skipped")

pid = prospect("Review Co", "r@review.com", "review.com", next_action="call", next_due="2026-09-22", status="working")
oid = draft(pid, "r@review.com")
W.set_stage(Req(), pid, W.StageBody(stage="review"))
p = one("SELECT * FROM prospects WHERE id=?", pid)
check("Review booked in the Pipeline stops the cold machine", (p["status"], one("SELECT state FROM outreach WHERE id=?", oid)["state"]), ("conversation", "skipped"))
check("  but keeps them on Today as a call", p["next_action"], "call")

# ── 3. a reply retires the draft and the machine goes quiet ─────────────
pid = prospect("Replier", "r@replier.com", "replier.com", next_action="email", next_due="2026-09-22", status="working")
sent = draft(pid, "r@replier.com", state="sent")
waiting = draft(pid, "r@replier.com", "Re: follow", step=2, variant="followup:2")
W.log_reply(Req(), sent, W.ReplyBody(kind="positive"))
check("a reply retires the follow-up that was waiting", one("SELECT state FROM outreach WHERE id=?", waiting)["state"], "skipped")
check("  and the lead is a conversation at the top of Today", one("SELECT status, next_action FROM prospects WHERE id=?", pid),
      {"status": "conversation", "next_action": W.CALLBACK_ACTION})
with closing(W.db()) as c:
    check("  autopilot would not send it either", any(r["id"] == waiting for r in W.autopilot_sendable(c, "all")), False)

# the reply tapped AFTER a call was already made does not re-queue
pid = prospect("Called Already", "c@called.com", "called.com", next_action="call", next_due="2026-09-22", status="working")
sent = draft(pid, "c@called.com", state="sent")
with closing(W.db()) as c:
    c.execute("UPDATE outreach SET reply_detected_at='2026-09-20T10:00:00' WHERE id=?", (sent,))
    c.commit()
W.log_touch(Req(), pid, W.TouchBody(outcome="spoke", days=5))
before = one("SELECT next_due, next_action FROM prospects WHERE id=?", pid)
W.log_reply(Req(), sent, W.ReplyBody(kind="positive"))
check("tapping the reply label after you already called does not drag them back to today",
      one("SELECT next_due, next_action FROM prospects WHERE id=?", pid), before)

# ── 4. skip is remembered ───────────────────────────────────────────────
pid = prospect("Skipper", "s@skipper.com", "skipper.com", next_action="call", next_due="2026-09-22")
oid = draft(pid, "s@skipper.com")
W.update_outreach(Req(), oid, W.OutreachBody(state="skipped", note="not this one"))
with closing(W.db()) as c:
    W.draft_batch(c); c.commit()
check("a skipped first email is not written again on the next pass",
      one("SELECT COUNT(*) AS n FROM outreach WHERE prospect_id=?", pid)["n"], 1)

# ── 5. Prospects tab buttons go through the queue ───────────────────────
pid = prospect("From Prospects", "f@fp.com", "fp.com", next_action="call", next_due="2026-09-22")
oid = draft(pid, "f@fp.com")
W.set_status(Req(), pid, W.StatusBody(status="called"))
p = one("SELECT * FROM prospects WHERE id=?", pid)
check("'Called' on the Prospects tab logs a touch", one("SELECT outcome FROM touches WHERE prospect_id=? ORDER BY id DESC", pid)["outcome"], "no_answer")
check("  and moves the queue, so Today doesn't ask for the same call", (p["step"], p["next_action"]), (2, "email"))
W.set_status(Req(), pid, W.StatusBody(status="booked"))
check("'Booked' there retires the draft and updates the Pipeline",
      (one("SELECT state FROM outreach WHERE id=?", oid)["state"], one("SELECT stage, status FROM prospects WHERE id=?", pid)),
      ("skipped", {"stage": "review", "status": "booked"}))

# ── 6. 'Emailed' on Today marks the Outbox draft as the email that went ──
pid = prospect("Hand Emailed", "h@hand.com", "hand.com", next_action="email", next_due="2026-09-22", status="working", step=2)
oid = draft(pid, "h@hand.com")
W.log_touch(Req(), pid, W.TouchBody(outcome="emailed"))
o = one("SELECT state, sent_via FROM outreach WHERE id=?", oid)
check("'Emailed' on Today marks the waiting draft sent, not left to go out again", (o["state"], o["sent_via"]), ("sent", "mail"))
check("  and Today moved on", one("SELECT next_action FROM prospects WHERE id=?", pid)["next_action"], "call")

# ── 7. a conversation stays a conversation ──────────────────────────────
pid = prospect("Caller", "", "", status="inbound", next_action="call", next_due="2026-09-22", place_id="phone:+12105550000", phone="+12105550000")
W.log_touch(Req(), pid, W.TouchBody(outcome="spoke"))
p = one("SELECT * FROM prospects WHERE id=?", pid)
check("logging a call on someone who called US keeps them inbound, not on the cold ladder",
      (p["status"], p["next_action"]), ("inbound", "call"))
check("  with another call scheduled", p["next_due"] > "2026-09-22", True)

# ── 8. the Today card knows what the Outbox holds ───────────────────────
pid = prospect("Held", "h@held.com", "held.com", next_action="email", next_due="2026-09-22", status="working", step=2)
oid = draft(pid, "h@held.com", step=2, variant="followup:2")
q = W.queue(Req())
row = next(r for r in q["queue"] if r["id"] == pid)
check("Today's EMAIL card carries the Outbox draft", (row["outbox_draft"] or {}).get("id"), oid)

# ── 9. no duplicates from adding a business already on file ─────────────
pid = prospect("Dup Cafe", "d@dupcafe.com", "dupcafe.com", phone="(210) 555-1234")
W.add_manual(Req(), W.AddBody(text="Dup Cafe, dupcafe.com"))
check("adding a domain already on file makes no second card",
      one("SELECT COUNT(*) AS n FROM prospects WHERE domain='dupcafe.com'")["n"], 1)
W.add_manual(Req(), W.AddBody(text="Dup Cafe Again, othersite.com, 210-555-1234"))
check("  nor does the same phone under a different site",
      one("SELECT COUNT(*) AS n FROM prospects WHERE company LIKE 'Dup Cafe%'")["n"], 1)

# ── 10. a scouted lead is due today, not after a restart ────────────────
W.queue_scans = lambda ids: None
W.insert_places([{"id": "gp1", "displayName": {"text": "Scouted Grill"}, "websiteUri": "https://scouted.com",
                  "nationalPhoneNumber": "(830) 555-0101", "businessStatus": "OPERATIONAL"}], "New Braunfels", "restaurant")
p = one("SELECT * FROM prospects WHERE domain='scouted.com'")
check("a scouted business lands on Today at once", (p["next_action"], p["next_due"] is not None, p["step"]), ("call", True, 1))
W.insert_places([{"id": "gp2-different-id", "displayName": {"text": "Scouted Grill"}, "websiteUri": "https://scouted.com",
                  "businessStatus": "OPERATIONAL"}], "New Braunfels", "restaurant")
check("  and finding it again under another Google id makes no twin",
      one("SELECT COUNT(*) AS n FROM prospects WHERE domain='scouted.com'")["n"], 1)

# ── 11. the receptionist's callers show up on Today ─────────────────────
W.set_setting("telnyx_api_key", "K")
try:
    r = W.assistant_webhook  # name check only
except AttributeError:
    r = None
with closing(W.db()) as c:
    pid = W.inbound_lead(c, "+12105559876", name="Maria", kind="ai_receptionist", outcome="hot", note="wants a call", callback=True, hot=True)
    c.commit()
p = one("SELECT * FROM prospects WHERE id=?", pid)
check("a hot receptionist call is at the top of Today", (p["next_action"], p["next_due"] is not None), (W.CALLBACK_ACTION, True))

# a passed lead who calls back comes back
pid = prospect("Passed Then Called", "", "", status="dead", place_id="phone:+12105551111", phone="+12105551111")
with closing(W.db()) as c:
    W.inbound_lead(c, "+12105551111", kind="sms", outcome="reply", note="yes please", hot=True); c.commit()
check("a lead you passed on who then writes back is un-passed and on Today",
      one("SELECT status, next_action FROM prospects WHERE id=?", pid), {"status": "conversation", "next_action": W.CALLBACK_ACTION})

# ── 12. "Stop" from the owner's own address, not the one we wrote to ─────
# Pat Rivera at Blue Door Bistro replied "Stop" from his own mailbox to
# an email sent to bluedoorbistro@gmail.com. Address-only matching would
# have called that 'unmatched' and kept following up.
pid = prospect("Blue Door Bistro", "bluedoorbistro@gmail.com", "bluedoorbistro.com",
               next_action="call", next_due="2026-09-22", status="working")
first = draft(pid, "bluedoorbistro@gmail.com", "Blue Door Bistro - something in your reviews", state="sent")
waiting = draft(pid, "bluedoorbistro@gmail.com", "Re: Blue Door Bistro - something in your reviews", step=3, variant="followup:3")
W.set_setting("imap_host", "h"); W.set_setting("imap_user", "d@x.dev"); W.set_setting("imap_password", "p")
import inbox as I
STOP = {"from": "pat@rivera-hospitality.example", "subject": "Re: Blue Door Bistro - something in your reviews",
        "body": "Stop\n\n> On September 11 I wrote:\n> Hi there,", "headers": {"From": "Pat Rivera <pat@rivera-hospitality.example>"},
        "raw": b"", "date": "2026-09-22T11:52:00", "to": "d@x.dev"}
W.inbox.fetch = lambda host, user, pw, since_days=30: [STOP]
W.inbox.fetch_sent = lambda host, user, pw, since_days=30: ("Sent", [])
r = W.scan_mailbox(days=7)
check("a 'Stop' from the owner's own address is matched to our email by subject", r["applied"], 1)
p = one("SELECT * FROM prospects WHERE id=?", pid)
check("  the lead is dead and off Today", (p["status"], p["next_due"]), ("dead", None))
check("  the Pipeline says lost", p["stage"], "lost")
check("  the waiting follow-up is retired", one("SELECT state FROM outreach WHERE id=?", waiting)["state"], "skipped")
check("  the scoreboard has the unsubscribe", one("SELECT reply FROM outreach WHERE id=?", first)["reply"], "unsubscribe")
with closing(W.db()) as c:
    W.draft_batch(c); c.commit()
    W.run_watson_followups(c, 10); c.commit()
check("  and nothing is ever drafted for them again",
      one("SELECT COUNT(*) AS n FROM outreach WHERE prospect_id=? AND state IN ('draft','approved')", pid)["n"], 0)
check("  they are not on Today", any(x["id"] == pid for x in W.queue(Req())["queue"]), False)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
