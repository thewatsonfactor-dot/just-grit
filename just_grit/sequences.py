# -*- coding: utf-8 -*-
"""ProActive outreach sequences — the copy, loaded as data.

The design rule this file exists to enforce, from Daniel's spec:

    Use the review signal to CHOOSE the angle. Never let it into the email.

An email that says "I noticed your tenants have been complaining" tells a
property manager you have been reading their bad reviews and are opening with
their worst day. It also repeats a third party's factual assertion about their
business in writing. The signal is a routing input; the prospect never learns
it existed. `leaks_signal()` at the bottom enforces that, and the tests fail
the build if any template trips it.

WHY THESE ARE TEMPLATES AND NOT AN LLM CALL
The original spec was a system prompt for generating sequences at runtime.
That made sense when there was no copy. There is now finished, compliance-
checked copy, so generating it fresh per prospect would buy variability we do
not want and re-open every hallucination risk the prompt's own HARD RULES
existed to close. Templates are free, deterministic, diffable, and testable.
The prompt's rules survive here as tests instead of instructions.
"""
from __future__ import annotations
import re

# ── merge fields ─────────────────────────────────────────────────────
FALLBACKS = {"first_name": "", "company": "your team",
             "community": "your community", "city": "the area", "units": ""}


def fill(text: str, ctx: dict) -> str:
    def sub(m):
        k = m.group(1).strip()
        v = (ctx.get(k) or "").strip()
        return v or FALLBACKS.get(k, "")
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, text or "")


# ── the greeting ──────────────────────────────────────────────────────
# 89 of the first 94 HomeRepair emails opened "Hi there," because there was
# no name on file, and they went to info@ and leads@ inboxes. "Hi there" to a
# shared inbox reads as a blast no matter how good the body is. Without a
# name, address the company - "Hi Edwards Property Management team," or,
# for a board, "Hi Misty Oaks board," - which at least shows it was written
# to them. And the subject line gets the short name: "budget season at Misty
# Oaks", not "...at Misty Oaks Homeowners Association".
LEGAL_TAIL = re.compile(r"[,\s]*\b(llc|l\.l\.c\.|inc\.?|incorporated|corp\.?|corporation|co\.|ltd\.?|"
                        r"pllc|lp|llp|crmc|realtors?)\b\.?\s*$", re.I)
ASSOC_TAIL = re.compile(r"\s*\b(homeowners?'?|home owners?'?|property owners?'?|owners?'?|community|condominium|"
                        r"condo|townhomes?|master)?\s*(association|assn\.?|assoc\.?|hoa|poa|coa)\b"
                        r"(\s*,?\s*(inc\.?|llc))?\s*$", re.I)


CITY_TAIL = re.compile(r"\s*[-–—,]?\s*(of\s+)?(san antonio|new braunfels|austin|boerne|schertz|seguin|"
                       r"san marcos|hill country|south texas|central texas|texas|tx)\s*$", re.I)
PM_TAIL = re.compile(r"\s*(real estate\s*&\s*property management|&\s*property management)\s*$", re.I)


def short_name(company: str, kind: str = "") -> str:
    """'Misty Oaks Homeowners Association, Inc.' -> 'Misty Oaks';
    'PMI Birdy Properties, CRMC' -> 'PMI Birdy Properties';
    'Hance Realty Real Estate & Property Management' -> 'Hance Realty'."""
    n = (company or "").strip()
    for _ in range(4):
        n2 = LEGAL_TAIL.sub("", n).strip(" ,")
        n2 = CITY_TAIL.sub("", n2).strip(" ,-–—") if len(n2.split()) > 2 else n2
        n2 = PM_TAIL.sub("", n2).strip(" ,")
        if kind == "hoa":
            n2 = ASSOC_TAIL.sub("", n2).strip(" ,")
        if n2 == n or len(n2) < 3:
            break
        n = n2
    return n or (company or "").strip()


