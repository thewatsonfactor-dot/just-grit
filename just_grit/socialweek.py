# -*- coding: utf-8 -*-
"""A finished week of posts, not a week of blanks.

Daniel, 2026-09-24, looking at the old "What to post this week": "I feel
like it needs a lot of work and improvement." It did. It handed him five
fill-in-the-blank captions ("[a tip your buyer can do this weekend]"),
signed them "Daniel Watson" instead of the business, promised a "free Home
Health Score" HomeRepair's own rules forbid, and left the picture, the
timing and the posting to him.

He chose "full autopilot": build the week, make the pictures, schedule it
to go out, and let him cancel anything he doesn't like. This module is the
content half - pure, no network, no database:

  plan(...)  five posts for one ISO week. Each one is complete: a caption
             that can go out as written, a picture description for the
             picture box, a day and a time, and the campaign it serves.
             Webapp makes the pictures, runs the caption through the AI
             writer when there is one (using `brief` as the idea), and
             schedules.

Every caption here is written to pass the business's rules as they stand:
no "free", no insurance, no prices except the campaign's own, no
diagnoses. tests/test_social_week.py checks every one of them.

The week is one tip, one offer, one question, one checklist, one local
post. No "behind the scenes" and no "customer review" post on autopilot:
an AI picture of "our crew" or an invented review would be a lie told to
the customers this is meant to win.
"""
from __future__ import annotations
import datetime as _dt
import hashlib

# kind, weekday (0=Mon), local time
SLOTS = [
    ("tip",       1, "12:00"),     # Tue lunch
    ("offer",     2, "18:30"),     # Wed after work
    ("question",  3, "19:00"),     # Thu evening scroll
    ("checklist", 4, "12:00"),     # Fri lunch, before the weekend
    ("local",     5, "09:30"),     # Sat morning, household decisions
]
DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LABEL = {"tip": "Helpful tip", "offer": "Your offer", "question": "Ask your followers",
         "checklist": "Quick checklist", "local": "About town"}


def season(month: int) -> str:
    return ("winter" if month in (12, 1, 2) else "spring" if month in (3, 4, 5)
            else "summer" if month in (6, 7, 8) else "fall")


