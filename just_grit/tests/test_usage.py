# -*- coding: utf-8 -*-
"""Usage communications: does each customer get told the one useful thing,
in the right place, at most as often as promised - and do we find out
whether it worked?

The job, in the words it was asked for: "executing digital usage
communications across email automation and in-product communications."
So this checks both channels and the thing that ties them together - an
email only goes to someone who isn't seeing the card in the product.
"""
import os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-usage-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import usage as U
import mailer
import webapp as W, workspaces as ws
from contextlib import closing
from fastapi import HTTPException

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

UTC = timezone.utc
TUE = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)          # a Tuesday, 10:00 in Texas
iso = lambda d: d.isoformat(timespec="seconds")

# ═══════════════════════ the pure decisions ═══════════════════════════
check("an empty workspace has adopted nothing", U.adoption_score({}), 0)
full = {"scans_30": 5, "drafts_30": 5, "autopilot_mode": "all", "autopilot_sent_30": 3, "mailbox": True,
        "textback_on": True, "telnyx_ready": True, "images_30": 2, "posts_30": 1, "competitors": 3}
check("one that uses everything has adopted 100%", U.adoption_score(full), 100)
check("text-back switched on without a working line does not count as used",
      U.features_used({"textback_on": True, "telnyx_ready": False})["textback"], False)
check("AI features count double in the score",
      U.adoption_score({"scans_30": 1}) > U.adoption_score({"competitors": 1}), True)

check("stage: two days old, barely used -> new",
      U.stage({"age_days": 2, "last_active_at": iso(TUE)}, TUE), "new")
check("stage: nothing ever done -> dormant", U.stage({}, TUE), "dormant")
check("stage: quiet 12 days -> going quiet",
      U.stage({"last_active_at": iso(TUE - timedelta(days=12)), "scans_30": 9}, TUE), "at_risk")
check("stage: quiet 40 days -> dormant",
      U.stage({"last_active_at": iso(TUE - timedelta(days=40))}, TUE), "dormant")
power = dict(full, drafts_30=40, last_active_at=iso(TUE))
check("stage: broad and deep -> power user", U.stage(power, TUE), "power")
check("stage: otherwise -> adopting",
      U.stage({"last_active_at": iso(TUE), "drafts_30": 3}, TUE), "adopting")

# a customer who sends by hand, has no mailbox connected, and is missing calls
snap = {"last_active_at": iso(TUE - timedelta(hours=3)), "drafts_30": 14, "sent_30": 12,
        "manual_sent_30": 12, "calls_missed_7": 3, "textback_on": False, "mailbox": False,
        "scans_total": 20, "scans_30": 6, "competitors": 1, "drafts_waiting": 0}
p = U.plan_in_app(snap, [], TUE)
check("the card picked is the highest-priority thing that applies", p and p["id"], "connect_mailbox")
card = U.render_in_app(U.PLAY_BY_ID["missed_calls"], snap)
check("a card is built from their own numbers", "3 callers" in card["title"], True)

hist = [{"id": 1, "play": "connect_mailbox", "channel": "in_app", "state": "active",
         "created_at": iso(TUE - timedelta(hours=1))}]
check("an existing card is left alone - never two at once", U.plan_in_app(snap, hist, TUE), None)
hist[0]["created_at"] = iso(TUE - timedelta(days=U.IN_APP_TTL_DAYS + 1))
check("  unless it has outlived its two weeks - then it gets its one second try",
      (U.plan_in_app(snap, hist, TUE) or {}).get("id"), "connect_mailbox")
hist.append(dict(hist[0], id=2, created_at=iso(TUE - timedelta(days=8)), state="expired"))
check("  and after that second try, it's the next thing's turn",
      (U.plan_in_app(snap, hist, TUE) or {}).get("id"), "missed_calls")

