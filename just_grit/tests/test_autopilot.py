# -*- coding: utf-8 -*-
"""Autopilot: the app sends outreach itself, with the switch in Daniel's hand.

"I would like an option on the follow-up emails to be done automatically
instead of me having to go through and approve each one - but I want the
option to turn on and off." "Probably should have that option for all the
email outreach." "It will need to update the CRM and mark as sent as well."

So: three modes (off / follow-ups / all); every auto-send goes through the
same record_sent() a tapped 'Mark sent' does; and it will not send without
a connected mailbox, outside working hours, past the daily cap, faster than
the hourly pace, or a draft the law would not allow.
"""
import os, pathlib, sys, tempfile
from datetime import datetime
TMP = tempfile.mkdtemp(prefix="jg-ap-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import mailer
import webapp as W, workspaces as ws
from contextlib import closing

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── the mailer, with nothing on the wire ────────────────────────────────
msg = mailer.build("daniel@thewatsonfactor.dev", "Daniel Watson", "info@blancocafe.com",
                   "Blanco Cafe - something in your reviews", "Hi there,\n\nA note.\n\nDaniel")
check("the message carries From with a name", msg["From"], "Daniel Watson <daniel@thewatsonfactor.dev>")
check("  a Date", bool(msg["Date"]), True)
check("  a Message-ID on the sender's domain", msg["Message-ID"].endswith("@thewatsonfactor.dev>"), True)
check("  and is plain text, one part - the shape that scored 10/10",
      (msg.get_content_type(), msg.is_multipart()), ("text/plain", False))
m2 = mailer.build("d@x.dev", "", "a@b.com", "Re: hi", "body", in_reply_to=msg["Message-ID"])
check("a follow-up threads under the previous email", m2["In-Reply-To"], msg["Message-ID"])
check("  and carries References", m2["References"], msg["Message-ID"])

class FakeSMTP:
    log = []
    def __init__(self, h, p): FakeSMTP.log.append(("connect", h, p)); self.tls = False
    def ehlo(self): pass
    def starttls(self): self.tls = True; FakeSMTP.log.append(("starttls",))
    def login(self, u, p): FakeSMTP.log.append(("login", u));
    def send_message(self, m): FakeSMTP.log.append(("send", m["To"])); return {}
    def quit(self): pass
mid = mailer.send("mail.privateemail.com", 587, "daniel@thewatsonfactor.dev", "pw", msg,
                  smtp_factory=FakeSMTP)
check("587 connects, upgrades to TLS, logs in, sends", [x[0] for x in FakeSMTP.log],
      ["connect", "starttls", "login", "send"])
check("  and returns the Message-ID", mid, msg["Message-ID"])

class NoTLS(FakeSMTP):
    def starttls(self):
        import smtplib; raise smtplib.SMTPNotSupportedError("no")
try:
    mailer.send("h", 587, "u", "pw", msg, smtp_factory=NoTLS); got = "sent"
except mailer.MailError as e:
    got = "clear" in str(e)
check("a server without encryption is refused - no password in the clear", got, True)

class BadLogin(FakeSMTP):
    def login(self, u, p):
        import smtplib; raise smtplib.SMTPAuthenticationError(535, b"no")
try:
    mailer.send("h", 587, "u", "pw", msg, smtp_factory=BadLogin); got = "sent"
except mailer.MailError as e:
    got = "Setup" in str(e)
check("a bad password says where to fix it", got, True)

check("Namecheap: SMTP host is the IMAP host", mailer.smtp_host_for("mail.privateemail.com"), "mail.privateemail.com")
check("  imap.example.com -> smtp.example.com", mailer.smtp_host_for("imap.example.com"), "smtp.example.com")

REAL_IS_OPEN = W.windows.is_open
W.windows.is_open = lambda dt, audience: True          # the sends below happen whenever the test runs
check("Tuesday 10am is inside the window", mailer.in_send_window(datetime(2026, 9, 22, 10, 0)), True)
check("Tuesday 7:59am is not", mailer.in_send_window(datetime(2026, 9, 22, 7, 59)), False)
check("Tuesday 5pm is not", mailer.in_send_window(datetime(2026, 9, 22, 17, 0)), False)
check("Saturday noon is not", mailer.in_send_window(datetime(2026, 9, 26, 12, 0)), False)

# ── the switch and the rails ────────────────────────────────────────────
W.DATA = pathlib.Path(TMP)
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("sender_name", "Daniel Watson"); W.set_setting("sender_email", "daniel@thewatsonfactor.dev")
W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130")
W.set_setting("daily_email_cap", "10")
W.scan_mailbox = lambda days=7, apply=True: {"scanned": 0}      # no network
W.inbox.list_folders = lambda h, u, p: ['(\\HasNoChildren \\Sent) "/" "Sent"']
class FakeIMAP:
    appended = []
    def __init__(self, h, p): pass
    def login(self, u, p): pass
    def append(self, folder, flags, date, raw): FakeIMAP.appended.append((folder, raw)); return ("OK", None)
    def logout(self): pass

st = W.autopilot_status(Req())
check("autopilot starts OFF", st["mode"], "")
W.set_setting("daily_email_cap", "999999")
check("a 999,999 cap does not lift autopilot's own ceiling", W.autopilot_status(Req())["cap"], W.AUTOPILOT_DAILY_MAX)
W.set_setting("daily_email_cap", "10")
check("  and lists its modes", st["modes"], ["", "followups", "all"])

# turn it on with no mailbox
st = W.autopilot_set(Req(), W.AutopilotBody(mode="followups"))
check("the switch flips to follow-ups", st["mode"], "followups")
check("  but with no mailbox it says it is paused, and why",
      any("mailbox" in b for b in st["blockers"]), True)
r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=FakeSMTP)
check("  and a pass sends nothing", (r["sent"], any("mailbox" in b for b in r["skipped"])), (0, True))