def greeting(ctx: dict, segment: str = "") -> str:
    first = (ctx.get("first_name") or "").strip()
    if first:
        return "Hi %s," % first
    seg = (segment or "").lower()
    if seg == "hoa":
        who = short_name(ctx.get("community") or ctx.get("company") or "", "hoa")
        return ("Hi %s board," % who) if who else "Hi there,"
    who = short_name(ctx.get("company") or "")
    return ("Hi %s team," % who) if who else "Hi there,"


# ── sequence A — HOA and condo boards ────────────────────────────────
# Timing note from the spec: boards set operating budgets Oct-Dec and vendor
# changes get approved into that budget. After January this is a next-year
# conversation. That is a real deadline, not a manufactured one.
SEQ_HOA = [
    {"step": 1, "day": 0,
     "subjects": ["budget season at {{community_short}}",
                  "common-area maintenance, next year",
                  "a question about your 2027 budget"],
     "preheader": "Budgets get set this fall, so it's an easy time to make a change.",
     "body": """{{greeting}}

I'm Daniel with HomeRepair.tech. We take care of common areas for HOA and condo communities around {{city}}. We walk the property every month, fix the small stuff while we're there, and send the board photos of everything.

Next year's budget is probably getting set about now, so this is when a change is easy to make.

If it would help, I can walk {{community}} and give the board a written scope and price to look at. It takes about an hour, and there's no commitment.

Want me to set up a time?"""},

    {"step": 2, "day": 4,
     "subjects": ["the part boards actually care about",
                  "what your board sees each month",
                  "documentation, not just the work"],
     "preheader": "What the board gets every month.",
     "body": """{{greeting}}

One more thing on the walkthrough I mentioned.

When a homeowner asks at a meeting what got fixed in March, the board should be able to answer. Everything we do gets a date and a photo, and the board gets a one-page summary each month.

I can send you a sample of that report so you can see it before deciding anything. Want me to?"""},

    # Day 11: something the board can use whether or not they ever call us.
    # The playbook's rule (Small Business plugin, sequence_patterns.md): the
    # one touch that asks for nothing is the one that most often gets read.
    {"step": 3, "day": 11,
     "subjects": ["the common-area checklist, no strings", "six things boards forget until they break",
                  "for your next walkthrough"],
     "preheader": "Keep it, use it, hand it to whoever does your walkthroughs.",
     "body": """{{greeting}}

No ask in this one. Here is the short common-area list we walk with, the items that turn into a special assessment when nobody is looking:

Irrigation controller reset each season, and a look for the one zone that is always running.
Entry and street-sign lighting - photocells fail quietly and nobody notices until a resident does.
Storm drain grates and swales cleared before the fall rains.
Pool equipment pad: pump seals, timer, chemical storage.
Playground hardware: bolts, s-hooks, surfacing depth.
Sidewalk trip hazards and fence line leaners.

Keep it, use it, hand it to whoever does your walkthroughs."""},

    {"step": 4, "day": 21,
     "subjects": ["closing this out", "last note from me", "no problem either way"],
     "preheader": "Last note from me.",
     "body": """{{greeting}}

I'll leave it here, since budget season is busy enough.

If the timing is just off, tell me a month to check back and I'll wait until then. If it's not a fit, no problem at all.

Good luck this year."""},
]

