# -*- coding: utf-8 -*-
"""Website > Ordering App > AI Vision when more than one offer qualifies.

Daniel's call (2026-09-18): stop leaning on AI Vision as the default pitch
for restaurants and lead with the branded ordering app instead, wherever the
evidence actually supports it. AI Vision's own regex (understaff, wait, slow,
rude, damage, scratch...) is broad enough to fire on almost any negative
review, which is why it was winning by default even for restaurants with a
real ordering-app case. pick_pitch keeps offer+proof paired - never quote a
review about slow service and then pitch an ordering page - so the fix has
to swap both together, not just the label.

Runs with no network and no real db - pick_pitch takes a plain dict.
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


# ── pick_pitch: restaurant, generic negative-service complaint, but already
#    qualified for Ordering App (no ordering system found on their own site)
#    -> Ordering App wins, and the proof is about ordering, not the review ──
d = {"company": "Blanco Cafe", "domain": "blancocafe.com", "vertical": "restaurant",
     "offers": "Ordering App, AI Vision",
     "complaint": "inattentive servers, checks dropped without a check-back"}
offer, proof = W.pick_pitch(d)
check("generic service complaint + qualified Ordering App -> offer is Ordering App",
      offer, "Ordering App")
check("proof talks about ordering, not the review", "order" in proof.lower())
check("proof does NOT quote the review complaint (would be a mismatch)",
      "checks dropped" not in proof)

# ── Same complaint, but Ordering App was never qualified for this prospect
#    (e.g. auto vertical, or a restaurant where an ordering system WAS found)
#    -> falls back to AI Vision exactly as before, no regression ──
d2 = {"company": "Auto Dude Detailing", "domain": "autodude.com", "vertical": "auto",
      "offers": "AI Vision",
      "complaint": "they scratched my bumper and left it dusty and dinged up"}
offer2, proof2 = W.pick_pitch(d2)
check("no qualified Ordering App -> AI Vision unaffected", offer2, "AI Vision")
check("proof still quotes the real review in the unaffected case",
      "scratched my bumper" in proof2)

# ── Same override applies on the unquotable-complaint branch (1b) - no quote
#    involved there either way, so it's a plain offer swap ──
d3 = {"company": "Nicha's", "domain": "nichas.com", "vertical": "restaurant",
      "offers": "Ordering App, AI Vision", "city": "San Antonio",
      "complaint": "(confirm on visit) service seemed slow"}
offer3, proof3 = W.pick_pitch(d3)
check("unquotable complaint + qualified Ordering App -> still swaps to Ordering App",
      offer3, "Ordering App")

# ── A prospect where Ordering App was never mentioned in `offers` at all
#    (no complaint mapped it, e.g. Website-only) is untouched ──
d4 = {"company": "Corner Diner", "domain": "cornerdiner.com", "vertical": "restaurant",
      "offers": "Website",
      "complaint": "the service was slow and the staff seemed rude"}
offer4, proof4 = W.pick_pitch(d4)
check("Ordering App not in offers -> AI Vision still wins normally", offer4, "AI Vision")

# ── qualify_one's own ordering: doesn't touch it here (needs network), but
#    lock in the priority list itself hasn't drifted ──
import inspect
src = inspect.getsource(W.qualify_one)
check("qualify_one order still Website > Ordering App > AI Vision",
      'order = ["Website", "Ordering App", "AI Vision"' in src)
check("qualify_one has the new no-ordering-found evidence branch",
      "no ordering system of their own to point to" in src)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
