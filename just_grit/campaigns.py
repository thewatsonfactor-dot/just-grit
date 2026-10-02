# -*- coding: utf-8 -*-
"""Campaign briefs, grounded in what we actually know about the business.

The old campaign tab was a client-side calculator with four text boxes. It
asked Daniel to type what the business sells - for a business the app had
already scanned, read the reviews of, and classified. And it projected revenue
from a hardcoded $8,400 ticket for everybody, then printed a ROAS to one
decimal place. Precision like that, invented, is worse than no number: it gets
believed.

This module does three things instead:

  readiness()  - can this business be advertised for at all, right now
  economics()  - what do WE actually know about ticket size and close rate
  brief()      - a plan specific enough to type into Ads Manager

The refusal is the feature. Sending paid traffic to a site with no form, no
phone link and no pixel doesn't produce leads, it produces a bill - and the
list of what to fix first is itself the engagement.
"""
from __future__ import annotations
import json, re

# ── readiness ────────────────────────────────────────────────────────
# A blocker means money spent is money lost, not money spent inefficiently.
# The distinction is the whole point: warnings cost you conversion rate,
# blockers cost you the entire budget.

BLOCKERS = {
    "no_https": ("The site isn't on HTTPS",
                 "Google Ads restricts insecure destinations and browsers warn on them. "
                 "Every click lands on a warning screen.",
                 "Install a certificate and force HTTPS. Usually free and same-day."),
    "no_viewport": ("The site isn't mobile-ready",
                    "Most local paid clicks are phones. A desktop-only page on a phone "
                    "bounces before it reads.",
                    "Add a responsive viewport and test on a real phone."),
    "noindex": ("The site tells search engines to stay away",
                "A noindex tag alongside a paid campaign is a contradiction worth "
                "resolving before spending.",
                "Remove the noindex, or confirm it's deliberate and only on some pages."),
}

# These two are computed rather than keyed, because "no way to convert" is a
# combination and "no measurement" isn't a finding at all.
NO_WAY_IN = ("There's nowhere for a click to go",
             "No contact form and no tappable phone number. Traffic arrives and has "
             "no action available to it - the budget buys visits and nothing else.",
             "Add a 3-field form (name, phone, need) and a tap-to-call number in the "
             "header. This is the single highest-value hour on the list.")

NO_MEASUREMENT = ("Nothing is measuring conversions",
                  "No analytics and no ad pixel on the page. Without them the campaign "
                  "cannot be optimised, cannot be attributed, and cannot be proven to "
                  "have worked - you'd be buying clicks you can't count.",
                  "Install GA4 and the pixel for whichever platform you run, and define "
                  "the conversion (form submit, call tap) before the first dollar.")

WARNINGS = {
    "no_reviews_shown": ("No reviews on the page",
                         "Paid traffic is colder than organic - it needs proof faster."),
    "slow_load":        ("The page is slow",
                         "Load time is both a Quality Score input and a bounce cause, so "
                         "it costs twice."),
    "cta_buried":       ("The call to action is buried",
                         "A paid visitor gives you one screen."),
    "no_service_area":  ("No service area named on the page",
                         "Geo-targeted ads to a page that never names the city read as "
                         "a mismatch to both the visitor and the platform."),
    "form_only_contact":("A form is the only way in",
                         "For an urgent local job the call IS the conversion."),
    "no_tap_to_call":   ("The phone number isn't tappable",
                         "On mobile that's a conversion lost to a copy-paste."),
}


