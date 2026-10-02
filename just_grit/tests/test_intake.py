# -*- coding: utf-8 -*-
"""Onboarding: four questions per offer in, a draft catalog out.

The question this answers is "how would the system know how to sell for
somebody else." It can't learn that from a website — a homepage says what a
company sells, never what is broken at a customer the moment before they buy.
That second thing IS the evidence rule, and the owner is the only one who
knows it.

So everything here must be built from the owner's own sentences, nothing
invented, and handed back as a draft rather than saved.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-in-")
os.environ["JUST_GRIT_DATA"] = TMP
sys.path.insert(0, '.')
import webapp as W, workspaces as ws
W.DATA = pathlib.Path(TMP)
for _s in list(ws.WORKSPACES):
    _t = ws.CURRENT.set(_s)
    try: W.init_db()
    finally: ws.CURRENT.reset(_t)
W.require_auth = lambda r: "owner@x.com"
class Req: headers = {}; cookies = {}

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── the trigger sentence becomes something the matcher can use ──────────
ev = W.trigger_to_evidence("they call after six and get voicemail so they book somewhere else")
check("keywords come out of the owner's sentence", "voice ?mail|voicemail" in ev)
check("  filler words are dropped", "they" not in ev and "and" not in ev)
check("  and it is a regex that compiles", bool(__import__("re").compile(ev)))
check("an empty trigger yields nothing rather than matching everything",
      W.trigger_to_evidence(""), "")
check("a trigger of pure filler yields nothing, not a catch-all",
      W.trigger_to_evidence("they just have some of the things that we do"), "")

# ── the round trip: owner's words in, editable draft out ────────────────
r = W.intake(Req(), W.IntakeBody(
    domain="stampschiropractic.com",
    sells="chiropractic care for accident injuries",
    buyer="people hurt in car wrecks around New Braunfels",
    offers=[{"name": "After-Hours Line",
             "trigger": "patients call after six and get voicemail so they book elsewhere",
             "costs": "the patient books with whoever picks up first",
             "does": "we answer every call around the clock and text the front desk"},
            {"name": "Half Answered", "trigger": "something", "costs": ""}]))
d = r["draft"]["offers"]
check("a complete offer becomes a draft offer", "After-Hours Line" in d)
check("an incomplete one is skipped and SAID so", "Half Answered" not in d)
check("  with a note naming it", any("Half Answered" in n for n in r["notes"]))

o = d["After-Hours Line"]
check("the cost sentence is the owner's own words",
      o["cost"], "The patient books with whoever picks up first.")
check("the fix sentence is theirs too",
      o["fix"], "We answer every call around the clock and text the front desk.")
check("the subject tail reads like a fragment, not a sentence",
      o["tail"][0].islower() and not o["tail"].endswith("."))
check("the evidence came from their trigger", "voice ?mail" in o["evidence"])
check("the question carries exactly one %s for the company name",
      o["question"].count("%s"), 1)

# ── nothing is saved until a human says so ──────────────────────────────
W.create_workspace(Req(), W.NewWorkspaceBody(slug="stamps", name="Stamps Chiropractic"))
tok = ws.CURRENT.set("stamps")
try:
    W.intake(Req(), W.IntakeBody(offers=[{"name": "X", "trigger": "voicemail after hours",
                                          "costs": "lost", "does": "we answer"}]))
    check("intake wrote nothing to the workspace", W.has_catalog(), False)
    # and the draft it produced is accepted by the real validator
    draft = W.intake(Req(), W.IntakeBody(offers=[{
        "name": "After-Hours Line",
        "trigger": "patients call after six and get voicemail",
        "costs": "they book with whoever picks up",
        "does": "we answer every call and text the desk"}]))["draft"]
    saved = W.save_catalog(Req(), W.CatalogBody(catalog=draft))
    check("the draft saves cleanly through the normal path", saved["saved"], ["After-Hours Line"])
    check("  nothing was dropped on the way", saved["dropped"], [])
    check("  and the workspace can now draft", W.has_catalog(), True)

    # end to end: an email built entirely from what the owner typed
    e = W.compose_email({"company": "Higher Living Chiropractic", "domain": "hl.com",
                         "vertical": "chiro", "offers": "After-Hours Line",
                         "complaint": "called twice and got voicemail both times"},
                        [], variant="finding")
    check("a real complaint selects the offer their trigger described",
          "whoever picks up" in e["body"])
    check("  and the email carries no Watson Factor product names",
          not any(p in e["body"] for p in ("AI Vision", "camera", "delivery apps")))
finally:
    ws.CURRENT.reset(tok)

# ── an unreachable site is reported, not silently ignored ───────────────
r2 = W.intake(Req(), W.IntakeBody(domain="this-domain-does-not-exist-31337.com"))
check("a site that won't load is called out in the notes",
      any("Couldn't read" in n for n in r2["notes"]))

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
