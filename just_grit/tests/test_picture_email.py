# -*- coding: utf-8 -*-
"""The missed-call email paints a picture.

Daniel, on "S&S Tire & Auto - the calls you're missing": "we're missing the
target. It should be: I see people can't call you from your website, and if
they do call and you're busy, we have an AI assistant that takes down all
their info and texts you. These are some of the things we could help you
with. Paint a picture. Be creative."

And the same draft was addressed to email@email.com - the filler address
from their site's footer.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-pic-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws, research as R, triggers as T
from contextlib import closing

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.set_setting("sender_name", "Daniel Watson"); W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130")

NOPHONE = "there's no phone number on your homepage — on a phone, there's no way to call you"
NOTAP = "your phone number isn't tap-to-call — on a phone, tapping it does nothing"
AFTER = "there's no way for after-hours visitors to request a quote — the 9 PM researcher can't reach you"
ONLY = "the only way to reach you from the site is to pick up the phone"
def lead(**kw):
    d = {"company": "S&S Tire & Auto", "domain": "sstireautotx.com", "vertical": "auto",
         "category": "Tire / Auto Repair", "opener": NOPHONE, "complaint": "", "offers": ""}
    d.update(kw); return d

e = W.compose_email(lead(), [], variant="finding")
b = e["body"]
check("S&S gets the AI Receptionist email", e["offer"], "AI Receptionist")
check("  subject says it the way Daniel did", e["subject"], "S&S Tire & Auto - people can't call you from your website")
check("  opens plainly: people can't call you from your website", "People can't call you from your website." in b)
check("  paints the picture - their trade, their customer", "nail in their tire" in b and "under a car" in b)
check("  both halves: the site, and the call you can't pick up", ("number to tap" in b, "It rings out" in b), (True, True))
check("  the fix: a tap button AND an assistant that texts you the details",
      ("button they can tap" in b, "texts it to you within seconds" in b), (True, True))
check("  the story is long, so the 'I also do...' sentence stays out (a first email reads best under 130 words) - and never a bullet list",
      ("I also do " in b, "\n- " in b), (False, False))
check("  - not the product already pitched, not the site fix again",
      ("answers the calls you can't" in b, "shows up when people around here search" in b), (False, False))
check("  no delivery-app pitch to a tire shop", "delivery apps" in b, False)
check("  ask and legal footer still there", ("Worth fifteen minutes?" in b, 'Reply "stop"' in b), (True, True))
check("  the old principle line is gone", "next number on the list" in b, False)

# each finding tells its own true story
t = W.compose_email(lead(opener=NOTAP), [], variant="finding")
check("a number that won't dial is the same story now", (t["offer"], "tapping it does nothing" in t["body"]), ("AI Receptionist", True))
a = W.compose_email(lead(opener=AFTER, vertical="contractor", category="HVAC", company="Hill Country HVAC"), [], variant="finding")
check("after-hours: no 'number to tap' claim, a quote form instead",
      ("number to tap" in a["body"], "quote request" in a["body"], "after you close" in a["subject"]), (False, True, True))
o = W.compose_email(lead(opener=ONLY, vertical="restaurant", category="Taqueria", company="Taco Haven"), [], variant="finding")
check("phone-only restaurant: dinner rush, text-us form",
      ("dinner rush" in o["body"], "'text us'" in o["body"]), (True, True))
short = W.compose_email({"company": "Taco Haven", "domain": "tacohaven.com", "vertical": "restaurant", "category": "Taqueria",
                         "opener": "", "complaint": "", "offers": "Website"}, [], variant="impact")
check("a short email gets the one-sentence menu, with the restaurant's own ordering in it",
      ("I also do " in short["body"], "own online ordering" in short["body"], "\n- " in short["body"]), (True, True, False))

# the trade picks the scene
scene = lambda **kw: W.compose_email(lead(**kw), [], variant="finding")["body"]
check("vet: a scared pet owner", "dog" in scene(vertical="appointment", category="Veterinary"))
check("dentist: a cracked tooth", "cracked a tooth" in scene(vertical="appointment", category="Dental"))
check("gym: someone ready to start today", "first class" in scene(vertical="generic", category="Fitness / Gym"))
check("chiro: a thrown-out back", "threw their back out" in scene(vertical="chiro", category="Chiropractor"))
check("unknown trade: the plain scene", "ready to spend money with you" in scene(vertical="generic", category="Bookstore"))
check("dentist gets no camera pitch in the list", "camera" in scene(vertical="appointment", category="Dental").lower(), False)

# the shorter variants carry the picture too, and the question one stays a question
art = W.compose_email(lead(), [], variant="artifact")["body"]
check("the 'send it' variant carries the picture", "nail in their tire" in art)
q = W.compose_email(lead(), [], variant="question")["body"]
check("the question variant stays one question", "Picture" in q, False)

# findings the scene isn't written for keep their own copy
w = W.compose_email(lead(opener="your site shows customers a 'Not Secure' warning right in the address bar"), [], variant="finding")
check("a different finding keeps its own email", ("Picture" in w["body"], w["offer"]), (False, "Website"))

# ── placeholder addresses ────────────────────────────────────────────────
check("email@email.com is filler", R.is_placeholder_email("email@email.com"))
check("  so is you@yourdomain.com", R.is_placeholder_email("you@yourdomain.com"))
check("  a real inbox is not", (R.is_placeholder_email("info@sstireautotx.com"), R.is_placeholder_email("owner@gmail.com")), (False, False))
check("the site reader skips it", R.emails_in('<a href="mailto:email@email.com">x</a> office@sstireautotx.com', "sstireautotx.com"),
      ["office@sstireautotx.com"])
check("and it's never picked as the recipient",
      T.usable_addresses([{"name": "", "email": "email@email.com"}], "email@email.com"), [])

with closing(W.db()) as c:
    c.execute("INSERT INTO prospects (place_id, company, domain, email, status, stage, added_at, vertical, category, opener) "
              "VALUES ('pl:ss','S&S Tire & Auto','sstireautotx.com','email@email.com','new','lead',?, 'auto','Tire / Auto Repair',?)", (W.now(), NOPHONE))
    ss = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO prospects (place_id, company, domain, email, status, stage, added_at, vertical, category, opener) "
              "VALUES ('pl:ib','inBalance','inbalancestudios.com','info@inbalancestudios.com','new','lead',?, 'generic','Fitness / Gym',?)", (W.now(), NOPHONE))
    ib = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO prospects (place_id, company, domain, email, status, stage, added_at, vertical, category, opener) "
              "VALUES ('pl:ed','Edited Co','edited.com','info@edited.com','new','lead',?, 'generic','Fitness / Gym',?)", (W.now(), NOPHONE))
    ed = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    old = "Hi there,\n\nA missed call from a customer with a problem right now is a job that goes to the next number on the list."
    for pid, to, body, orig in ((ss, "email@email.com", old, old), (ib, "info@inbalancestudios.com", old, old),
                                (ed, "info@edited.com", "my own words", old)):
        c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, subject_original, body_original, state, created_at, variant, step) "
                  "VALUES (?,?,?,?,?,?,?,'draft',?,'finding',1)", (pid, "AI Receptionist", to, "s", body, "s", orig, W.now()))
    c.commit()
    n = W.drop_placeholder_addresses(c); c.commit()
    check("the draft to email@email.com is pulled from the Outbox", n, 1)
    check("  and the filler address is cleared off the lead", c.execute("SELECT email FROM prospects WHERE id=?", (ss,)).fetchone()[0], "")
    r = W.redraft_untouched(c); c.commit()
    check("waiting drafts nobody touched get the new copy", r, 1)
    check("  inBalance's draft now paints the picture",
          "first class" in c.execute("SELECT body FROM outreach WHERE prospect_id=?", (ib,)).fetchone()[0])
    check("  a draft Daniel edited is left alone",
          c.execute("SELECT body FROM outreach WHERE prospect_id=?", (ed,)).fetchone()[0], "my own words")
    check("  running it again changes nothing", W.redraft_untouched(c), 0)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