def readiness(prospect: dict) -> dict:
    """Blockers, warnings, and an honest statement of what we could not check.

    A scan that predates the gate has no stored keys, so this reports "unknown"
    rather than "ready". Silence is not a pass.
    """
    gate = {}
    raw = prospect.get("stack") or ""
    if raw:
        try:
            gate = json.loads(raw)
        except (ValueError, TypeError):
            gate = {}

    state = (prospect.get("scan_state") or "").lower()
    if state != "done":
        return {"assessable": False, "ready": False, "blockers": [], "warnings": [],
                "why": "This site hasn't been scanned successfully%s. Nothing can be "
                       "planned against a page nobody has read."
                       % (" (%s)" % state if state else "")}
    if not gate.get("scanned_at"):
        return {"assessable": False, "ready": False, "blockers": [], "warnings": [],
                "why": "This scan predates campaign readiness, so the pixel and mobile "
                       "checks weren't recorded. Re-scan the site and the gate will "
                       "answer properly."}

    keys = set(gate.get("keys") or [])
    blockers, warnings = [], []

    for k, (title, why, fix) in BLOCKERS.items():
        if k in keys:
            blockers.append({"key": k, "title": title, "why": why, "fix": fix})

    # Nowhere to convert: no form AND no way to call.
    no_form = "no_form" in keys
    no_call = ("no_tap_to_call" in keys) or ("no_phone" in keys)
    if no_form and no_call:
        t, w, f = NO_WAY_IN
        blockers.append({"key": "no_conversion_path", "title": t, "why": w, "fix": f})

    # Nothing counting. `analytics`/`retargeting` are False when the scan ran
    # and found none - distinct from None, which means it never looked.
    if gate.get("analytics") is False and gate.get("retargeting") is False:
        t, w, f = NO_MEASUREMENT
        blockers.append({"key": "no_measurement", "title": t, "why": w, "fix": f})

    for k, (title, why) in WARNINGS.items():
        if k in keys and not any(b["key"] == "no_conversion_path" for b in blockers):
            warnings.append({"key": k, "title": title, "why": why})

    rating = prospect.get("rating")
    if rating is not None and rating < 4.0:
        warnings.append({"key": "low_rating",
                         "title": "Rating is %.1f" % rating,
                         "why": "Paid clicks check Google before they call. Under 4.0 the "
                                "ad pays to send people to a bad first impression."})

    return {"assessable": True, "ready": not blockers,
            "blockers": blockers, "warnings": warnings,
            "why": ("Ready to plan." if not blockers else
                    "%d thing%s must be fixed before any budget goes out."
                    % (len(blockers), "" if len(blockers) == 1 else "s"))}


# ── economics ────────────────────────────────────────────────────────

def economics(conn) -> dict:
    """Ticket size and close rate from the deals actually closed.

    Returns None for anything the data cannot support, and says why. The old
    calculator assumed $8,400 for every business on earth; the honest version
    of that is to admit when three wins is three wins.
    """
    won = conn.execute("SELECT COUNT(*), IFNULL(AVG(amount),0), IFNULL(SUM(amount),0), "
                       "IFNULL(SUM(mrr),0) FROM deals WHERE state='won'").fetchone()
    n_won, avg_amt, sum_amt, sum_mrr = won[0], won[1], won[2], won[3]
    n_lost = conn.execute("SELECT COUNT(*) FROM deals WHERE state='lost'").fetchone()[0]
    n_open = conn.execute("SELECT COUNT(*) FROM deals WHERE state='open'").fetchone()[0]

    notes = []
    ticket = round(avg_amt) if n_won else None
    if n_won == 0:
        notes.append("No won deals recorded yet, so there is no ticket size to plan "
                     "against. Log the closes and this fills itself in.")
    elif n_won < 5:
        notes.append("Average ticket is from %d win%s. That's a real number but not yet "
                     "a reliable one - treat it as a starting assumption, not a forecast."
                     % (n_won, "" if n_won == 1 else "s"))

    close_rate = None
    if n_lost == 0 and n_won > 0:
        notes.append("No lost deals are recorded, so a close rate can't be computed - "
                     "%d wins and zero losses would read as 100%%, which isn't true. "
                     "Start logging the passes and this becomes real." % n_won)
    elif n_won + n_lost >= 10:
        close_rate = round(n_won / float(n_won + n_lost), 3)
    elif n_won + n_lost > 0:
        notes.append("Only %d closed deals on record. A close rate needs about ten "
                     "before it means anything." % (n_won + n_lost))

    return {"n_won": n_won, "n_lost": n_lost, "n_open": n_open,
            "avg_ticket": ticket, "won_value": round(sum_amt),
            "mrr": round(sum_mrr), "close_rate": close_rate, "notes": notes}


# ── the brief ────────────────────────────────────────────────────────

CHANNELS_BY_VERTICAL = {
    "contractor":  [("Google Search", "Somebody with a leak types it into Google. This is "
                                      "demand that already exists - capture beats create."),
                    ("Google Local Services", "Pay per lead, not per click, and it sits "
                                      "above the ads. Needs licence and insurance on file."),
                    ("Meta", "Retargeting only at first - people who already hit the site. "
                             "Cold prospecting for urgent trades is the expensive way in.")],
    "restaurant":  [("Meta", "Radius targeting with real photos. Discovery beats search "
                             "here - nobody googles a restaurant they've never heard of."),
                    ("Google Search", "Brand terms and 'near me' only. Defends the name "
                             "against aggregators bidding on it."),
                    ("Google Business Profile", "Free, and usually outperforms both. Fix "
                             "it before paying for anything.")],
    "auto":        [("Google Search", "High intent, tight radius, business-hours only."),
                    ("Meta", "Offer-led creative to a 5-mile fence.")],
    "appointment": [("Google Search", "Condition and service terms, not brand."),
                    ("Meta", "Lookalike from the existing patient list, 1%.")],
    "generic":     [("Google Search", "Start where the intent already is."),
                    ("Meta", "Retargeting first, prospecting only once something converts.")],
}

