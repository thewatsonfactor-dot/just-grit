# -*- coding: utf-8 -*-
"""pick_pitch promises offer and proof always come from the SAME evidence.

Step 1 honoured that. Step 3 did not: it returned `listed[0]` - whatever
qualify_one ranked first, usually AI Vision off a middling star rating - and
paired it with a finding from the website scanner. Twelve of the first
forty-five sends went out that way, and four carried a subject line promising
"something in your reviews" over a body that never mentioned a review.

The case these tests are named for is real: Studio 1604 Pilates and Fitness
received a subject about their reviews, a body about their phone number not
being tap-to-call, and a pitch about pointing a security camera at it.

No network, no db.
"""
import sys
sys.path.insert(0, '.')
import webapp as W

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── the Studio 1604 regression, end to end ──────────────────────────────
studio = {"company": "Studio 1604 Pilates and Fitness", "domain": "studio1604.com",
          "vertical": "generic", "offers": "AI Vision",
          "opener": "your phone number isn't tap-to-call - on a phone, tapping it does nothing"}
offer, proof = W.pick_pitch(studio)
# Since 2026-09-23 a number that won't dial is the missed-call story (Daniel:
# "people can't call you from your website, and if they do call and you're
# busy...") - and that email's fix includes making the number tappable.
check("a website finding no longer sells cameras", offer, "AI Receptionist")
check("  the proof is still the real site finding", "tap-to-call" in proof)
email = W.compose_email(studio, [], variant="finding")
check("  the subject no longer promises reviews",
      "in your reviews" not in email["subject"])
check("  the body never mentions a camera", "camera" not in email["body"].lower())
check("  and the fix offered is a better site",
      "page" in email["body"].lower() or "site" in email["body"].lower())

# ── the finding decides which offer, not `offers` ───────────────────────
def pitch(opener, offers="AI Vision"):
    return W.pick_pitch({"company": "X", "domain": "x.com", "offers": offers,
                         "opener": opener})[0]
check("booking finding -> Ordering App",
      pitch("there's no way to book online - if they can't call right then, they book elsewhere"),
      "Ordering App")
check("catering finding -> Ordering App",
      pitch("there's no way to ask about catering or a big party without calling"), "Ordering App")
check("no-phone-number finding -> AI Receptionist",
      pitch("there's no phone number on your homepage - on a phone, there's no way to call you"),
      "AI Receptionist")
check("after-hours quote finding -> AI Receptionist",
      pitch("there's no way for after-hours visitors to request a quote"), "AI Receptionist")
check("reviews-not-shown finding -> Reviews & Rewards",
      pitch("your Google reviews don't show anywhere on your own site"), "Reviews & Rewards")
check("a plain site-speed finding -> Website",
      pitch("your server takes over a second to even start responding"), "Website")

# ── never guess an offer from `offers` ──────────────────────────────────
unclassifiable = {"company": "Y", "domain": "y.com", "offers": "AI Vision, Website",
                  "complaint": "the parking lot is always completely full on weekends"}
offer, proof = W.pick_pitch(unclassifiable)
check("an unclassifiable review quotes them but names no product", offer, "")
check("  their words are still used", "parking lot" in proof)
e = W.compose_email(unclassifiable, [], variant="finding")
check("  and the email falls back to honest generic copy",
      "camera" not in e["body"].lower())

# ── step 1, the path that works, is untouched ───────────────────────────
good = {"company": "Blanco Cafe", "domain": "blanco.com", "vertical": "restaurant",
        "offers": "AI Vision",
        "complaint": "inattentive servers, checks dropped without a check-back"}
offer, proof = W.pick_pitch(good)
check("a real review complaint still drives the pitch", offer, "AI Vision")
check("  and still quotes them", "checks dropped" in proof)
e = W.compose_email(good, [], variant="finding")
check("  subject may promise reviews, because the body delivers one",
      ("review" in e["subject"].lower()) and ("review" in e["body"].lower()))

# ── the ordering-app override from the earlier fix still stands ─────────
check("restaurant w/ qualified Ordering App still beats a generic Vision match",
      W.pick_pitch({"company": "Z", "domain": "z.com", "vertical": "restaurant",
                    "offers": "Ordering App, AI Vision",
                    "complaint": "service was slow and the staff seemed rude"})[0],
      "Ordering App")

# ── nothing at all ──────────────────────────────────────────────────────
offer, proof = W.pick_pitch({"company": "Nothing Known", "domain": "nk.com"})
check("with no evidence at all, no product is named", offer, "")
check("  and the line is honest about why we're writing",
      "came across" in proof)

# ── "Website" is two problems, and they need different sentences ────────
onsite = W.compose_email(studio, [], variant="finding")
check("a site-scan finding does NOT claim they're hard to find on Google",
      "hard to find on Google" not in onsite["subject"])
check("  it paints them finding you and the site losing them",
      "They land on your site" in onsite["body"])
nosite = {"company": "No Site Co", "vertical": "contractor", "offers": "Website",
          "note": "Web: no website | Ordering: n/a"}
off, proof = W.pick_pitch(nosite)
e2 = W.compose_email(nosite, [], variant="finding")
check("but a genuinely unfindable business still gets the findability pitch",
      "hard to find on Google" in e2["subject"])
check("  and is not told they 'already found you'",
      "already found you" not in e2["body"])

# ── the raw <title> leak: subject says "Kirby Family Vet", body says the
# whole scraped title with the pipe in it. usable_company() gates on the
# CLEANED name, so these rows pass the gate and go out looking like a form
# letter. The proof sentence must use the same name the subject does.
kirby = {"company": "Best Vet Hospital In San Antonio, TX | Kirby Family Vet",
         "domain": "kirbyfamilyvet.com",
         "complaint": "Multiple reviews flag long waits and rude front desk"}
check("a piped page title still passes the company gate",
      W.usable_company(kirby["company"], kirby["domain"]))
offer, proof = W.pick_pitch(kirby)
check("  the proof sentence never carries the raw title", "|" not in proof)
check("  the proof uses the cleaned name", "reviews for Kirby Family Vet " in proof)
e3 = W.compose_email(kirby, [], variant="finding")
check("  subject and body agree on the name",
      "Kirby Family Vet" in e3["subject"] and "|" not in e3["body"])

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