dis = [{"id": 1, "play": "connect_mailbox", "channel": "in_app", "state": "dismissed",
        "created_at": iso(TUE - timedelta(hours=5)), "dismissed_at": iso(TUE - timedelta(hours=2))}]
check("'not now' on one card doesn't put the next one straight up", U.plan_in_app(snap, dis, TUE), None)
check("  a day later the next one may show",
      (U.plan_in_app(snap, dis, TUE + timedelta(days=1, hours=1)) or {}).get("id"), "missed_calls")
check("  and the dismissed one stays down for its week",
      U.eligible(U.PLAY_BY_ID["connect_mailbox"], snap, dis, TUE + timedelta(days=3))[1], "dismissed recently")
check("a card whose goal is already true is never raised",
      U.eligible(U.PLAY_BY_ID["connect_mailbox"], dict(snap, mailbox=True), [], TUE)[1], "already done")

# in-app first, email as the fallback
mb = U.PLAY_BY_ID["connect_mailbox"]
play, why = U.plan_email(snap, [], TUE)
check("no email before the card has had its chance", play, None)
check("  and the report says why", "in-app card goes first" in why, True)
up = [{"id": 1, "play": "connect_mailbox", "channel": "in_app", "state": "active",
       "created_at": iso(TUE - timedelta(days=1))}]
check("card up one day -> still no email", U.plan_email(snap, up, TUE)[0], None)
seen = [dict(up[0], created_at=iso(TUE - timedelta(days=5)), shown_at=iso(TUE - timedelta(days=4)))]
check("card seen in the app -> they don't also get an email",
      (U.plan_email(snap, seen, TUE)[0] or {}).get("id") != "connect_mailbox", True)
unseen = [dict(up[0], created_at=iso(TUE - timedelta(days=4)))]
check("card up 4 days and nobody saw it -> the email goes",
      (U.plan_email(snap, unseen, TUE)[0] or {}).get("id"), "connect_mailbox")

quiet = dict(snap, last_active_at=iso(TUE - timedelta(days=15)), due_today=7, drafts_waiting=4)
check("going quiet -> the win-back email goes first, no card needed",
      (U.plan_email(quiet, [], TUE)[0] or {}).get("id"), "win_back")
check("  and it's written from their pile",
      "7 leads" in U.render_email(U.PLAY_BY_ID["win_back"], quiet,
                                  {"business": "Blanco Cafe", "first_name": "Ana", "now": TUE,
                                   "link": "https://x", "sender": "Daniel Watson",
                                   "company": "The Watson Factor", "address": "1 Main St"})["body"], True)

sent1 = [{"id": 9, "play": "missed_calls", "channel": "email", "state": "sent",
          "created_at": iso(TUE - timedelta(days=1)), "sent_at": iso(TUE - timedelta(days=1))}]
check("never two product emails within 3 days", U.plan_email(quiet, sent1, TUE)[0], None)
two = [dict(sent1[0], id=8, sent_at=iso(TUE - timedelta(days=6)), created_at=iso(TUE - timedelta(days=6))),
       dict(sent1[0], id=9, sent_at=iso(TUE - timedelta(days=3, hours=1)), created_at=iso(TUE - timedelta(days=4)))]
check("never more than 2 in a week", "2 emails this week" in U.plan_email(quiet, two, TUE)[1], True)
maxed = [dict(sent1[0], play="win_back", sent_at=iso(TUE - timedelta(days=d)),
              created_at=iso(TUE - timedelta(days=d))) for d in (30, 60)]
check("a nudge stops after its lifetime cap - one reminder, not a carousel",
      (U.plan_email(quiet, maxed, TUE)[0] or {}).get("id") != "win_back", True)