W.set_setting("imap_host", "mail.privateemail.com"); W.set_setting("imap_user", "daniel@thewatsonfactor.dev")
W.set_setting("imap_password", "secret")
check("mailbox connected -> that blocker clears",
      any("mailbox" in b for b in W.autopilot_status(Req())["blockers"]), False)

# a prospect with a first email sent 5 days ago -> a follow-up is owed
with closing(W.db()) as c:
    c.execute("INSERT INTO prospects (place_id, company, email, domain, status, stage, added_at) "
              "VALUES ('p1','Blanco Cafe','info@blancocafe.com','blancocafe.com','new','lead',?)", (W.now(),))
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, sent_at, variant, step) "
              "VALUES (?, 'Reviews', 'info@blancocafe.com', 'Blanco Cafe - something in your reviews', 'Hi there,\n\nfirst\n\nDaniel', "
              "'sent', datetime('now','-5 days'), datetime('now','-5 days'), 'finding', 1)", (pid,))
    # and a brand-new prospect nobody has written to
    c.execute("INSERT INTO prospects (place_id, company, email, domain, status, stage, added_at, offer_evidence) "
              "VALUES ('p2','Gristmill','info@gristmill.com','gristmill.com','new','lead',?, 'x')", (W.now(),))
    pid2 = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()

FakeSMTP.log.clear()
r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=FakeSMTP, imap_factory=FakeIMAP)
check("follow-ups mode drafts the owed follow-up and sends it", r["sent"], 1)
check("  through the mailbox", [x for x in FakeSMTP.log if x[0] == "send"], [("send", "info@blancocafe.com")])
check("  and nothing to the brand-new prospect - first emails wait for a tap",
      any("gristmill" in str(x) for x in FakeSMTP.log), False)
with closing(W.db()) as c:
    row = c.execute("SELECT * FROM outreach WHERE prospect_id=? AND step=2", (pid,)).fetchone()
    check("  the row is SENT", row["state"], "sent")
    check("  marked as autopilot", row["sent_via"], "autopilot")
    check("  with its Message-ID kept for threading the next one", row["message_id"].startswith("<"), True)
    check("  copy verified as-drafted (nothing could have rewritten it)", row["copy_check"], "as_drafted")
    t = c.execute("SELECT * FROM touches WHERE prospect_id=? AND kind='email' ORDER BY id DESC", (pid,)).fetchone()
    check("  the CRM has the touch - same as a tapped 'Mark sent'", (t["outcome"], t["note"].startswith("Sent: ")), ("sent", True))
    check("  a copy went to the Sent folder", FakeIMAP.appended[0][0], "Sent")
    check("  and the Sent folder name is remembered", W.setting("sent_folder", ""), "Sent")
    check("  the first email is untouched", c.execute("SELECT sent_via FROM outreach WHERE prospect_id=? AND step=1", (pid,)).fetchone()[0], "")
    check("  the new prospect has no draft in follow-ups mode",
          c.execute("SELECT COUNT(*) FROM outreach WHERE prospect_id=?", (pid2,)).fetchone()[0], 0)

st = W.autopilot_status(Req())
check("status counts today's autopilot sends", st["sent_today"], 1)

# a second pass sends nothing more (nothing owed)
r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=FakeSMTP, imap_factory=FakeIMAP)
check("a second pass has nothing to send", r["sent"], 0)

# ── 'all' mode: first emails too, with the offer catalog ────────────────
W.autopilot_set(Req(), W.AutopilotBody(mode="all"))
FakeSMTP.log.clear()
try:
    r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=FakeSMTP, imap_factory=FakeIMAP)
    check("all mode writes and sends the first email to the new prospect",
          any(x == ("send", "info@gristmill.com") for x in FakeSMTP.log), True)
    with closing(W.db()) as c:
        row = c.execute("SELECT * FROM outreach WHERE prospect_id=?", (pid2,)).fetchone()
        check("  recorded as sent by autopilot", (row["state"], row["sent_via"]), ("sent", "autopilot"))
        p = c.execute("SELECT * FROM prospects WHERE id=?", (pid2,)).fetchone()
        check("  and the queue moved on - next action is set", bool(p["next_action"]), True)
