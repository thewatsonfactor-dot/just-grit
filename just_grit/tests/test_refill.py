# -*- coding: utf-8 -*-
"""The Outbox fills itself.

"It doesn't populate the emails. It should load the leads from Today,
automatically, after the ones in there are sent out. 50 at a time."

What was wrong on the live database: 275 leads, 48 with an email, all 48
already written to. Nothing in the Outbox went and found addresses. The
refill reads a lead's site (homepage + contact page) for an address on
their own domain, qualifies them, and writes - until 50 are waiting - and
runs on its own when the Outbox is empty.
"""
import os, pathlib, sys, tempfile, time
TMP = tempfile.mkdtemp(prefix="jg-refill-")
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
W.set_setting("sender_name", "Daniel"); W.set_setting("sender_email", "d@x.dev")
W.set_setting("sender_address", "1 Main St"); W.set_setting("daily_email_cap", "999999")

class Res:
    def __init__(self, html, status=200): self.html = html; self.status = status; self.robots_allowed = True; self.error = ""
SITES = {}
FETCHED = []
def fake_fetch(url):
    FETCHED.append(url)
    host = url.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    path = "/" + "/".join(url.replace("https://", "").split("/")[1:])
    site = SITES.get(host)
    if not site:
        return Res("", 404)
    return Res(site.get(path) or site.get("/") or "", 200)

# 60 leads with a site and no address; 40 list an email on the contact page, 20 don't
with closing(W.db()) as c:
    for i in range(60):
        dom = "biz%02d.com" % i
        c.execute("INSERT INTO prospects (place_id, company, domain, status, stage, added_at, vertical, category, tier, rating, reviews) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("pl:" + dom, "Business %02d" % i, dom, "new", "lead", W.now(), "restaurant", "Full-Service Restaurant", 2, 4.0, 40))
        if i < 40:
            SITES[dom] = {"/": '<a href="/contact">Contact</a><p>Best tacos in town</p>',
                          "/contact": '<a href="mailto:hello@%s">Email us</a>' % dom}
        else:
            SITES[dom] = {"/": "<p>no contact info here</p>"}
    c.commit()

W.qualify_one = lambda d: (["AI Vision"], {"AI Vision": "Their own reviews describe service going wrong in the room."})   # no network
st = W.refill_status(Req())
check("before: nothing waiting, 60 leads with a site still unread", (st["waiting"], st["candidates"]), (0, 60))

t0 = time.time()
st = W.refill_outbox(ws.PRIMARY, want=50, fetch=fake_fetch)
check("the refill reads sites until 50 are waiting", st["waiting"] >= 40, True)
check("  found addresses on the ones that list one", st["found"], 40)
check("  read each site's contact page, not just the homepage", any(u.endswith("/contact") for u in FETCHED), True)
check("  qualified them", st["qualified"], 40)
check("  and wrote the emails", st["drafted"] >= 40, True)
check("  the 20 with nothing listed are recorded as checked",
      W.db().execute("SELECT COUNT(*) FROM prospects WHERE email='' AND email_checked_at<>''").fetchone()[0], 20)
check("  and are not read again next pass", W.refill_status(Req())["candidates"], 0)
check("  the run is not left 'running'", st["running"], False)
with closing(W.db()) as c:
    check("  the found address is on their own domain",
          c.execute("SELECT email FROM prospects WHERE domain='biz03.com'").fetchone()[0], "hello@biz03.com")
    drafts = c.execute("SELECT COUNT(*) FROM outreach WHERE state='draft'").fetchone()[0]
check("40 drafts waiting - it stopped when it ran out of leads with an address", drafts, 40)
check("  and said it is out of leads", (st["step"], st["need_leads"]), ("out of leads", True))

# a full Outbox is left alone
FETCHED.clear()
st = W.refill_outbox(ws.PRIMARY, want=30, fetch=fake_fetch)
check("with more waiting than wanted, a refill reads nothing", (st["step"], FETCHED), ("full", []))

# the automatic version fires only when the Outbox is EMPTY
check("auto-refill does nothing while emails are waiting and no unread leads remain", W.refill_auto(ws.PRIMARY), False)
with closing(W.db()) as c:
    c.execute("UPDATE outreach SET state='sent', sent_at=? WHERE state='draft'", (W.now(),)); c.commit()
    # ten new leads arrive
    for i in range(60, 70):
        dom = "biz%02d.com" % i
        c.execute("INSERT INTO prospects (place_id, company, domain, status, stage, added_at, vertical, tier) VALUES (?,?,?,?,?,?,?,?)",
                  ("pl:" + dom, "Business %02d" % i, dom, "new", "lead", W.now(), "restaurant", 2))
        SITES[dom] = {"/": '<a href="mailto:owner@%s">mail</a>' % dom}
    c.commit()
W.refill_outbox = lambda slug, want=50, fetch=None, max_sites=60: (W.refill_state(slug).update({"step": "ran"}) or W.refill_state(slug))
check("once everything is sent, the automatic refill kicks in", W.refill_auto(ws.PRIMARY), True)
time.sleep(0.3)
check("  and not again within the hold-off", W.refill_auto(ws.PRIMARY), False)
W.refill_state(ws.PRIMARY)["step"] = "paused - more next pass"
check("  but a run that hit its site budget short of 50 resumes at once", W.refill_auto(ws.PRIMARY), True)
time.sleep(0.3)

# focus limits what gets read
W.focus_set(Req(), W.FocusBody(trades=["Dental"]))
with closing(W.db()) as c:
    check("working Dental, none of these restaurants are candidates", len(W.email_candidates(c, 100)), 0)
W.focus_set(Req(), W.FocusBody(trades=[]))
with closing(W.db()) as c:
    check("  everything: the ten new ones are", len(W.email_candidates(c, 100)), 10)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