# ── sequence B — property management companies ───────────────────────
# Step 1 has three variants. The signal picks which one fires and is then
# discarded. Steps 2 and 3 are shared, so a wrong guess at step 1 costs one
# email, not the sequence.
B1_BY_SIGNAL = {
    # Daniel, 2026-09-28: lead with Snap It, Send It. The tenant sends the
    # request with a photo, the manager taps approve, we dispatch inside the
    # dollar limit he set. Preventive visits are the second line.
    "slow_response": {
        "subjects": ["tenant maintenance requests",
                     "a photo, an approve button, a tech",
                     "{{city}} maintenance, different angle"],
        "preheader": "Tenants send a photo, you tap approve, we send a tech.",
        "body": """{{greeting}}

I'm Daniel with HomeRepair.tech. We make maintenance requests easier on property managers. A tenant snaps a photo of the problem and sends it in. You tap approve, and we send a tech. We stay inside the dollar limit you set for each property.

We also handle the routine stuff on a schedule, like AC filters, water heaters and electrical checks, so fewer emergencies come up at all.

We use our own crews along I-35.{{pm_link_line}}

Would fifteen minutes be worth it to see if this fits how {{company}} works?"""},

    # The differentiator here is not "we keep records" - a landlord's own vendor
    # keeping records settles nothing. It is that an outside company documents
    # the unit at move-in and move-out and both sides see the same file. That is
    # what turns a deposit argument into two people looking at the same photos,
    # and it is the reason a property manager cares: they are in the middle of
    # every one of those fights.
    #
    # What this copy deliberately does NOT claim: that the record is legally
    # admissible, that it wins disputes, or that it prevents them. Those are
    # outcomes nobody can promise. It describes the mechanism and stops.
    "turnover_disputes": {
        "subjects": ["move-in and move-out condition",
                     "the deposit conversation",
                     "a third party in the middle"],
        "preheader": "Dated photos at move-in and move-out, taken by a third party.",
        "body": """{{greeting}}

I'm Daniel with HomeRepair.tech. By the time a deposit is in dispute, it's usually hard to find what the unit looked like at move-in or what got fixed during the lease.

We take care of that part. We photograph and date the unit's condition at move-in and again at move-out, and you and the tenant both get the same report. Since we're an outside company, everyone is looking at the same photos.{{pm_link_line}}

Would fifteen minutes be worth it to see if this fits how {{company}} handles turnover?"""},

    "vendor_quality": {
        "subjects": ["one crew, not a rotating list",
                     "vendor coverage in {{city}}",
                     "who actually shows up"],
        "preheader": "Our own crews along I-35, with a record for each property.",
        "body": """{{greeting}}

I'm Daniel with HomeRepair.tech. When a different tech shows up to every job, nothing carries over from the last visit and your team ends up explaining the property all over again.

We use our own crews along I-35 and keep a condition record for each property, so whoever shows up can see what was done before.{{pm_link_line}}

Would fifteen minutes be worth it to see if this fits {{company}}?"""},
}

# ── sequence C — commercial buildings ────────────────────────────────
# Daniel, 2026-09-25: "make sure we start offering services to HOAs and
# commercial buildings for maintenance." The buyer is the owner or manager
# of a small commercial building - an office, a retail strip, a medical
# office - whose tenants call THEM when the AC quits or a parking-lot light
# is out. The pitch is the same mechanism as residential: scheduled
# walkthroughs, a documented condition record per building, small items
# handled before they become tenant calls. No prices - commercial pricing is
# not in the catalog yet, so the copy asks for a walkthrough and stops.
SEQ_COMMERCIAL = [
    {"step": 1, "day": 0,
     "subjects": ["before the tenant calls",
                  "{{city}} building maintenance, different angle",
                  "a question about your buildings"],
     "preheader": "Catching problems before your tenants call about them.",
     "body": """{{greeting}}

I'm Daniel with HomeRepair.tech. When a tenant calls about a rooftop unit that quit or a leak over the weekend, it's usually something that could have been spotted weeks before.

We walk commercial buildings on a schedule, handle the small repairs while we're there, and keep a dated photo record for each address. You get one company to call instead of a different vendor for every problem.

Would fifteen minutes be worth it to see if this fits how {{company_short}} runs its buildings?"""},

    {"step": 2, "day": 4,
     "subjects": ["what scheduled maintenance looks like for your building", "one more thing on this", "how our building visits work"],
     "preheader": "How it works, in plain terms.",
     "body": """{{greeting}}

Following up on my note about your building. Here's how it works.

We walk the building and score its condition. Filters, roof drains, lighting and the outside go on a schedule. Every visit and repair gets logged to that address, so you can pull it up any time.

We'd start with one building, and you'd see the report before deciding anything. Want me to set that up?"""},

    # Day 11: something the building owner can use, no ask.
    {"step": 3, "day": 11,
     "subjects": ["six things that become a tenant call", "the building list, no strings",
                  "for your next roof walk"],
     "preheader": "Keep it, use it, hand it to whoever walks the roof.",
     "body": """{{greeting}}

No ask in this one. Here is the short list we walk with, the items that turn into a tenant call when nobody is looking:

Roof drains and scuppers cleared before storm season - ponding is where most roof claims start.
Rooftop unit filters every quarter, condensate lines flushed before summer.
Backflow preventer test date - the city wants it annually and the notice goes to whoever is on file.
Exterior and parking lighting photocells.
Fire extinguisher tags and exit-sign batteries.
Sidewalk and curb trip hazards, especially at the ADA route.

Keep it, use it, hand it to whoever walks the roof."""},

    {"step": 4, "day": 21,
     "subjects": ["closing the loop", "last one from me", "all good either way"],
     "preheader": "Last note from me.",
     "body": """{{greeting}}

This is my last note, I promise.

If the timing is just off, tell me a month to check back and I'll wait until then. If it's not a fit, no problem at all."""},
]