# ── HomeRepair: homeowners in San Antonio / New Braunfels ────────────────
HOME = {
    "tips": {
        "fall": [
            ("Before you switch the thermostat to heat, change the air filter.",
             "The first cold morning is when most people flip to heat - and a clogged filter makes the "
             "system work harder all winter. It's a two-minute swap, and while you're there, write the "
             "date on the new filter's edge so you know when it went in.",
             "Close-up of hands sliding a clean new air filter into a hallway return vent in a bright Texas home, "
             "the old dusty filter leaning against the wall, soft natural light, photoreal, no text, no logos"),
            ("Test your smoke alarms when the clocks change.",
             "Clocks fall back the first Sunday in November - an easy day to remember. Press and hold the "
             "test button on every smoke alarm and carbon monoxide detector until it sounds. If one chirps "
             "on its own, it's asking for a new battery.",
             "A hand pressing the test button on a white ceiling smoke alarm in a sunny living room, "
             "shallow depth of field, photoreal, no text, no logos"),
            ("Feel for drafts around your doors.",
             "On a breezy day, run your hand along the edges of each outside door. Feel air moving? The "
             "weather-stripping is worn, and that's heat you're paying for leaving the house.",
             "Close-up of a hand running along the edge of a front door with new weather-stripping, "
             "late afternoon autumn light, Texas home, photoreal, no text, no logos"),
            ("Your dryer vent needs cleaning too - not just the lint trap.",
             "Lint builds up in the duct behind the dryer, where you never see it. Once a year, have the "
             "vent line cleared out. Loads dry faster, and a clogged dryer vent is a known fire risk.",
             "A laundry room with the dryer pulled slightly away from the wall showing the silver vent duct, "
             "clean and bright, photoreal, no text, no logos"),
            ("Unhook the garden hoses before the first freeze.",
             "A hose left attached can hold water that freezes back into the outdoor faucet. Disconnect "
             "hoses, drain them, and cover outside faucets before the first hard freeze of the season.",
             "A coiled green garden hose hanging on a hook beside an outdoor faucet with an insulated cover, "
             "brick wall of a Texas home, crisp autumn morning, photoreal, no text, no logos"),
            ("Clear the gutters once the leaves are down.",
             "Clogged gutters send rainwater over the edge and right down along your foundation. Once the "
             "trees drop their leaves, clear them out and make sure the downspouts point water away from the house.",
             "A clean gutter and downspout on a single-story Texas ranch home, oak leaves on the lawn, "
             "clear blue autumn sky, photoreal, no text, no logos"),
        ],
        "winter": [
            ("Know where your main water shut-off is - before you need it.",
             "If a pipe ever leaks, the first thing you'll want is to turn off the water. Find the main "
             "shut-off valve today, make sure it turns, and show everyone in the house where it is.",
             "A hand turning a main water shut-off valve near the foundation of a Texas home, "
             "morning light, photoreal, no text, no logos"),
            ("Hard freeze in the forecast? Here's the short list.",
             "Let faucets on outside walls drip, open the cabinet doors under sinks so warm air reaches the "
             "pipes, and cover outdoor faucets. Small steps, and they're a lot easier than dealing with a burst pipe.",
             "A kitchen sink with the cabinet doors open underneath and the faucet dripping slightly, "
             "frost on the window, cozy warm light, photoreal, no text, no logos"),
            ("Press the TEST button on your GFCI outlets.",
             "Those outlets with the two little buttons in kitchens and bathrooms should be tested about "
             "once a month. Press TEST - the power should cut off - then press RESET.",
             "Close-up of a finger pressing the test button on a white GFCI outlet in a clean kitchen "
             "backsplash, photoreal, no text, no logos"),
            ("Check your water heater's manual for a flush schedule.",
             "Sediment settles at the bottom of a tank water heater over time. Many manufacturers recommend "
             "draining a few gallons once a year - your manual will say how often for your model.",
             "A tidy garage corner with a tank water heater and a garden hose attached to its drain valve, "
             "clean and bright, photoreal, no text, no logos"),
        ],
        "spring": [
            ("Run every sprinkler zone and watch it.",
             "Before the heat sets in, turn on each zone for a minute and walk the yard. Broken heads and "
             "sprinklers watering the sidewalk are easy to spot now - and follow your city's watering schedule.",
             "A lawn sprinkler head spraying an arc of water over green grass in a Texas front yard, "
             "morning sun backlighting the droplets, photoreal, no text, no logos"),
            ("Give your AC some room to breathe before summer.",
             "Pull weeds and trim shrubs so there's about two feet of clear space around the outdoor unit, "
             "and put in a fresh filter inside. Your AC is about to do a lot of work.",
             "An outdoor AC condenser unit beside a Texas home with neatly trimmed shrubs and clear space "
             "around it, bright spring day, photoreal, no text, no logos"),
            ("Flip your ceiling fans for summer.",
             "Most ceiling fans have a small switch on the housing. In warm months, set the blades to spin "
             "counterclockwise so they push air down and the room feels cooler.",
             "A ceiling fan in a bright living room with a hand reaching to the direction switch on the "
             "motor housing, photoreal, no text, no logos"),
            ("Look at the caulk around your tub and shower.",
             "Cracked or peeling caulk lets water get behind the tile. If it's pulling away, it's time to "
             "scrape out the old bead and run a fresh one.",
             "Close-up of a fresh white caulk line along the edge of a bathtub and tile wall, clean bathroom, "
             "soft light, photoreal, no text, no logos"),
        ],
        "summer": [
            ("In a Texas summer, check your AC filter every month.",
             "When the AC runs all day, filters load up faster. Pull it out and hold it to the light - if "
             "you can't see through it, it's time for a new one.",
             "A person holding a dusty air filter up to a sunny window next to a clean new one, "
             "Texas home interior, photoreal, no text, no logos"),
            ("Is your AC drain line dripping outside?",
             "While the AC runs, there's usually a small pipe outside dripping water. If it's dry on a humid "
             "day, the line may be clogged - worth a look before water backs up inside.",
             "A small white PVC condensate pipe on the side of a Texas home with a few drops of water "
             "falling onto rocks, bright summer day, photoreal, no text, no logos"),
            ("Storm season: trim the branches over your roof.",
             "Branches hanging over the roof scrape shingles and drop leaves into the gutters, and in a "
             "storm they're the first thing to come down. Keep them trimmed back.",
             "A live oak tree neatly trimmed back from the roofline of a single-story Texas home, "
             "blue summer sky, photoreal, no text, no logos"),
            ("Keep a storm kit where you can find it in the dark.",
             "Flashlights, batteries, a phone power bank and a first-aid kit - in one bin, in one spot, "
             "that everyone in the house knows about.",
             "A plastic storage bin on a shelf holding flashlights, batteries, a power bank and a first-aid kit, "
             "neatly organized garage, photoreal, no text, no logos"),
        ],
    },
    "questions": [
        ("What's the one fix that's been on your list the longest?",
         "No judgment - everybody has one. Tell us yours below. 👇"),
        ("Be honest: when did you last change your AC filter?",
         "A) This month  B) This season  C) ...there's a filter?\nAnswer below - we won't tell anyone."),
        ("Which would you rather never do again?",
         "Clean the gutters, fix a running toilet, or patch a hole in the drywall. Pick one below. 👇"),
        ("What's the little thing in your house that bugs you every single day?",
         "A sticky door? A drip? A squeaky hinge? Tell us - we hear the best ones."),
        ("Homeowners: what do you wish someone had told you your first year in the house?",
         "Share it below. Somebody who just got their keys will thank you."),
        ("How many things on your honey-do list could be done in under an hour?",
         "Count them. We bet it's more than you think. 👇"),
    ],
    "checklists": {
        "fall": ("Your 10-minute fall checklist",
                 ["Swap the AC / furnace filter", "Test smoke and CO alarms",
                  "Check doors for drafts", "Unhook garden hoses before the first freeze",
                  "Clear leaves off the gutters"],
                 "Each one takes a few minutes - pick a Saturday morning and knock it out."),
        "winter": ("Your freeze-ready checklist",
                   ["Find the main water shut-off", "Cover outdoor faucets",
                    "Know which sinks are on outside walls", "Test the GFCI outlets",
                    "Keep a flashlight where you can find it"],
                   "Do it on a mild day, so you're not doing it in the cold."),
        "spring": ("Your spring home checklist",
                   ["Run and check every sprinkler zone", "Clear space around the outdoor AC unit",
                    "Put in a fresh filter", "Flip ceiling fans counterclockwise",
                    "Check the caulk in the tub and shower"],
                   "Knock it out before the heat shows up."),
        "summer": ("Your mid-summer home check",
                   ["Check the AC filter (monthly in this heat)", "Make sure the AC drain line is dripping outside",
                    "Trim branches away from the roof", "Put a storm kit together",
                    "Check that the gutters are clear"],
                   "Fifteen minutes now saves a hot afternoon later."),
    },
    "checklist_prompt": ("A clipboard with a handwritten home maintenance checklist on a kitchen counter next to "
                         "a mug of coffee and a pen, warm morning light in a Texas home, photoreal, the paper "
                         "shows only checkbox shapes and scribbles, no readable text, no logos"),
    "local": {
        "fall": ("That first cool morning in San Antonio hits different.",
                 "Windows open, coffee on the porch, and the AC finally gets a break. Enjoy it - and if the "
                 "house has been waiting on a few things all summer, this is the weather to get them done.",
                 "A front porch of a Texas home on a cool autumn morning, a coffee mug on a wooden rail, "
                 "live oak trees and soft golden light, photoreal, no text, no logos"),
        "winter": ("Hill Country winters are mild... until they aren't.",
                   "Most weeks you barely need a jacket, and then one hard freeze shows up. A little prep on a "
                   "nice day makes that night a lot less stressful.",
                   "A Hill Country home with a light frost on the lawn at sunrise, smoke from a chimney, "
                   "clear winter sky, photoreal, no text, no logos"),
        "spring": ("Bluebonnet season around New Braunfels.",
                   "The roadsides are blue, the mornings are perfect, and everyone's outside. Enjoy it - "
                   "spring is also the best time to get the house ready before the Texas heat arrives.",
                   "A field of Texas bluebonnets along a quiet Hill Country road on a sunny spring morning, "
                   "photoreal, no text, no logos"),
        "summer": ("Texas summer: triple digits and the AC running nonstop.",
                   "Stay cool out there. Keep the blinds closed in the afternoon, check on your neighbors, "
                   "and give your AC a little help with a clean filter.",
                   "A shady Texas backyard in summer with a ceiling fan turning on a covered patio and a glass "
                   "of iced tea on a table, bright heat outside, photoreal, no text, no logos"),
    },
    "offer_hooks": [
        "Your to-do list called. It would like a day off.",
        "That loose cabinet door isn't going to fix itself.",
        "Picture a Saturday with nothing on the honey-do list.",
        "Small jobs pile up. Here's how to stop the pile.",
        "What if the little fixes just... got done?",
        "Your house runs on a calendar. Most people never see it.",
        "Spring AC, summer water heater, fall gutters, winter freeze check. That's the whole year.",
        "The cheapest repair is the one that never happens.",
    ],
    "tags": "#HomeRepairTech #SanAntonio #NewBraunfels #HomeMaintenance",
    "soft_cta": "When you'd rather hand it off, we're here.",
}