recap = U.PLAY_BY_ID["weekly_recap"]
busy = {"drafts_7": 9, "autopilot_sent_7": 4, "replies_7": 1, "last_active_at": iso(TUE)}
check("the weekly recap only goes on a Monday", U.eligible(recap, busy, [], TUE)[1], "only on Mondays")
check("  and does on one", U.eligible(recap, busy, [], TUE - timedelta(days=1))[0], True)
lines = U.recap_lines(busy)
check("the recap lists what the AI did, singular where it's one",
      ("9 emails written" in lines, "1 reply caught" in lines, "images" in lines), (True, True, False))
check("a week with nothing done gets no recap", U.eligible(recap, {}, [], TUE - timedelta(days=1))[0], False)

em = U.render_email(mb, snap, {"business": "Blanco Cafe", "first_name": "Ana", "now": TUE,
                               "link": "https://grit.blancocafe.com", "sender": "Daniel Watson",
                               "company": "The Watson Factor", "address": "123 Main St, New Braunfels TX"})
check("every email carries the mailing address the law wants", "123 Main St" in em["body"], True)
check("  and says how to make it stop", 'Reply "stop"' in em["body"], True)
check("  and links into the right tab", "grit.blancocafe.com#setup" in em["body"], True)

# measuring it
h = [{"id": 1, "play": "connect_mailbox", "channel": "in_app", "state": "active",
      "created_at": iso(TUE - timedelta(days=2))},
     {"id": 2, "play": "connect_mailbox", "channel": "email", "state": "sent",
      "created_at": iso(TUE - timedelta(days=30))},
     {"id": 3, "play": "weekly_recap", "channel": "email", "state": "sent", "created_at": iso(TUE)}]
check("goal met -> the recent message is credited, the old one and the recap aren't",
      U.goals_met(h, dict(snap, mailbox=True), TUE), [1])
check("holdout 0% -> nobody is held back", any(U.in_holdout("w%d" % i, "x", 0) for i in range(200)), False)
check("holdout is the same answer every time for the same workspace",
      [U.in_holdout("blancocafe", "missed_calls", 20) for _ in range(5)].count(
          U.in_holdout("blancocafe", "missed_calls", 20)), 5)
share = sum(U.in_holdout("w%d" % i, "missed_calls", 20) for i in range(2000)) / 2000
check("  and roughly the share asked for (20%%: got %.0f%%)" % (share * 100), 0.15 < share < 0.25, True)
fn = U.funnel([{"play": "missed_calls", "channel": "in_app", "state": "active", "shown_at": "x",
                "clicked_at": "x", "converted_at": "x"},
               {"play": "missed_calls", "channel": "in_app", "state": "done", "shown_at": "x"},
               {"play": "missed_calls", "channel": "in_app", "state": "holdout", "converted_at": "x"}])
row = fn[0]
check("funnel counts delivered / seen / clicked / converted",
      (row["delivered"], row["seen"], row["clicked"], row["converted"]), (2, 2, 1, 1))
check("  conversion rate", row["conv_rate"], 0.5)
check("  holdout kept apart as the control", (row["holdout"], row["holdout_rate"]), (1, 1.0))
check("  and no lift is claimed on three rows", row["lift"], None)


# ═══════════════════════ in the app ══════════════════════════════════
W.DATA = pathlib.Path(TMP)
for _s in list(ws.WORKSPACES):
    _t = ws.CURRENT.set(_s)
    try:
        W.init_db()
    finally:
        ws.CURRENT.reset(_t)

OWNER, CUSTOMER = "daniel@thewatsonfactor.dev", "ana@blancocafe.com"

class Req:
    headers = {}; cookies = {}
    class client: host = "10.0.0.9"

def as_(email):
    W.caller_email = lambda r: email

def in_ws(slug, fn, *a, **kw):
    tok = ws.CURRENT.set(slug)
    try:
        return fn(*a, **kw)
    finally:
        ws.CURRENT.reset(tok)

