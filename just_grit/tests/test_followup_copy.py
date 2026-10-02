# -*- coding: utf-8 -*-
"""A follow-up has to work for somebody who never read the first email.

The version this tests replaced said only:

    "Short one - did this land with you?"

"this" referred to an email the reader had, by definition, not answered and
most likely never opened. There was no antecedent. Worse, the Outbox approves
a draft through a `mailto:` link, which always opens a NEW compose window -
so the original was never quoted underneath, and the subject's "Re:" was a
claim the message could not back up.

The root cause was upstream: compose_watson_followup was handed a subject
line and nothing else, so it could not have restated the point in principle.

The rule these tests enforce: the follow-up may only repeat text that can be
shown to appear in the email that actually went out. Touch 2 then opens a
DIFFERENT door - the next-best offer, in that offer's own general words,
never a quote it has no evidence for (Daniel, 2026-09-22: "if there is not
a response from the first product email we try to follow and also offer
different products"). Touch 3 is the last note, carrying the point and a
link to the one-sheet.

Temp data dir, no network, never touches the live databases.
"""
import os, pathlib, sqlite3, sys, tempfile
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-fcopy-")
os.environ["JUST_GRIT_DATA"] = TMP
import webapp as W
import workspaces as ws
W.DATA = pathlib.Path(TMP)
W.init_db()
W.set_setting("sender_name", "Daniel Watson")
W.set_setting("sender_phone", "(210) 810-7476")
W.set_setting("sender_site", "thewatsonfactor.dev")
W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130")

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

COMPLAINT = "Understaffed; inattentive servers; checks dropped without a check-back"
SENT_BODY = (
    "Hi there,\n\n"
    "I was reading the reviews for Blanco Cafe and more than one of them says "
    'the same thing - "%s."\n\n'
    "Every one of those is a table that tipped less and a review that scared off "
    "the next guest.\n\n"
    "Your cameras already see it. I wire them so a manager gets a ping when a table "
    "has gone too long without a check-back - during the shift, not after the review.\n\n"
    "Worth fifteen minutes? I'm local - New Braunfels.\n\n"
    "Daniel Watson\nThe Watson Factor Development\n(210) 810-7476 · thewatsonfactor.dev\n\n"
    "—\n3217 Wild Iris, New Braunfels, TX 78130\n"
    'Reply "stop" and I\'ll remove you permanently.\n' % COMPLAINT)
SENT_SUBJ = "Blanco Cafe - something in your reviews"
SENT_AT = "2026-08-23T14:02:11+00:00"

D = {"company": "Blanco Cafe", "domain": "blancocafesa.com", "vertical": "restaurant",
     "complaint": COMPLAINT, "email": "info@blancocafesa.com"}

def compose(step, d=None, body=SENT_BODY, sent=SENT_AT, contacts=None):
    return W.compose_watson_followup(d or D, contacts or [], step, SENT_SUBJ, body, sent)

two, three = compose(2), compose(3)
above2 = two["body"].split("Daniel Watson")[0]
above3 = three["body"].split("Daniel Watson")[0]

# ── it must read as a sentence to a cold reader ─────────────────────────
check("the demonstrative with no antecedent is gone",
      "did this land with you" in two["body"], False)
check("the reader is told what it was about", "Blanco Cafe's reviews" in above2, True)
check("  and offered a different idea, plainly", "Different idea this time" in above2, True)
check("  with the date the first one went out", "August 23" in above2, True)
check("  and the business named", "Blanco Cafe" in above2, True)
check("the last touch says it is the last", "Last note from me" in above3, True)
check("  and also carries the point", COMPLAINT in above3, True)

# ── the original travels with it, because a mailto has no thread ────────
check("the first email is quoted underneath", "> I was reading the reviews" in two["body"], True)
check("  introduced by when it was sent", "On August 23 I wrote:" in two["body"], True)
check("  as a real quote block, not pasted prose",
      all(ln.startswith(">") for ln in two["body"].split("I wrote:\n\n")[1].strip().split("\n")
          if ln.strip()), True)
check("  and the subject can honestly say Re:", two["subject"], "Re: " + SENT_SUBJ)

# ── the footer appears once, not twice ──────────────────────────────────
check("the mailing address is in the message exactly once",
      two["body"].count("3217 Wild Iris"), 1)
check("  and so is the opt-out line",
      two["body"].count('Reply "stop"'), 1)
check("  because the quote stops at the old sign-off",
      "> Daniel Watson" in two["body"], False)

# ── it may not promise a quote that is not there ────────────────────────
noquote = compose(2, body="")
check("with nothing to quote it does not claim there is",
      "below" in noquote["body"].split("Daniel Watson")[0], False)
