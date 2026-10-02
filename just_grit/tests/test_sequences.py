# -*- coding: utf-8 -*-
"""The one rule: the signal chooses the angle and never reaches the prospect.

The spec's HARD RULES were written as instructions to a language model. Here
they are assertions instead, which is the difference between hoping and knowing.
"""
import sys
sys.path.insert(0, '.')
import sequences as S

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r" % (got,)))

SET = {"sender_name": "Daniel Watson", "sender_site": "HomeRepair.tech",
       "sender_phone": "(830) 205-0202", "sender_cell": "(210) 810-7476", "legal_entity": "HomeRepair Tech LLC",
       "sender_address": "12739 O'Conner Rd, Suite 102, San Antonio, TX 78233"}
get = lambda k, d="": SET.get(k, d)
CTX = {"first_name": "Melanie", "company": "Property Professionals",
       "community": "Oak Hollow", "city": "New Braunfels"}

# ── nothing ever leaks the signal ────────────────────────────────────
print("— leak scan over every template, every segment, every signal —")
combos = [("hoa", None), ("commercial", None), ("realtor", None)] + [("property_manager", s) for s in
          list(S.B1_BY_SIGNAL) + list(S.SIGNAL_ALIASES) + [None, "nonsense"]]
for seg, sig in combos:
    r = S.render(seg, sig, CTX, get, "https://homerepair.tech/u/abc")
    for e in r["emails"]:
        blob = " ".join(e["subject_options"]) + " " + e["preheader"] + " " + e["body_text"]
        leaks = S.leaks_signal(blob)
        check("%-16s/%-18s step %d clean" % (seg, sig or "-", e["step"]), leaks, [])

# ── the guard actually catches things (a matcher that can see) ───────
print("— the guard is not vacuous —")
check("catches a review mention",
      any("reviews" in x for x in S.leaks_signal("I saw your reviews were rough")), True)
check("catches 'I noticed'",
      any("been looking" in x for x in S.leaks_signal("I noticed your response times")), True)
check("catches a tenant assertion",
      any("their people feel" in x for x in S.leaks_signal("your tenants have been unhappy")), True)
check("catches a star rating",
      any("rating" in x for x in S.leaks_signal("you're at 4.6 stars across 82 reviews")), True)
check("catches banned filler",
      any("peace of mind" in x for x in S.leaks_signal("for total peace of mind")), True)
check("clean text passes", S.leaks_signal("Worth fifteen minutes?"), [])

# The guard must NOT fire on Daniel's own board copy. My first version banned
# the bare word "complaint" and flagged "a resident complaint, an exterior
# repair" - generic board-meeting language that asserts nothing about anyone.
# Over-matching a guard is how you end up rewriting good copy to satisfy a
# regex, so both directions are pinned.
check("generic 'a resident complaint' is allowed",
      S.leaks_signal("when something comes up at a meeting - a resident complaint"), [])
check("'resident issue reporting' is allowed",
      S.leaks_signal("scheduled walkthroughs, resident issue reporting"), [])

# ── routing ──────────────────────────────────────────────────────────
print("— angle selection —")
check("slow_response routes to itself", S.resolve_signal("slow_response"), "slow_response")
check("deferred_condition aliases", S.resolve_signal("deferred_condition"), "slow_response")
check("communication aliases", S.resolve_signal("communication"), "turnover_disputes")
check("unknown falls back", S.resolve_signal("banana"), "slow_response")
check("missing falls back", S.resolve_signal(None), "slow_response")
check("hoa ignores signal entirely",
      S.render("hoa", "vendor_quality", CTX, get)["angle_used"], None)
check("pm sequence is 4 steps", len(S.steps_for("property_manager", "vendor_quality")), 4)
check("hoa sequence is 4 steps", len(S.steps_for("hoa")), 4)
check("steps 2 and 3 are shared across variants",
      S.steps_for("property_manager", "slow_response")[1]["body"]
      == S.steps_for("property_manager", "vendor_quality")[1]["body"], True)

# ── merge fields ─────────────────────────────────────────────────────
print("— merge fields —")
r = S.render("hoa", None, {}, get)
b = r["emails"][0]["body_text"]
check("empty context leaves no raw tokens", "{{" not in b, True)
# A bare "there," on its own line reads like a merge field that failed.
check("no-name, no-company greeting is still a real greeting", b.startswith("Hi there,"), True)
check("and never a bare 'there,'", b.startswith("there,"), False)
check("community falls back", "your community" in b, True)
subj = r["emails"][0]["subject_options"][0]