as_(OWNER)
in_ws(ws.PRIMARY, W.set_setting, "allowed_emails", OWNER)
for k, v in {"imap_host": "mail.privateemail.com", "imap_user": OWNER, "imap_password": "pw",
             "sender_email": OWNER, "sender_name": "Daniel Watson", "sender_company": "The Watson Factor",
             "sender_address": "123 Main St, New Braunfels TX 78130"}.items():
    in_ws(ws.PRIMARY, W.set_setting, k, v)
W.create_workspace(Req(), W.NewWorkspaceBody(slug="blancocafe", name="Blanco Cafe", host="grit.blancocafe.com"))
in_ws("blancocafe", W.set_setting, "workspace_emails", CUSTOMER)
in_ws("blancocafe", W.set_setting, "sender_name", "Ana Ruiz")

NOW = datetime.now(timezone.utc)
with closing(W.db("blancocafe")) as c:
    c.execute("INSERT INTO prospects (company, domain, status, added_at) VALUES ('Taco Loco','tacoloco.com','new',?)",
              (iso(NOW - timedelta(days=2)),))
    for i in range(12):
        c.execute("INSERT INTO outreach (prospect_id, to_addr, subject, body, state, created_at, sent_at, sent_via) "
                  "VALUES (1,'a@b.com','s','b','sent',?,?,'')", (iso(NOW - timedelta(days=3)), iso(NOW - timedelta(days=3))))
    for i in range(2):
        c.execute("INSERT INTO inbound_calls (call_session_id, from_number, started_at) VALUES (?,?,?)",
                  ("cs%d" % i, "+12105550%03d" % (100 + i), iso(NOW - timedelta(days=1))))
    c.commit()

# it's all off until the owner switches it on
as_(CUSTOMER)
check("messages off -> the customer sees no card", in_ws("blancocafe", W.usage_nudge, Req())["nudge"], None)
check("  but opening the app is still recorded as last seen",
      bool(in_ws("blancocafe", W.setting, "usage_last_seen", "")), True)
try:
    in_ws("blancocafe", W.usage_overview, Req()); got = "allowed"
except HTTPException as e:
    got = e.status_code
check("a customer can't open the owner's Adoption report", got, 403)
try:
    in_ws("blancocafe", W.usage_settings, Req(), W.UsageSettings(mode="email")); got = "allowed"
except HTTPException as e:
    got = e.status_code
check("  or flip the switch", got, 403)

as_(OWNER)
W.usage_settings(Req(), W.UsageSettings(mode="in_app"))
check("the switch is account-level", W.usage_mode(), "in_app")
try:
    W.usage_settings(Req(), W.UsageSettings(mode="blast")); got = "allowed"
except HTTPException as e:
    got = e.status_code
check("  and refuses a mode that doesn't exist", got, 400)

as_(CUSTOMER)
n1 = in_ws("blancocafe", W.usage_nudge, Req())["nudge"]
check("switched on -> the customer gets one card, decided on the spot", n1 and n1["play"], "connect_mailbox")
n2 = in_ws("blancocafe", W.usage_nudge, Req())["nudge"]
check("  the same card on the next load, not a new one", n2["id"], n1["id"])
with closing(W.db("blancocafe")) as c:
    r = dict(c.execute("SELECT * FROM usage_messages WHERE id=?", (n1["id"],)).fetchone())
check("  it's recorded as seen", bool(r["shown_at"]), True)
check("  and stored with the adoption and stage it was shown at", (r["stage"] != "", r["adoption"] >= 0), (True, True))
with closing(W.db(ws.PRIMARY)) as c:
    W._usage_tables(c)
    leaked = c.execute("SELECT COUNT(*) FROM usage_messages WHERE play='connect_mailbox'").fetchone()[0]
check("the customer's messages live in the customer's file, not the owner's", leaked, 0)

in_ws("blancocafe", W.usage_nudge_act, Req(), n1["id"], W.NudgeAct(action="click"))
check("a click is recorded, and the card stays until the job is done",
      in_ws("blancocafe", W.usage_nudge, Req())["nudge"]["id"], n1["id"])