NEGATIVES_UNIVERSAL = ["free", "cheap", "diy", "how to", "salary", "jobs", "job",
                       "career", "careers", "hiring", "training", "course", "school",
                       "wikipedia", "reddit", "youtube", "used", "second hand",
                       "complaints", "lawsuit", "scam", "near me jobs"]

NEGATIVES_BY_VERTICAL = {
    "contractor":  ["parts", "supply", "wholesale", "rental", "permit cost", "license"],
    "restaurant":  ["recipe", "menu prices", "calories", "coupon code", "delivery driver"],
    "auto":        ["parts", "junkyard", "salvage", "for sale", "insurance quote"],
    "appointment": ["medicaid", "free clinic", "walk in free", "symptoms"],
    "generic":     [],
}


def _geo(p):
    for k in ("territory", "market", "city"):
        v = (p.get(k) or "").strip()
        if v:
            return v
    return ""


def _proof_points(p):
    """Ad copy has to be true. These are the only claims we can support."""
    out = []
    r, n = p.get("rating"), p.get("reviews")
    if r and n and n >= 10:
        out.append("%.1f stars across %d Google reviews" % (r, n))
    geo = _geo(p)
    if geo:
        out.append("Local to %s - say the city in the headline" % geo)
    # NOT p["offers"] - that column holds what The Watson Factor would sell THEM
    # ("Ordering App", "AI Vision"), not what they sell their own customers.
    # Putting our product names into their ad copy as their services is the same
    # class of error as telling them they already own cameras.
    out.append("Their own service list isn't on file. Get it from them before "
               "writing headlines - the ad has to name what they actually sell.")
    if len(out) == 1:
        out.insert(0, "No verifiable proof points on file yet. Do not write claims the "
                       "business cannot substantiate - get the review count first.")
    return out


def _test_plan(econ):
    """How to size the first test, without inventing a cost-per-lead.

    Nobody knows the CPC for these keywords in this city until it runs. What
    IS knowable is how much traffic it takes before a conversion rate means
    anything: roughly 100 clicks to see whether a page converts at all, and
    about 30 conversions before optimising toward one. Those are rules of
    thumb and are labelled as such.
    """
    steps = [
        "Pull the real CPC range for your keyword list in Google Keyword Planner "
        "before committing a budget. It is free and it is specific to this city.",
        "Size the first flight at roughly 100 clicks. Below that a conversion rate is "
        "noise - the same problem the email A/B test has at ten sends a day.",
        "Set the kill criterion in writing before launch: if 100 clicks produce zero "
        "conversions, the landing page is the problem, not the bid.",
        "Do not touch bids or audiences for the first two weeks. Every change restarts "
        "the learning and you end up with four half-tests instead of one result.",
    ]
    if econ.get("avg_ticket"):
        steps.append("Your average win is $%s. That is the ceiling on what a lead can be "
                     "worth - work backwards from it, and remember it comes from %d deal%s."
                     % (f"{econ['avg_ticket']:,}", econ["n_won"],
                        "" if econ["n_won"] == 1 else "s"))
    else:
        steps.append("There is no won-deal average on file, so there is no defensible "
                     "target cost per lead yet. Run the test to find out what a lead "
                     "costs, then decide if it's worth it.")
    return steps


def brief(prospect: dict, econ: dict) -> dict:
    """The plan. Only produced when readiness passes."""
    v = (prospect.get("vertical") or "generic").lower()
    if v not in CHANNELS_BY_VERTICAL:
        v = "generic"
    geo = _geo(prospect)
    negatives = NEGATIVES_UNIVERSAL + NEGATIVES_BY_VERTICAL.get(v, [])

    landing = []
    try:
        for f in json.loads(prospect.get("findings") or "[]"):
            if f.get("severity") in ("serious", "warning") and f.get("fix"):
                landing.append({"do": f["fix"], "because": f.get("title") or ""})
    except (ValueError, TypeError):
        pass

    return {
        "company": prospect.get("company"),
        "vertical": v,
        "geography": geo or "(no service area on file - set one before targeting)",
        "channels": [{"name": n, "why": w} for n, w in CHANNELS_BY_VERTICAL[v]],
        "proof_points": _proof_points(prospect),
        "negative_keywords": negatives,
        "landing_page_fixes": landing[:6],
        "test_plan": _test_plan(econ),
        "economics": econ,
    }