print("— the greeting names them, not 'there' —")
g = lambda seg, ctx: S.render(seg, None, ctx, get)["emails"][0]["body_text"].splitlines()[0]
check("a first name is used", g("property_manager", {"first_name": "Ann", "company": "X"}), "Hi Ann,")
check("no name: the company, with the legal tail dropped",
      g("property_manager", {"company": "PMI Birdy Properties, CRMC"}), "Hi PMI Birdy Properties team,")
check("  LLC / Inc go too", g("property_manager", {"company": "Global Realty Group, LLC"}), "Hi Global Realty Group team,")
check("a board is addressed as a board, by the community's short name",
      g("hoa", {"community": "Misty Oaks Homeowners Association, Inc."}), "Hi Misty Oaks board,")
check("  'Property Owners Association' too", S.short_name("Stone Oak Property Owners Association", "hoa"), "Stone Oak")
check("  and a bare 'HOA'", S.short_name("Lafayette Place HOA", "hoa"), "Lafayette Place")
check("the HOA subject uses the short name",
      S.render("hoa", None, {"community": "Misty Oaks Homeowners Association"}, get)["emails"][0]["subject_options"][0],
      "budget season at Misty Oaks")
check("a name that is nothing but a legal tail still greets", S.short_name("LLC"), "LLC")

print("— no cost claims (the playbook forbids 'free') —")
import re as _re
for seg in ("hoa", "property_manager", "commercial", "realtor"):
    for e in S.render(seg, None, {"company": "X", "community": "X"}, get)["emails"]:
        check("%s step %d makes no cost claim" % (seg, e["step"]),
              bool(_re.search(r"\bfree\b|no cost|costs you nothing|no charge|complimentary", e["body_text"], _re.I)), False)

print("— commercial buildings —")
c = S.render("commercial", "vendor_quality", {"company": "Stone Oak Office Partners LLC", "city": "San Antonio"}, get)
check("commercial is its own 4-step sequence", (len(c["emails"]), c["angle_used"]), (4, None))
check("  it talks about buildings and tenants, not units and residents",
      ("tenant" in c["emails"][0]["body_text"] and "building" in c["emails"][0]["body_text"]
       and "resident" not in c["emails"][0]["body_text"]), True)
check("  the footer says why a commercial owner is getting it", "commercial property" in c["emails"][0]["body_text"])
check("  and quotes no price", "$" in " ".join(e["body_text"] for e in c["emails"]), False)
for e in c["emails"]:
    check("  step %d passes the leak guard" % e["step"], S.leaks_signal(e["body_text"], ignore=["Stone Oak Office Partners"]), [])
check("step days for commercial", S.step_days("commercial"), {1: 0, 2: 4, 3: 11, 4: 21})

print("— real estate agents —")
r = S.render("realtor", None, {"company": "Keller Williams Heritage, LLC", "city": "San Antonio"}, get)
check("realtor is its own 4-step sequence", (len(r["emails"]), r["angle_used"]), (4, None))
b1 = r["emails"][0]["body_text"]
check("  it talks about the option period and the inspection report", "option period" in b1 and "inspection report" in b1)
check("  the trial is worded without 'free'", "$10 a month after the first 30 days" in b1)
check("  no link set: the ask is a reply", "Reply and I will set you up" in b1)
check("  the deck's unsourced '1 in 4' statistic is not repeated", "1 in 4" in " ".join(e["body_text"] for e in r["emails"]), False)
check("  no inspection/appraisal claim", any(w in b1.lower() for w in ("licensed", "appraisal", "trec")), False)
check("  footer says why an agent is getting it", "you sell real estate" in b1)
r = S.render("realtor", None, {"first_name": "Ann", "company": "X", "realtor_link": "https://homerepair.tech/realtors"}, get)
check("with the partner page set, step 1 links to it", "Take a look: https://homerepair.tech/realtors" in r["emails"][0]["body_text"])
check("  and greets by name", r["emails"][0]["body_text"].startswith("Hi Ann,"))
check("step 2 carries the referral terms", "$50" in r["emails"][1]["body_text"] and "$100" in r["emails"][1]["body_text"])
check("no bare token survives in a subject", "{{" not in subj, True)