# ── sequence D — real estate agents and brokerages ──────────────────
# From the ProActive Home Intelligence deck (2026-09-25): agents lose deals
# and commission in the option period, when the buyer's inspection lands
# and there are three days to price the fixes. The partner toolkit is a
# $10/month tool - a 0-100 condition score, a photo-based repair estimate
# with local cost ranges, and a HomePassport record that transfers to the
# buyer at closing. Referral: $50 per client who joins, $100 more when
# their first job completes.
#
# What this copy will NOT say: "free" (the playbook rule - so the trial is
# "$10 a month after the first 30 days"), the deck's "1 in 4 deals
# renegotiate" statistic (unsourced), and anything that reads as a
# licensed inspection or appraisal. The link comes from the
# `realtor_page_url` setting; with none set, the ask is a reply.
SEQ_REALTOR = [
    {"step": 1, "day": 0,
     "subjects": ["the option period, before it starts",
                  "repair numbers inside three days",
                  "a question about your listings in {{city}}"],
     "preheader": "A repair estimate the same day the inspection report comes in.",
     "body": """{{greeting}}

I'm Daniel with HomeRepair.tech. When the inspection report comes in, you've got about three days to put a price on the repairs, and good contractors are booked out a week. So the number usually ends up being a guess.

We have a tool for that. You take photos of the items on the report and get a written repair estimate with Central Texas prices the same day, plus a condition score from 0 to 100. You can use it to negotiate under contract, or before listing to show the house was taken care of.

It's $10 a month after the first 30 days, for all your listings.

{{realtor_ask}}"""},

    {"step": 2, "day": 4,
     "subjects": ["the closing gift part", "what your buyer keeps", "one more thing on this"],
     "preheader": "What your buyer keeps after closing.",
     "body": """{{greeting}}

One more thing agents tend to like.

At closing, the buyer gets a record of the house with the score, photos, repairs and a 12-month maintenance calendar, and your name is on it. They keep it for years, so you stay in front of them.

If a client signs up for a home care plan through your link, we pay you $50, and another $100 when their first job is done.

Want me to set you up?"""},

    # Day 11: something an agent can use on the next listing, no ask.
    {"step": 3, "day": 11,
     "subjects": ["six things to ask a seller before listing", "the pre-listing list, no strings",
                  "before the inspector finds it"],
     "preheader": "The items that most often blow up an option period around here.",
     "body": """{{greeting}}

No ask in this one. The six items that most often turn a clean option period into a credit fight in Central Texas, worth asking a seller about before the sign goes up:

How old is the water heater, and when was it last flushed.
The AC: age, and does the condensate line drip outside in July.
Doors that stick or crack lines over windows - foundation questions get asked either way.
Roof age against the last hail event.
The electrical panel brand - inspectors flag certain older ones on sight.
Any plumbing leak that was fixed but never documented.

Keep it, use it on the next listing appointment."""},

    {"step": 4, "day": 21,
     "subjects": ["closing the loop", "last one from me", "no problem either way"],
     "preheader": "Last note from me.",
     "body": """{{greeting}}

This is my last note, I promise.

If you'd rather see it on a real house first, reply with an address you have under contract and I'll show you what the report would say. If it's not a fit, no problem at all."""},
]