# ── The Watson Factor: local business owners ─────────────────────────────
BIZ = {
    "tips": [
        ("Try calling your business from your own website.",
         "Open your site on your phone and tap your phone number. If it doesn't dial - if you'd have to "
         "copy it - some customers just won't bother. A tap-to-call link fixes it.",
         "A person's hand holding a phone showing a simple business website with a large call button, "
         "blurred work truck in the background, photoreal, no readable text, no logos"),
        ("Your Google Business Profile is your real front door.",
         "Check that your hours, phone number and photos are current. For a lot of customers, that "
         "listing is the first - and only - thing they see before they call.",
         "A small business owner at a shop counter updating a listing on a laptop, warm light, "
         "photoreal, no readable text, no logos"),
        ("Ask for the review while the customer is still smiling.",
         "The best time to ask is right when the job's done. Text them the direct link so it's one tap - "
         "the longer you wait, the less likely it happens.",
         "A tradesperson handing a phone back to a smiling homeowner at a front door, bright day, "
         "photoreal, no readable text, no logos"),
        ("Answer every review - the good ones and the bad ones.",
         "A short thank-you on a good review and a calm, helpful reply on a bad one both tell the next "
         "customer that someone's paying attention.",
         "A small business owner typing a reply on a phone at a cafe table, relaxed and friendly, "
         "photoreal, no readable text, no logos"),
        ("How long does your website take to load on a phone?",
         "If your homepage takes more than a few seconds on a phone, people leave before they ever see "
         "what you do. Test it on your own phone, on cellular, not Wi-Fi.",
         "A person waiting impatiently while looking at a phone on a busy sidewalk, "
         "photoreal, no readable text, no logos"),
        ("A missed call is a customer calling someone else.",
         "A lot of callers who hit voicemail don't leave a message - they call the next name on the list. "
         "Having every call answered, even after hours, keeps them with you.",
         "A contractor on a ladder with a phone buzzing in a tool belt pocket, sunny afternoon, "
         "photoreal, no readable text, no logos"),
    ],
    "questions": [
        ("How many calls do you think you missed last week while you were on a job?",
         "Take a guess below. Most owners are surprised by the real number. 👇"),
        ("If you could hand off one task tomorrow, what would it be?",
         "Tell us below - we're curious what eats up your week."),
        ("Where do most of your new customers find you?",
         "Google, referrals, or social? Answer below. 👇"),
        ("When did you last look at your own website on your phone?",
         "Be honest. Then go look. We'll wait. 😄"),
    ],
    "checklist": ("5-minute online checkup for your business",
                  ["Tap your phone number on your website - does it dial?",
                   "Check your Google hours are right", "Reply to your newest review",
                   "Load your site on your phone on cellular", "Call your own number after hours - what happens?"],
                  "Five minutes, and you'll know exactly where calls are slipping away."),
    "checklist_prompt": ("A notepad checklist on a desk beside a phone and a coffee cup in a small business office, "
                         "the paper shows only checkbox shapes and scribbles, no readable text, no logos, photoreal"),
    "local": ("San Antonio runs on small businesses.",
              "The plumber, the taqueria, the shop down the street - they're what make this town work. "
              "Tag a local business you'd send your best friend to. 👇",
              "A sunny San Antonio street with small shops and a food truck, people walking, "
              "photoreal, no readable text, no logos"),
    "offer_hooks": [
        "You're on a ladder. The phone rings. Now what?",
        "Your best customers might be calling while you're busy.",
        "What happens to a call you can't pick up?",
    ],
    "tags": "#SmallBusiness #SanAntonio #TheWatsonFactor",
    "soft_cta": "Want a hand with any of this? Message us.",
}