except Exception as e:
    check("all mode runs", "%s: %s" % (type(e).__name__, e), "ran")

# ── a draft with no legal footer never goes ─────────────────────────────
with closing(W.db()) as c:
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
              "VALUES (?, 'x', 'info@blancocafe.com', 'no footer', 'Hi\n\n[ADD YOUR MAILING ADDRESS IN SETTINGS - required by law]', 'draft', ?, 'finding', 1)", (pid, W.now()))
    c.commit()
    check("a draft still carrying the address placeholder is not sendable",
          any("no footer" in r["subject"] for r in W.autopilot_sendable(c, "all")), False)

# ── approved rows are NOT auto-sent (they may be open in Mail) ──────────
with closing(W.db()) as c:
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
              "VALUES (?, 'x', 'info@blancocafe.com', 'approved one', 'Hi\n\nbody\n\nDaniel\n3217 Wild Iris', 'approved', ?, 'finding', 1)", (pid, W.now()))
    c.commit()
    check("an 'approved' row - already handed to Mail - is never auto-sent",
          any(r["subject"] == "approved one" for r in W.autopilot_sendable(c, "all")), False)

# ── a refused send is recorded on the row, and the pass stops ───────────
class Refuse(FakeSMTP):
    def send_message(self, m): return {m["To"]: (550, b"no")}
with closing(W.db()) as c:
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
              "VALUES (?, 'x', 'info@blancocafe.com', 'will refuse', 'Hi\n\nbody\n\nDaniel\n3217 Wild Iris', 'draft', ?, 'finding', 1)", (pid, W.now()))
    c.commit()
    check("the 48-hour rule: Blanco Cafe was emailed minutes ago, so nothing else goes to them yet",
          (bool(W.recent_touch(c, pid)), any(r["subject"] == "will refuse" for r in W.autopilot_sendable(c, "all", now_only=True))), (True, False))
    c.execute("UPDATE outreach SET sent_at=datetime('now','-3 days') WHERE prospect_id=? AND state='sent'", (pid,)); c.commit()
    check("  three days on, it may", any(r["subject"] == "will refuse" for r in W.autopilot_sendable(c, "all", now_only=True)), True)
r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=Refuse, imap_factory=FakeIMAP)
check("a refusal sends nothing and reports the reason", (r["sent"], bool(r["errors"])), (0, True))
with closing(W.db()) as c:
    row = c.execute("SELECT state, send_error FROM outreach WHERE subject='will refuse'").fetchone()
    check("  the draft stays a draft with the error on it", (row["state"], "refused" in row["send_error"]), ("draft", True))
    check("  and is not retried by the next pass", any(r["subject"] == "will refuse" for r in W.autopilot_sendable(c, "all")), False)

# ── the clock ───────────────────────────────────────────────────────────
W.workspace_tz = lambda: __import__("zoneinfo").ZoneInfo("America/Chicago")
import datetime as _dt
real_now = W.datetime
class Sat(real_now):
    @classmethod
    def now(cls, tz=None):
        return real_now(2026, 9, 26, 12, 0, tzinfo=tz) if tz else real_now(2026, 9, 26, 12, 0)
W.datetime = Sat
W.windows.is_open = REAL_IS_OPEN
with closing(W.db()) as c:       # something waiting, so there is a reason to give
    c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
              "VALUES (?, 'x', 'info@gristmill.com', 'saturday one', 'Hi\n\nbody\n\nDaniel\n3217 Wild Iris', 'draft', ?, 'followup:x', 2)", (pid2, W.now()))
    c.commit()
r = W.autopilot_pass(ws.PRIMARY, force=False, smtp_factory=FakeSMTP, imap_factory=FakeIMAP)
check("on a Saturday the scheduled pass sends nothing and says why",
      (r["sent"], "sending hours" in r.get("waiting", ""), "Next window opens" in r.get("waiting", "")), (0, True, True))
W.datetime = real_now
W.windows.is_open = lambda dt, audience: True

# ── off means off ───────────────────────────────────────────────────────
W.autopilot_set(Req(), W.AutopilotBody(mode=""))
FakeSMTP.log.clear()
r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=FakeSMTP)
check("switched off, a pass does nothing at all", (r["mode"], r["sent"], FakeSMTP.log), ("", 0, []))
try:
    W.autopilot_set(Req(), W.AutopilotBody(mode="yolo")); got = "allowed"
except Exception as e:
    got = getattr(e, "status_code", None)
check("an unknown mode is refused", got, 400)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