# The spec lists six categories but ships copy for three. The other three map
# onto the closest written variant rather than silently falling through to a
# default - and `deferred_condition` is the spec's own fallback.
SIGNAL_ALIASES = {
    "deferred_condition": "slow_response",
    "communication":      "turnover_disputes",
    "inspection_repairs": "turnover_disputes",
}
DEFAULT_SIGNAL = "slow_response"

B_SHARED = [
    {"step": 2, "day": 4,
     "subjects": ["what preventive maintenance looks like on your properties", "one more thing on this", "how our property visits work"],
     "preheader": "How it works, in plain terms.",
     "body": """{{greeting}}

Following up on my note about the properties you manage. Here's how it works.

Tenants send requests with a photo, and you approve them in our system. Each property also gets visits on a schedule for filters, the water heater and electrical, plus a written health and safety report every quarter. At move-in and move-out we photograph the unit, so there's a third-party record of its condition.

We could start with one property.{{pm_details}}

Want me to set that up?"""},

    # Day 11: something the property manager can use, no ask.
    {"step": 3, "day": 11,
     "subjects": ["six things that become an emergency work order", "the rental list, no strings",
                  "for your maintenance tech"],
     "preheader": "Keep it, use it, hand it to your maintenance tech.",
     "body": """{{greeting}}

No ask in this one. Here is the short list we work rentals against, the items that turn into an after-hours work order when nobody is looking:

Water heater flushed once a year - the sediment in our water is what kills them early.
AC condensate line cleared every month May through September, the number one summer call.
Hose bib covers on before the first freeze, usually mid-November here.
Dryer vent cleared at every turn.
Braided toilet and sink supply lines replaced once they are old enough to stiffen.
Gutters and downspouts cleared in October, before the leaves.

Keep it, use it, hand it to your maintenance tech."""},

    {"step": 4, "day": 21,
     "subjects": ["closing the loop", "last one from me", "all good either way"],
     "preheader": "Last note from me.",
     "body": """{{greeting}}

This is my last note, I promise.

If the timing is just off, tell me a month to check back and I'll wait until then. If it's not a fit, no problem at all."""},
]


def resolve_signal(cat: str | None) -> str:
    cat = (cat or "").strip().lower()
    cat = SIGNAL_ALIASES.get(cat, cat)
    return cat if cat in B1_BY_SIGNAL else DEFAULT_SIGNAL


def parse_variant(variant: str | None):
    """('property_manager', 'slow_response') from 'seq:property_manager:slow_response'.

    The outreach row's own `variant` string is the only record of which
    sequence and angle a prospect is on - so the follow-up scheduler reparses
    it rather than carrying a second copy of the same fact. Returns
    (None, None) for anything that isn't one of ours.
    """
    parts = (variant or "").split(":", 2)
    if len(parts) == 3 and parts[0] == "seq":
        return parts[1], parts[2]
    return None, None


def step_days(segment, signal=None) -> dict:
    """{step_number: day_offset_from_step_1} for this sequence.

    The scheduler's only source of timing. If a step's `day` ever moves in
    the copy above, follow-ups move with it automatically instead of quietly
    drifting out of sync with a number hardcoded somewhere else.
    """
    return {s["step"]: s["day"] for s in steps_for(segment, signal)}