LIBRARIES = {"homerepair": HOME, "watson": BIZ}


def library_for(slug: str) -> dict | None:
    return LIBRARIES.get(slug)


def monday_of(week: str) -> _dt.date:
    """'YYYY-Www' -> that week's Monday."""
    y, w = week.split("-W")
    return _dt.date.fromisocalendar(int(y), int(w), 1)


def week_of(d: _dt.date) -> str:
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def _seed(week: str, salt: str = "") -> int:
    return int(hashlib.sha1((week + salt).encode()).hexdigest()[:8], 16)


def _tags(camp: dict | None, lib: dict) -> str:
    raw = ((camp or {}).get("hashtags") or "").strip() or lib["tags"]
    seen, out = set(), []
    for t in raw.replace(",", " ").split():
        t = "#" + t.lstrip("#")
        if t.lower() not in seen and len(t) > 1:
            seen.add(t.lower())
            out.append(t)
    return " ".join(out[:6])


def _cta(camp: dict | None, lib: dict) -> str:
    c = ((camp or {}).get("cta") or "").strip()
    if not c:
        return lib["soft_cta"]
    return c if c[-1] in ".!?" else c + "."


def plan(week: str, slug: str, biz: dict, campaigns: list, variant: int = 0) -> list[dict]:
    """Five finished posts for `week`. `campaigns` are the business's live
    campaigns (dicts from social_campaigns) - offers rotate through them.
    `variant` shifts every choice, for "swap this post"."""
    lib = library_for(slug)
    mon = monday_of(week)
    sea = season((mon + _dt.timedelta(days=3)).month)
    camps = [c for c in campaigns if c] or [None]
    s = _seed(week) + variant
    main = camps[s % len(camps)]
    name = biz.get("name") or "us"
    out = []
    for i, (kind, dow, at) in enumerate(SLOTS):
        day = mon + _dt.timedelta(days=dow)
        post = {"kind": kind, "label": LABEL[kind], "date": day.isoformat(), "day": DAY[dow],
                "time": at, "slot": "%s-%s" % (week, kind), "campaign_id": None,
                "brief": "", "caption": "", "picture_prompt": ""}
        camp = main
        if lib is None:                                   # a workspace with no library: campaigns only
            camp = camps[(s + i) % len(camps)]
            if not camp:
                continue
            kind = "offer"
        if kind == "tip":
            pool = lib["tips"][sea] if isinstance(lib["tips"], dict) else lib["tips"]
            title, body, prompt = pool[(s + 1) % len(pool)]
            post["caption"] = "%s\n\n%s\n\n%s\n\n%s" % (title, body, lib["soft_cta"], _tags(camp, lib))
            post["brief"] = "A genuinely useful tip. The tip: %s %s" % (title, body)
            post["picture_prompt"] = prompt
        elif kind == "question":
            q, follow = lib["questions"][(s + 2) % len(lib["questions"])]
            post["caption"] = "%s\n\n%s\n\n%s" % (q, follow, _tags(camp, lib))
            post["brief"] = ("A question post to get comments. Ask exactly this, in a friendly way, and "
                             "invite answers: %s %s No selling in this one." % (q, follow))
            pool = lib["tips"][sea] if isinstance(lib["tips"], dict) else lib["tips"]
            post["picture_prompt"] = pool[(s + 1 + len(pool) // 2) % len(pool)][2]   # never the tip's picture
            camp = None
        elif kind == "checklist":
            ck = lib["checklists"][sea] if "checklists" in lib else lib["checklist"]
            title, items, close = ck
            lines = "\n".join("✔ " + x for x in items)
            post["caption"] = "%s\n\n%s\n\n%s\n\n%s\n\n%s" % (title, lines, close, _cta(camp, lib), _tags(camp, lib))
            post["brief"] = ("A short checklist post. Title: %s. Items: %s. Keep the items as a checklist "
                             "with ✔ marks." % (title, "; ".join(items)))
            post["picture_prompt"] = lib["checklist_prompt"]
        elif kind == "local":
            lo = lib["local"][sea] if isinstance(lib["local"], dict) and sea in lib["local"] else lib["local"]
            head, body, prompt = lo
            post["caption"] = "%s\n\n%s\n\n%s - %s\n\n%s" % (head, body, lib["soft_cta"], name, _tags(camp, lib))
            post["brief"] = ("A friendly local post about the season in %s - about the place, not a sales "
                             "pitch. The idea: %s %s" % ((biz.get("city") or "town").split(",")[0], head, body))
            post["picture_prompt"] = prompt
            camp = None
        else:                                             # offer
            if lib is not None:
                camp = camps[(s + 1) % len(camps)] if len(camps) > 1 else main
            hooks = (lib or HOME)["offer_hooks"]
            hook = hooks[(s + i) % len(hooks)]
            if camp:
                parts = [hook, (camp.get("offer") or "").strip().rstrip(".") + ".",
                         (camp.get("details") or "").strip(), _cta(camp, lib or HOME)]
                post["picture_prompt"] = (camp.get("picture_idea") or "").strip()
            else:
                parts = [hook, (biz.get("sells") or "").strip().rstrip(".") + ".", (lib or HOME)["soft_cta"]]
            post["caption"] = "\n\n".join(p for p in parts if p.strip(". ")) + "\n\n" + _tags(camp, lib or HOME)
            post["brief"] = "An offer post. Open with a hook like: \"%s\". Then the campaign." % hook
            if not post["picture_prompt"]:
                pool = (lib or HOME)["tips"]
                pool = pool["fall"] if isinstance(pool, dict) else pool
                post["picture_prompt"] = pool[s % len(pool)][2]
        post["kind"] = kind
        post["label"] = LABEL[kind]
        post["campaign_id"] = (camp or {}).get("id")
        post["campaign"] = (camp or {}).get("name") or ""
        out.append(post)
    return out
