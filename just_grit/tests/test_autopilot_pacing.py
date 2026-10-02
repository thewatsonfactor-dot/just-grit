# -*- coding: utf-8 -*-
"""Autopilot spaces its sends and says when the next one goes.

Daniel, 2026-09-24 2:00 PM: "i just turned on autopilot seems to be stalled".
It wasn't stalled - it sent 8 emails between 1:45 and 1:47, hit its 8-an-hour
limit, and went quiet for an hour with the panel still saying "Running." A
burst like that also looks like a blast to Gmail. Now: one email about every
7-8 minutes, and the panel says when the next one goes and why.
"""
import os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-pace-")
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

W.DATA = pathlib.Path(TMP)
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
for k, v in dict(sender_name="Daniel", sender_email="d@thewatsonfactor.dev", sender_address="1 Main St, New Braunfels, TX 78130",
                 imap_host="mail.privateemail.com", imap_user="d@thewatsonfactor.dev", imap_password="pw",
                 daily_email_cap="999999", autopilot_mode="all", sent_folder="Sent").items():
    W.set_setting(k, v)
W.scan_mailbox = lambda days=7, apply=True: {"scanned": 0}
W.draft_batch = lambda c, **k: {"drafted": 0}
mailer.append_sent = lambda *a, **k: None
REAL_IS_OPEN = W.windows.is_open
W.windows.is_open = lambda dt, audience: True           # it's always a weekday afternoon here
SENT = []
class FakeSMTP:
    def __init__(self, h, p): pass
    def ehlo(self): pass
    def starttls(self): pass
    def login(self, u, p): pass
    def send_message(self, m): SENT.append(m["To"]); return {}
    def quit(self): pass

with closing(W.db()) as c:
    for i in range(12):
        c.execute("INSERT INTO prospects (place_id, company, email, status, stage, added_at) VALUES (?,?,?,?,?,?)",
                  ("p%d" % i, "Biz %d" % i, "b%d@x.com" % i, "new", "lead", W.now()))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, variant, step) "
                  "VALUES (?, 'Website', ?, 'hi', 'Hi there,\n\nbody\n\nDaniel', 'draft', ?, 'finding', 1)", (pid, "b%d@x.com" % i, W.now()))
    c.commit()

check("gap at 8 an hour: 7.5 minutes", W.autopilot_gap_minutes(), 7.5)
r = W.autopilot_pass(ws.PRIMARY, smtp_factory=FakeSMTP)
check("a pass sends ONE email, not a burst of three", r["sent"], 1)
r = W.autopilot_pass(ws.PRIMARY, smtp_factory=FakeSMTP)
check("the very next pass (90s later) waits", r["sent"], 0)
check("  and says when the next one goes and why",
      ("Next email goes out about" in r.get("waiting", ""), "minutes apart" in r.get("waiting", "")), (True, True))
st = W.autopilot_status(Req())
check("the panel knows the next send time", bool(st["next_send"]["at"]) and bool(st["next_send"]["local"]), True)
check("  and shows 'last sent' in local time, not UTC", ("T" in st["last_sent_local"], "M" in st["last_sent_local"]), (False, True))

# 8 minutes later, the next one goes
with closing(W.db()) as c:
    c.execute("UPDATE outreach SET sent_at=? WHERE state='sent'",
              ((datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat(timespec="seconds"),)); c.commit()
check("after the gap the next one goes", W.autopilot_pass(ws.PRIMARY, smtp_factory=FakeSMTP)["sent"], 1)

# a full hour's allowance: wait for the oldest to roll off
with closing(W.db()) as c:
    base = datetime.now(timezone.utc) - timedelta(minutes=50)
    ids = [r[0] for r in c.execute("SELECT id FROM outreach WHERE state='draft' ORDER BY id LIMIT 6")]
    for i, oid in enumerate(ids):
        c.execute("UPDATE outreach SET state='sent', sent_via='autopilot', sent_at=? WHERE id=?",
                  ((base + timedelta(minutes=i)).isoformat(timespec="seconds"), oid))
    c.execute("UPDATE outreach SET sent_at=? WHERE state='sent' AND id NOT IN (%s)" % ",".join(map(str, ids)),
              ((base - timedelta(minutes=2)).isoformat(timespec="seconds"),))
    c.commit()
    nxt = W.autopilot_next_send(c)
check("8 out in the last hour: next one when the oldest is an hour old",
      (nxt["why"], abs((datetime.fromisoformat(nxt["at"]) - (base - timedelta(minutes=2) + timedelta(hours=1))).total_seconds()) < 5),
      ("this hour's 8 are out", True))
before = len(SENT)
r = W.autopilot_pass(ws.PRIMARY, force=True, smtp_factory=FakeSMTP)
check("'Send what's ready now' still respects the hour's allowance", (len(SENT) - before, bool(r.get("waiting"))), (0, True))

# after hours: next send is when that audience reads again. Watson Factor
# writes to shops and restaurants: mid-morning Tue-Fri, Monday after 1 -
# never Monday morning (the playbook: nobody reads it).
W.windows.is_open = REAL_IS_OPEN
with closing(W.db()) as c:
    c.execute("UPDATE outreach SET sent_at=? WHERE state='sent'", ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),)); c.commit()
    orig = W.datetime
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 23, 30, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2026, 9, 25, 23, 30)
    W.datetime = FakeDT
    nxt = W.autopilot_next_send(c)
    W.datetime = orig
got = datetime.fromisoformat(nxt["at"]).astimezone(W.workspace_tz())
check("Friday 6:30 PM: the next email is Monday 1:00 PM - shops don't read Monday morning",
      (got.strftime("%a %H:%M"), nxt["why"]), ("Mon 13:00", "outside sending hours for everyone waiting"))

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