def steps_for(segment: str, signal: str | None = None) -> list:
    """The four steps for one prospect, angle already chosen: the trigger,
    how it works, something useful with no ask, a clean close."""
    if (segment or "").lower() in ("hoa", "condo", "hoa_board"):
        return list(SEQ_HOA)
    if (segment or "").lower() == "commercial":
        return list(SEQ_COMMERCIAL)
    if (segment or "").lower() == "realtor":
        return list(SEQ_REALTOR)
    b1 = B1_BY_SIGNAL[resolve_signal(signal)]
    return [{"step": 1, "day": 0, **b1}] + list(B_SHARED)


# ── signature and footer ─────────────────────────────────────────────
# Read from settings, never hardcoded. There are currently two different
# mailing addresses in play for this one LLC and this file is not the place
# to pick one - it renders what Settings holds and the caller reports the gap.

def signature(get) -> str:
    """Cell first. On a cold email the mobile is the number that gets answered,
    and a prospect who wants to talk now should not have to pick."""
    name  = get("sender_name", "Daniel Watson")
    site  = get("sender_site", "HomeRepair.tech")
    cell  = get("sender_cell", "").strip()
    phone = get("sender_phone", "").strip()
    nums = []
    if cell:
        nums.append("%s cell" % cell)
    if phone and phone != cell:
        nums.append("%s office" % phone)
    lines = [name, site]
    if nums:
        lines.append(" · ".join(nums))
    return "\n".join(lines)


def footer(get, unsubscribe_url: str = "", segment: str = "") -> tuple[str, list]:
    """Returns (text, problems). Problems are CAN-SPAM gaps, not style notes.

    There is no unsubscribe URL wired up yet, and printing "[SET unsubscribe
    URL]" into a real send is worse than any of the things this file exists to
    prevent. So it falls back to the reply-based opt-out the Watson Factor
    workspace already uses - a working mechanism, honoured by hand, not a
    placeholder. A one-click link is still better and stays on the list.
    """
    legal = get("legal_entity", "").strip()
    addr  = get("sender_address", "").strip()
    problems = []
    if not legal:
        problems.append("No legal entity name set. CAN-SPAM requires the sender be "
                        "identified.")
    if not addr:
        problems.append("No physical mailing address set. Required by law on every "
                        "commercial email.")
    if unsubscribe_url:
        opt_out = "Unsubscribe: %s" % unsubscribe_url
    else:
        opt_out = 'Reply "stop" and I\'ll take you off permanently.'
        problems.append("NOTE (not a blocker): no one-click unsubscribe URL, so this "
                        "falls back to reply-to-opt-out. Legal, and what Watson Factor "
                        "already does, but a link is better once there's a page for it.")
    why = ("you sell real estate in our service area" if segment == "realtor"
           else "you own or manage commercial property in our service area" if segment == "commercial"
           else "you serve on the board of a community in our service area" if segment == "hoa"
           else "you manage residential property in our service area")
    text = ("%s\n%s\n\n"
            "You're receiving this because %s.\n%s"
            % (legal or "[SET legal_entity IN SETTINGS]",
               addr or "[SET sender_address IN SETTINGS]", why, opt_out))
    return text, problems


def render(segment, signal, ctx, get, unsubscribe_url="") -> dict:
    """One prospect, four emails, ready to queue."""
    foot, problems = footer(get, unsubscribe_url, (segment or "").lower())
    sig = signature(get)
    ctx = dict(ctx or {})
    ctx.setdefault("greeting", greeting(ctx, segment))
    ctx.setdefault("company_short", short_name(ctx.get("company") or ""))
    ctx.setdefault("community_short", short_name(ctx.get("community") or ctx.get("company") or "", "hoa"))
    link = (ctx.get("realtor_link") or "").strip()
    ctx.setdefault("realtor_ask", ("Take a look: %s" % link) if link
                   else "Worth a look before your next option period? Reply and I will set you up.")
    # Daniel, 2026-09-25: "https://homerepair.tech/pm/ - include this for the
    # property managers." A sentence in step 1, three words in step 2, and
    # nothing at all when the page URL is blank in Setup.
    pm = (ctx.get("pm_link") or "").strip()
    ctx.setdefault("pm_link_line", (" The property manager side is at %s if you want to see it first." % pm) if pm else "")
    ctx.setdefault("pm_details", (" Details at %s." % pm) if pm else "")
    out = []
    for s in steps_for(segment, signal):
        body = fill(s["body"], ctx)
        out.append({
            "step": s["step"], "send_day": s["day"],
            "subject_options": [fill(x, ctx) for x in s["subjects"]],
            "preheader": fill(s["preheader"], ctx),
            "body_text": "%s\n\n%s\n\n—\n%s\n" % (body, sig, foot),
        })
    return {"segment": segment, "angle_used": (None if segment in ("hoa", "commercial", "realtor")
                                               else resolve_signal(signal)),
            "emails": out, "compliance_problems": problems}