# ── the property-manager page ────────────────────────────────────────
print("— the PM page —")
pm = S.render("property_manager", "slow_response", dict(CTX, pm_link="https://homerepair.tech/pm/"), get)["emails"]
check("step 1 points at the PM page, before the ask",
      "The property manager side is at https://homerepair.tech/pm/ if you want to see it first." in pm[0]["body_text"]
      and pm[0]["body_text"].index("homerepair.tech/pm/") < pm[0]["body_text"].index("fifteen minutes"), True)
check("step 2 has the short form", "Details at https://homerepair.tech/pm/." in pm[1]["body_text"], True)
check("steps 3 and 4 don't repeat it", any("homerepair.tech/pm/" in e["body_text"] for e in pm[2:]), False)
for sig in S.B1_BY_SIGNAL:
    check("  every step-1 variant carries it (%s)" % sig,
          "homerepair.tech/pm/" in S.render("property_manager", sig, dict(CTX, pm_link="https://homerepair.tech/pm/"), get)["emails"][0]["body_text"], True)
nolink = S.render("property_manager", "slow_response", CTX, get)["emails"]
check("no page set: no link, no dangling sentence, no double spaces",
      ("homerepair.tech/pm" in nolink[0]["body_text"], "{{" in nolink[0]["body_text"], "  " in nolink[0]["body_text"].split("\n\n—")[0]), (False, False, False))
check("HOA and commercial emails never get the PM page",
      any("homerepair.tech/pm" in e["body_text"] for seg in ("hoa", "commercial") for e in S.render(seg, None, dict(CTX, pm_link="https://homerepair.tech/pm/"), get)["emails"]), False)

# ── no invented numbers, per HARD RULE 1 ─────────────────────────────
print("— no numbers the spec didn't supply —")
import re
# The realtor program's own prices (from Daniel's Home Intelligence deck):
# $10/month for the toolkit, $50 + $100 referral. Anything else is invented.
SUPPLIED = {"realtor": {"$10", "$50", "$100"}}
for seg, sig in combos:
    for e in S.render(seg, sig, CTX, get)["emails"]:
        body = e["body_text"].split("\n\nDaniel Watson")[0]     # copy only, not footer
        nums = [n.rstrip(",") for n in re.findall(r"\$[\d,]+|\b\d+(?:\.\d+)?%", body)]
        nums = [n for n in nums if n not in SUPPLIED.get(seg, set())]
        check("%-16s/%-18s step %d has no $ or %% the spec didn't supply" % (seg, sig or "-", e["step"]), nums, [])

# ── CAN-SPAM gaps are reported, not silently rendered ────────────────
print("— compliance —")
bare = lambda k, d="": {"sender_name": "D"}.get(k, d)
p = S.render("hoa", None, CTX, bare)["compliance_problems"]
check("missing entity and address both reported",
      len([x for x in p if not x.startswith("NOTE")]), 2)
check("and the unsubscribe fallback is flagged as advisory",
      any(x.startswith("NOTE") for x in p), True)
check("full settings + url reports nothing",
      S.render("hoa", None, CTX, get, "https://x/u/1")["compliance_problems"], [])
# Never ship a placeholder. With no URL the footer must carry a real opt-out.
nofoot = S.render("hoa", None, CTX, get)["emails"][0]["body_text"]
check("no-URL footer has no placeholder", "[SET" in nofoot, False)
check("no-URL footer carries a working opt-out", 'Reply "stop"' in nofoot, True)
sig = S.signature(get)
check("cell is labelled and comes first",
      sig.splitlines()[-1].startswith("(210) 810-7476 cell"), True)
check("office number still present", "office" in sig, True)
check("one number only isn't labelled twice",
      S.signature(lambda k,d="": {"sender_cell":"(210) 810-7476"}.get(k,d)
                  ).count("cell"), 1)

# ── a company's own name is not our marketing filler ─────────────────
print("— proper nouns —")
pom = {"first_name": "Sam", "company": "Peace of Mind Property Management",
       "city": "New Braunfels"}
body = S.render("property_manager", "slow_response", pom, get, "https://x/u/1")["emails"][0]["body_text"]
check("their name trips the naive scan", S.leaks_signal(body) != [], True)
check("and is clean once their name is excluded",
      S.leaks_signal(body, ignore=[pom["company"]]), [])
check("excluding their name does not blind the guard",
      S.leaks_signal("I noticed your reviews", ignore=[pom["company"]]) != [], True)

print("-" * 68)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