try:
    in_ws("blancocafe", W.usage_nudge_act, Req(), n1["id"], W.NudgeAct(action="delete")); got = "ok"
except HTTPException as e:
    got = e.status_code
check("  an unknown action is refused", got, 400)

# they do the thing
for k, v in {"imap_host": "mail.privateemail.com", "imap_user": CUSTOMER, "imap_password": "x"}.items():
    in_ws("blancocafe", W.set_setting, k, v)
res = in_ws("blancocafe", W.usage_pass, "blancocafe")
with closing(W.db("blancocafe")) as c:
    r = dict(c.execute("SELECT * FROM usage_messages WHERE id=?", (n1["id"],)).fetchone())
check("connecting the mailbox retires the card", r["state"], "done")
check("  and credits it as converted", bool(r["converted_at"]), True)
check("  and the next most useful card goes up - their missed calls", (res["card"] or {}).get("play"), "missed_calls")
check("  in their words", "2 callers hung up" in res["card"]["title"], True)

n3 = in_ws("blancocafe", W.usage_nudge, Req())["nudge"]
in_ws("blancocafe", W.usage_nudge_act, Req(), n3["id"], W.NudgeAct(action="dismiss"))
check("dismissed -> nothing replaces it today", in_ws("blancocafe", W.usage_nudge, Req())["nudge"], None)

# ═══════════════════════ by email ════════════════════════════════════

class FakeSMTP:
    sent = []
    fail = False
    def __init__(self, h, p, timeout=30): pass
    def ehlo(self): pass
    def starttls(self): pass
    def login(self, u, p): pass
    def send_message(self, m):
        if FakeSMTP.fail:
            import smtplib; raise smtplib.SMTPRecipientsRefused({m["To"]: (550, b"no")})
        FakeSMTP.sent.append((m["To"], m["Subject"], m.get_content())); return {}
    def quit(self): pass

as_(OWNER)
W.usage_settings(Req(), W.UsageSettings(mode="email"))

# a page load never sends mail, whatever the mode
as_(CUSTOMER)
before = len(FakeSMTP.sent)
in_ws("blancocafe", W.usage_nudge, Req())
check("a page load never sends an email", len(FakeSMTP.sent), before)

real_window = W.mailer.in_send_window
SAT = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
r = in_ws("blancocafe", W.usage_pass, "blancocafe", SAT, smtp_factory=FakeSMTP)
check("Saturday -> no email", (r["email"], r["email_reason"]), (None, "Outside sending hours."))
W.mailer.in_send_window = lambda dt: True

# a fresh quiet customer: the card for missed calls went up 4 days ago, unseen
with closing(W.db("blancocafe")) as c:
    c.execute("DELETE FROM usage_messages")
    c.execute("INSERT INTO usage_messages (play, channel, state, title, created_at) VALUES "
              "('missed_calls','in_app','active','x',?)", (iso(NOW - timedelta(days=4)),))
    c.commit()
r = in_ws("blancocafe", W.usage_pass, "blancocafe", smtp_factory=FakeSMTP)
check("card unseen for 4 days -> the email follows", (r["email"] or {}).get("play"), "missed_calls")
check("  to the customer's own login, from the owner's mailbox",
      FakeSMTP.sent[-1][0] if FakeSMTP.sent else None, CUSTOMER)
check("  subject from their numbers", FakeSMTP.sent[-1][1], "2 missed calls this week")
check("  with the address and the way out in the body",
      ("123 Main St" in FakeSMTP.sent[-1][2], 'Reply "stop"' in FakeSMTP.sent[-1][2]), (True, True))
with closing(W.db("blancocafe")) as c:
    er = dict(c.execute("SELECT * FROM usage_messages WHERE channel='email' ORDER BY id DESC LIMIT 1").fetchone())
check("  and recorded as sent with its Message-ID", (er["state"], bool(er["message_id"])), ("sent", True))