# ── the guard ────────────────────────────────────────────────────────
# Anything here means the copy told the prospect we read their reviews, or
# claimed knowledge we cannot defend if they ask "how would you know that?"
LEAK_PATTERNS = [
    # Second person is what makes it a leak. The A2 board email says "a resident
    # complaint, an exterior repair" - a generic description of what comes up at
    # a board meeting, asserting nothing about this community. My first pass
    # banned the bare noun and flagged Daniel's own copy. The rule was never
    # "avoid the word"; it is "never claim to know THEIR reputation."
    (r"\byour\b[^.!?]{0,40}\b(reviews?|ratings?|complaints?|feedback|stars?)\b",
     "refers to their reviews/ratings/complaints"),
    (r"\b(reviews?|ratings?|complaints?|feedback)\b[^.!?]{0,25}\b(about|against|for)\s+(you|your|them|their)\b",
     "refers to feedback about them"),
    # "a resident complaint" is a noun compound describing what lands on a board
    # agenda. "your residents seem frustrated" is a claim about this community.
    # The difference is a possessive or a finite verb, so require one.
    (r"\b(your|their)\s+(tenants?|residents?|customers?|owners?)\b[^.!?]{0,30}"
     r"\b(complain\w*|unhappy|frustrat\w*|upset|dissatisfied)\b|"
     r"\b(tenants?|residents?|customers?|owners?)\s+"
     r"(have|has|are|were|been|keep|seem|sound|say)\b[^.!?]{0,30}"
     r"\b(complain\w*|unhappy|frustrat\w*|upset|dissatisfied)\b",
     "asserts how their people feel"),
    (r"\b\d(\.\d)?\s*stars?\b|\bstar rating\b|\bYelp\b|\bGoogle review",
     "names a rating or a review source"),
    (r"\bI noticed\b|\bI saw that\b|\bI(\'ve| have) been reading\b|"
     r"\bit looks like you(\'ve| have| are|\'re)\b|\bI can see (you|your)\b",
     "implies we have been looking at them"),
    (r"\bmany companies your size\b|\bcompanies like yours (struggle|often|tend)\b|"
     r"\bmost (firms|companies) your size\b",
     "fake-personalised filler"),
]
# Words the spec bans outright as marketing filler.
BANNED_WORDS = ["solution", "leverage", "streamline", "seamless", "unlock",
                "revolutionize", "peace of mind", "hope this finds you well",
                "quick question", "circling back"]


def leaks_signal(text: str, ignore=()) -> list:
    """Every reason this text must not be sent. Empty list means clean.

    `ignore` holds proper nouns that are theirs, not ours - the company name,
    the community name. There is a real property manager on the New Braunfels
    list called "Peace of Mind Property Management", and "peace of mind" is on
    the banned-filler list. Scanning the merged text without this would refuse
    to email a company because of its own legal name.
    """
    scan = text or ""
    for token in ignore:
        token = (token or "").strip()
        if len(token) >= 4:
            scan = re.sub(re.escape(token), " ", scan, flags=re.I)
    found = []
    for rx, why in LEAK_PATTERNS:
        if re.search(rx, scan, re.I):
            found.append(why)
    low = scan.lower()
    for w in BANNED_WORDS:
        if w in low:
            found.append("banned phrase: %s" % w)
    return found
