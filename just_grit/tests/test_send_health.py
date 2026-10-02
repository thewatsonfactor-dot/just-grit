# -*- coding: utf-8 -*-
"""The daily-cap decision, made on real bounce/unsubscribe numbers instead of
a guess. Modeled on Warmbly's own day-one ramp (10 -> +5/day -> ceiling 50)
and their bulk-sender bar - see Deliverability_and_Warmbly_Review.

Runs against an in-memory sqlite db, never the live one.
"""
import sqlite3
import sys
sys.path.insert(0, '.')
import webapp as W

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))


def fresh_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE outreach (
        id INTEGER PRIMARY KEY, state TEXT, sent_at TEXT, reply TEXT DEFAULT '')""")
    return c


def seed(c, n, reply=None, days_ago=1):
    for _ in range(n):
        c.execute("INSERT INTO outreach (state, sent_at, reply) VALUES "
                  "('sent', date('now', ?), ?)", ("-%d days" % days_ago, reply))
    c.commit()


# ── send_health ──────────────────────────────────────────────────────────
c = fresh_db()
seed(c, 20, reply=None)               # clean sends, no reply at all
seed(c, 2, reply="bounce")
seed(c, 1, reply="unsubscribe")
seed(c, 1, reply="positive")
seed(c, 1, reply="negative")
seed(c, 1, reply="auto_reply")        # not a real reply for this purpose
h = W.send_health(c, days=30)
check("counts total sent", h["sent"], 26)
check("counts bounces", h["bounced"], 2)
check("counts unsubscribes", h["unsubscribed"], 1)
check("counts only positive+negative as replied, not auto_reply",
      h["replied"], 2)
check("bounce rate is bounced/sent", h["bounce_rate"], round(100 * 2 / 26, 1))
check("unsubscribe rate is unsubscribed/sent", h["unsubscribe_rate"], round(100 * 1 / 26, 1))

c2 = fresh_db()
seed(c2, 5, reply="bounce", days_ago=40)   # outside the 30-day window
h2 = W.send_health(c2, days=30)
check("a send outside the window doesn't count", h2["sent"], 0)
check("zero sent means rates are None, not a divide-by-zero", h2["bounce_rate"], None)


# ── cap_recommendation ───────────────────────────────────────────────────
def health(sent, bounced=0, unsub=0, replied=0, days=30):
    def rate(n): return round(100.0 * n / sent, 1) if sent else None
    return {"window_days": days, "sent": sent, "bounced": bounced,
            "unsubscribed": unsub, "replied": replied,
            "bounce_rate": rate(bounced), "unsubscribe_rate": rate(unsub),
            "reply_rate": rate(replied)}

r = W.cap_recommendation("passing", health(5, bounced=0), current_cap=10)
check("too few sends -> hold, regardless of how clean", r["action"], "hold")

r = W.cap_recommendation("passing", health(30, bounced=3), current_cap=10)  # 10% bounce
check("high bounce rate -> lower, even with passing auth", r["action"], "lower")

r = W.cap_recommendation("failing", health(30, bounced=0), current_cap=10)
check("clean sends but failing auth -> hold, not raise", r["action"], "hold")

r = W.cap_recommendation("passing", health(30, bounced=0, unsub=0), current_cap=10)
check("clean sends and passing auth -> raise", r["action"], "raise")
check("raise respects the +5 increment", r["suggested_cap"], 15)

r = W.cap_recommendation("passing", health(30, bounced=0, unsub=0), current_cap=48)
check("raise never exceeds the ceiling", r["suggested_cap"], 50)

r = W.cap_recommendation("passing", health(30, bounced=0, unsub=0), current_cap=50)
check("already at the ceiling -> hold, not raise past it", r["action"], "hold")

r = W.cap_recommendation("passing", health(30, bounced=1, unsub=1), current_cap=10)  # ~3.3%
check("bounce rate between 2-5% is a hold, not a raise or a lower",
      r["action"], "hold")


print("-" * 70)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