check("  and it still says something true",
      "I wrote on August 23" in noquote["body"], True)

# ── it may not repeat what the first email did not say ──────────────────
other = dict(D, complaint="Rude staff and long waits")
fabricated = compose(2, d=other)
check("a complaint absent from the sent email is NOT repeated",
      "Rude staff" in fabricated["body"], False)
check("  it falls back to the version that claims nothing about the reviews",
      "something I'd noticed" in fabricated["body"], True)
check("  and the sent email is still quoted, so the point survives",
      "> I was reading the reviews" in fabricated["body"], True)

# ── touch 2 pitches a DIFFERENT product; touch 3 pitches none ───────────
copy = W.offer_copy_map()
check("the first email was about AI Vision", "cameras already see it" in SENT_BODY, True)
check("touch 2 carries a different offer", two["offer"] not in ("", "AI Vision"), True)
check("  in that offer's own words", copy[two["offer"]][2][:40] in above2, True)
check("  and never the camera pitch again", "cameras" in above2.lower(), False)
check("  exactly one product's copy above the quote",
      sum(1 for name, (t, cst, fx) in copy.items() if fx[:40] in above2), 1)
check("touch 3 pitches nothing new", any(fx[:40] in above3 for _, (t, cst, fx) in copy.items()), False)
check("  it is stamped as a follow-up, not a variant under test",
      two["variant"], "followup:2")

# ── shape and edges ─────────────────────────────────────────────────────
longc = "x" * 400
longbody = SENT_BODY.replace(COMPLAINT, longc)
out = W.compose_watson_followup(dict(D, complaint=longc), [], 3, SENT_SUBJ, longbody, SENT_AT)
check("a runaway complaint is truncated, not pasted whole",
      len(out["body"].split("Daniel Watson")[0]) < 700, True)
check("  and says so with an ellipsis", "..." in out["body"], True)

huge = SENT_BODY.replace("Every one of those", "PADDING " * 400 + "Every one of those")
out = W.compose_watson_followup(D, [], 2, SENT_SUBJ, huge, SENT_AT)
check("a runaway original is clipped rather than quoted in full",
      len(out["body"]) < 3000, True)

check("an unreadable send date is left out instead of guessed",
      "None" in compose(2, sent="not a date")["body"], False)
check("  and the fallback wording still reads",
      "I wrote recently" in compose(2, body="", sent="")["body"], True)

named = compose(2, contacts=[{"name": "Bobby Ruiz", "email": "bobby@x.com"}])
check("a named contact is greeted by first name", named["body"].startswith("Hi Bobby,"), True)
check("an unknown contact is not", two["body"].startswith("Hi there,"), True)

check("the legal footer is still enforced", two["address_missing"], False)

# ── the complaint column is not always a complaint ──────────────────────
# Twenty-nine of the forty-five rows due a follow-up literally say
# "not verified". Five more carry an analyst's shorthand. None of it is
# something a customer said, and none of it may ever be quoted to an owner.
for junk in ("not verified", "Not Verified", "none", "N/A", "unknown", "TBD",
             "(confirm on visit) upscale table service",
             "category-risk only (busy vet, front-desk phone)"):
    faked = SENT_BODY.replace(COMPLAINT, junk)
    out = W.compose_watson_followup(dict(D, complaint=junk), [], 2, SENT_SUBJ, faked, SENT_AT)
    check("%r is never quoted as a customer complaint" % junk[:34],
          junk in out["body"].split("Daniel Watson")[0], False)
check("  even so, the original still goes underneath",
      "I wrote:" in W.compose_watson_followup(
          dict(D, complaint="not verified"), [], 2, SENT_SUBJ, SENT_BODY, SENT_AT)["body"], True)

# ── an analyst's label is stripped, the customer's words are kept ───────
labelled = SENT_BODY.replace('"%s."' % COMPLAINT, '"employee standing around."')
out = W.compose_watson_followup(dict(D, complaint="Isolated: employee standing around"),
                                [], 3, SENT_SUBJ, labelled, SENT_AT)
above = out["body"].split("Daniel Watson")[0]
check("the words the email actually used are quoted (touch 3 carries the point)",
      '"employee standing around."' in above, True)
check("  and our own label is not", "Isolated" in above, False)
out = W.compose_watson_followup(dict(D, complaint="Isolated: something never sent"),
                                [], 2, SENT_SUBJ, labelled, SENT_AT)
check("  stripping a label does not license quoting words that were not sent",
      "something never sent" in out["body"], False)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