n_before = len(FakeSMTP.sent)
r = in_ws("blancocafe", W.usage_pass, "blancocafe", smtp_factory=FakeSMTP)
check("the next pass sends nothing - 3 days between emails", len(FakeSMTP.sent), n_before)
check("  and says so", "within the last 3 days" in r["email_reason"], True)

r = in_ws(ws.PRIMARY, W.usage_pass, ws.PRIMARY, smtp_factory=FakeSMTP)
check("your own workspaces never get product email - no customer logins",
      r["email_reason"], "No customer logins on this workspace to email.")

as_(OWNER)
W.usage_settings(Req(), W.UsageSettings(optout_slug="blancocafe", optout=True))
with closing(W.db("blancocafe")) as c:
    c.execute("DELETE FROM usage_messages WHERE channel='email'"); c.commit()
r = in_ws("blancocafe", W.usage_pass, "blancocafe", smtp_factory=FakeSMTP)
check("a customer who said stop gets nothing", r["email"], None)
W.usage_settings(Req(), W.UsageSettings(optout_slug="blancocafe", optout=False))

FakeSMTP.fail = True
r = in_ws("blancocafe", W.usage_pass, "blancocafe", smtp_factory=FakeSMTP)
with closing(W.db("blancocafe")) as c:
    er = dict(c.execute("SELECT * FROM usage_messages WHERE channel='email' ORDER BY id DESC LIMIT 1").fetchone())
check("a refused send is recorded as failed, with the reason", (er["state"], bool(er["error"])), ("error", True))
FakeSMTP.fail = False

in_ws(ws.PRIMARY, W._set_primary_setting, "sender_address", "")
r = in_ws("blancocafe", W.usage_pass, "blancocafe", smtp_factory=FakeSMTP)
check("no mailing address on file -> no email at all", "mailing address" in r["email_reason"], True)
in_ws(ws.PRIMARY, W._set_primary_setting, "sender_address", "123 Main St, New Braunfels TX 78130")
W.mailer.in_send_window = real_window

# ═══════════════════════ the owner's report ══════════════════════════
as_(OWNER)
ov = W.usage_overview(Req())
wsrow = next(w for w in ov["workspaces"] if w["slug"] == "blancocafe")
check("the report has every workspace", {w["slug"] for w in ov["workspaces"]} >= {"watson", "homerepair", "blancocafe"}, True)
check("  with stage, adoption and the features in use",
      (wsrow["stage"] in U.STAGES, isinstance(wsrow["adoption"], int), len(wsrow["features"])),
      (True, True, len(U.FEATURES)))
check("  a reason for every message that isn't going", all(p["reason"] for p in wsrow["plays"]), True)
fm = {(x["play"], x["channel"]): x for x in ov["funnel"]}
check("  and the funnel shows the failed send as a failure, not a delivery",
      ((fm.get(("missed_calls", "email")) or {}).get("errors"),
       (fm.get(("missed_calls", "email")) or {}).get("delivered")), (1, 0))
check("the trend has today's point", len(wsrow["trend"]) >= 1, True)

pv = W.usage_preview(Req(), "blancocafe", "missed_calls")
check("preview renders the real email from their numbers", pv["email"]["subject"], "2 missed calls this week")
check("  and the card", "2 callers" in pv["card"]["title"], True)
pv2 = W.usage_preview(Req(), "blancocafe", "drafts_waiting")
check("an in-app-only play previews with no email", pv2["email"], None)

W.mailer.smtplib.SMTP = FakeSMTP
t = W.usage_test_email(Req(), W.UsageTest(slug="blancocafe", play="missed_calls"))
check("a test email goes to the owner, never the customer", (t["to"], FakeSMTP.sent[-1][0]), (OWNER, OWNER))
check("  marked as a test", FakeSMTP.sent[-1][1].startswith("[TEST] "), True)

print("\nFAILS: %d" % fails)
sys.exit(1 if fails else 0)
