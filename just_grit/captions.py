# -*- coding: utf-8 -*-
"""Post text that writes itself - around a campaign, from the picture.

Daniel, 2026-09-24: "auto gen the description for the post, and maybe we
should make an area where we can select a campaign and then it makes the
post in regards to the campaign."

A campaign is a promo the business sets up once: what it is, the facts
(prices, what's included), who it's for, the call to action, the link and
the hashtags. Pick one and every post is written around it.

Two writers, one answer:

  AI        the workspace's own image-provider key (Gemini, Meta or an
            OpenAI-compatible host) looks at the actual picture plus the
            campaign and writes the caption. A fraction of a cent.
  template  fill-in patterns from the same campaign fields. Free, instant,
            and what you get when there is no key, the provider is down, or
            the AI's draft breaks a rule twice.

THE RULES ARE CODE, NOT A SUGGESTION. HomeRepair's playbook forbids "free",
"covered by insurance" and any quoted repair price. A model told not to say
"free" will still say it one time in twenty, so the draft is checked after
it comes back: banned words, and any dollar figure that is not one of the
business's own real prices. One retry with the problem named, then the
template. A caption that could put the business in trouble never reaches
the post box looking finished.

Pure: no database. webapp.py stores campaigns and passes them in; the
network goes through one injectable `http`, the same one imagegen uses.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re

import imagegen

MAX_CAPTION = 2200            # Instagram's hard limit
MAX_TAGS = 8                  # 30 is the limit; 3-8 is what reads as human

# ── campaigns ────────────────────────────────────────────────────────────
FIELDS = ("name", "offer", "details", "audience", "cta", "link", "hashtags",
          "picture_idea", "starts", "ends")

# What each workspace starts with, so the dropdown is useful on day one.
# HomeRepair's are the real Protect ladder from the D2D playbook - no
# invented prices, and nothing that says "free" (the playbook forbids it).
# The catalog changes on 2026-10-01 (Daniel's Replit spec, 2026-09-25):
# Basic $29, Essential $59 (the hero - four seasonal visits), Pro $179, all
# +$49 setup waived on annual prepay; a $49 Home Health & Safety Audit and a
# $79 HomePassport report as one-time products. Until then the September
# ladder stands, so both are kept and `starters_for()` picks by date.
CATALOG_EFFECTIVE = "2026-10-01"
CATALOG_VERSION = "2026-10"
# September campaign names that carry a new name in October (the offer changed).
RENAMED = {"Under an hour? It's included": "Four seasons, one plan"}

STARTERS_2026_09 = {
    "homerepair": [
        {"name": "Under an hour? It's included",
         "offer": "Protect Essential - $99 a month",
         "details": ("One tech visit every month with up to 60 minutes of labor included. "
                     "Priority scheduling. $100 in credits every 6 months. Month-to-month, "
                     "cancel anytime. Anything past the hour is $95/hr, and only after you "
                     "approve it."),
         "audience": "Homeowners in San Antonio and New Braunfels with a running list of small fixes",
         "cta": "See the plans at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#HomeRepairTech #SanAntonio #NewBraunfels #HomeMaintenance",
         "picture_idea": ("A friendly technician in a dark work shirt tightening a cabinet hinge "
                          "in a bright San Antonio kitchen, homeowner smiling in the background, "
                          "natural light, photoreal, no text")},
        {"name": "Fall home checklist",
         "offer": "Get the fall list handled on your monthly visit",
         "details": ("Fall is when Texas homes switch from AC to heat. Air filters, "
                     "weather-stripping, smoke detector batteries, a gutter check, the dryer "
                     "vent - small jobs that fit inside the up-to-60-minute monthly visit on "
                     "Protect Essential, $99 a month."),
         "audience": "Homeowners getting ready for cooler weather",
         "cta": "Book your first visit at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#FallChecklist #HomeRepairTech #SanAntonio #NewBraunfels",
         "picture_idea": ("A technician replacing an HVAC air filter in the hallway of a Texas "
                          "home, warm autumn light through the window, photoreal, no text"),
         "starts": "2026-09-15", "ends": "2026-11-30"},
        {"name": "Eyes on your home - $29/mo",
         "offer": "Protect Basic - $29 a month",
         "details": ("Your Home Intelligence Score, AI risk monitoring, and a HomePassport "
                     "that keeps your home's history in one place. $50 in credits every 6 "
                     "months. No visits on Basic. Month-to-month."),
         "audience": "Homeowners and renters who want to know what's coming before it breaks",
         "cta": "Start at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#HomeRepairTech #HomeOwnership #SanAntonio",
         "picture_idea": ("A homeowner on the back porch of a Texas home at golden hour, "
                          "looking at a phone and smiling, house in soft focus, photoreal, no text")},
        {"name": "Rental owners & property managers",
         "offer": "Protect Pro - $199 a month per property",
         "details": ("Up to 2 visits a month, 60 minutes of labor per visit, a dedicated "
                     "account manager, $300 in credits every 6 months. Make-readies and "
                     "turns are quoted at $95/hr before any work starts."),
         "audience": "Landlords and property managers in San Antonio and New Braunfels",
         "cta": "Email info@homerepair.tech to set up your properties",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#PropertyManagement #SanAntonioRentals #HomeRepairTech",
         "picture_idea": ("A row of well-kept single-family rental homes on a sunny San Antonio "
                          "street, a service van parked out front, photoreal, no text")},
    ],
}

STARTERS = {
    "homerepair": [
        {"name": "Four seasons, one plan",
         "offer": "Protect Essential - $59 a month",
         "details": ("Four seasonal care visits a year: spring AC check, summer water heater, "
                     "fall gutters, winter freeze check. Member labor rate of $75/hr instead of "
                     "$95. A $50 repair credit every 6 months, good on repair jobs of $350 or more. "
                     "$49 setup fee, waived when you prepay the year at $588. Cancel anytime."),
         "audience": "Homeowners in San Antonio and New Braunfels who want the house looked after on a schedule",
         "cta": "See the plans at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#HomeRepairTech #SanAntonio #NewBraunfels #HomeMaintenance",
         "picture_idea": ("A friendly technician in a dark work shirt checking an outdoor AC unit "
                          "beside a bright Texas home, homeowner smiling on the porch, natural "
                          "light, photoreal, no text")},
        {"name": "Fall home checklist",
         "offer": "Get the fall list handled on your fall care visit",
         "details": ("Fall is when Texas homes switch from AC to heat. Gutters, air filters, "
                     "weather-stripping, smoke detector batteries, the dryer vent - the fall "
                     "visit on Protect Essential covers it, $59 a month with member labor at "
                     "$75/hr for anything bigger."),
         "audience": "Homeowners getting ready for cooler weather",
         "cta": "Book your fall visit at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#FallChecklist #HomeRepairTech #SanAntonio #NewBraunfels",
         "picture_idea": ("A technician replacing an HVAC air filter in the hallway of a Texas "
                          "home, warm autumn light through the window, photoreal, no text"),
         "starts": "2026-09-15", "ends": "2026-11-30"},
        {"name": "Eyes on your home - $29/mo",
         "offer": "Protect Basic - $29 a month",
         "details": ("Your 0-100 Home Intelligence Score, a digital HomePassport that keeps "
                     "your home's history in one place, member labor at $85/hr instead of $95, "
                     "and a $25 repair credit every 6 months on jobs of $250 or more. $49 setup "
                     "fee, waived when you prepay the year at $276. No visits on Basic."),
         "audience": "Homeowners and renters who want to know what's coming before it breaks",
         "cta": "Start at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#HomeRepairTech #HomeOwnership #SanAntonio",
         "picture_idea": ("A homeowner on the back porch of a Texas home at golden hour, "
                          "looking at a phone and smiling, house in soft focus, photoreal, no text")},
        {"name": "Rental owners & property managers",
         "offer": "Protect Pro - $179 a month per property",
         "details": ("Twelve monthly concierge visits a year, two gutter cleans, same-day "
                     "emergency priority, and a dedicated home manager. Member labor at $65/hr "
                     "instead of $95, and a $150 repair credit every 6 months on jobs of $500 or "
                     "more. $49 setup fee, waived when you prepay the year at $1,788. Turns are "
                     "quoted before any work starts."),
         "audience": "Landlords and property managers in San Antonio and New Braunfels",
         "cta": "Email info@homerepair.tech to set up your properties",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#PropertyManagement #SanAntonioRentals #HomeRepairTech",
         "picture_idea": ("A row of well-kept single-family rental homes on a sunny San Antonio "
                          "street, a service van parked out front, photoreal, no text")},
        {"name": "$49 Home Health & Safety Audit",
         "offer": "A $49 Home Health & Safety Audit",
         "details": ("A technician walks your home and checks the systems that fail quietly - "
                     "plumbing, electrical panel, HVAC, drain lines, safety items - and you get a "
                     "written report with a 0-100 score. $49, and the fee is waived if you join "
                     "an annual plan at booking or on the day."),
         "audience": "Homeowners who want to know what the house is hiding before it becomes a bill",
         "cta": "Book your audit at homerepair.tech/checkyourhomescore",
         "link": "https://homerepair.tech/checkyourhomescore",
         "hashtags": "#HomeRepairTech #HomeInspection #SanAntonio #NewBraunfels",
         "picture_idea": ("A technician with a tablet checking an electrical panel in a tidy "
                          "Texas garage, bright and calm, photoreal, no text")},
        {"name": "HomePassport report - $79",
         "offer": "A HomePassport Property Report - $79, one time",
         "details": ("A verified 0-100 property health score, a photo vault of the home's "
                     "systems and repairs, and a condition record that follows the house - "
                     "the thing to hand a buyer, an appraiser, or your future self. $79, one time."),
         "audience": "Sellers getting ready to list, buyers in the option period, and owners who want the record",
         "cta": "Order yours at homerepair.tech/plans",
         "link": "https://homerepair.tech/plans",
         "hashtags": "#HomeRepairTech #HomePassport #SanAntonioRealEstate",
         "picture_idea": ("A neat folder of home documents and a phone showing a green 0-100 "
                          "score gauge on a kitchen counter, soft morning light, photoreal, no text")},
    ],
    "watson": [
        {"name": "Never miss a call",
         "offer": "An AI receptionist that answers when you can't",
         "details": ("Answers every call, day or night. Takes the caller's name, number and "
                     "what they need, and texts it to you right away. A missed call gets an "
                     "automatic text back, so the customer doesn't call the next company."),
         "audience": "Local business owners who are on a job when the phone rings",
         "cta": "Message us and we'll let you hear it answer a call",
         "link": "",
         "hashtags": "#SmallBusiness #SanAntonio #AIReceptionist #TheWatsonFactor",
         "picture_idea": ("A contractor on a ladder with his phone buzzing in his pocket, a "
                          "calm text notification glowing on screen, bright Texas afternoon, "
                          "photoreal, no text")},
        {"name": "Websites that make the phone ring",
         "offer": "A website built to turn visits into calls",
         "details": ("A tap-to-call number at the top, a 3-field quote form, fast on phones, "
                     "and your Google reviews right on the page."),
         "audience": "Local business owners whose website hasn't brought in a call in months",
         "cta": "Send us your site and we'll show you what's costing you calls",
         "link": "",
         "hashtags": "#SmallBusiness #WebDesign #SanAntonio #TheWatsonFactor",
         "picture_idea": ("A small-business owner at a shop counter smiling at a phone showing "
                          "an incoming call, warm light, photoreal, no text")},
    ],
}

# The house rules every caption is written under, per workspace. Editable in
# the app; these are the starting text.
RULES = {
    "homerepair": ("Never use the word \"free\". Never say anything is covered by insurance. "
                   "No specific repair outcomes or diagnoses - we are a maintenance concierge, "
                   "not an inspector or contractor. Never quote a repair cost. The only prices "
                   "that exist: Protect Basic $29/mo ($276/yr), Protect Essential $59/mo ($588/yr, "
                   "four seasonal visits), Protect Pro $179/mo ($1,788/yr, monthly visits); a $49 "
                   "setup fee waived on annual prepay; member labor $85, $75 or $65 an hour vs $95 "
                   "standard; repair credits of $25, $50 or $150 every 6 months on jobs of $250, $350 or "
                   "$500 and up; a $49 Home Health & Safety Audit; a $79 HomePassport report."),
    "watson": ("Never invent prices, results, statistics or client names. No promises of a "
               "number of leads or calls."),
}
BANNED = {
    "homerepair": "free, covered by insurance, insurance covers, guarantee, guaranteed",
    "watson": "guarantee, guaranteed",
}
RULES_2026_09 = {
    "homerepair": ("Never use the word \"free\". Never say anything is covered by insurance. "
                   "No specific repair outcomes or diagnoses - we are a maintenance concierge, "
                   "not an inspector or contractor. Never quote a repair cost. The only prices "
                   "that exist: plans start at $29/mo (Basic), Essential is $99/mo with up to "
                   "60 minutes, Pro is $199/mo, and overflow labor is $95/hr with approval."),
}
DEFAULT_RULES = "Never invent prices, discounts, guarantees, statistics or customer names."


def catalog_live(today: str) -> bool:
    """Is the October catalog in force on `today` (YYYY-MM-DD)?"""
    return (today or "") >= CATALOG_EFFECTIVE


def starters_for(slug: str, today: str) -> list:
    if slug in STARTERS_2026_09 and not catalog_live(today):
        return STARTERS_2026_09[slug]
    return STARTERS.get(slug, [])


def rules_for(slug: str, today: str) -> str:
    if slug in RULES_2026_09 and not catalog_live(today):
        return RULES_2026_09[slug]
    return RULES.get(slug, DEFAULT_RULES)


def clean_campaign(d: dict) -> dict:
    out = {k: re.sub(r"[ \t]+", " ", str(d.get(k) or "")).strip() for k in FIELDS}
    out["name"] = out["name"][:80]
    for k in ("offer", "audience", "cta", "hashtags"):
        out[k] = out[k][:300]
    for k in ("details", "picture_idea"):
        out[k] = out[k][:1500]
    out["link"] = out["link"][:500]
    for k in ("starts", "ends"):
        out[k] = out[k][:10] if re.match(r"^\d{4}-\d{2}-\d{2}", out[k]) else ""
    return out


def hashtags(raw: str, limit: int = MAX_TAGS) -> list[str]:
    seen, out = set(), []
    for t in re.split(r"[\s,]+", raw or ""):
        t = re.sub(r"[^\w]", "", t.lstrip("#"))
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append("#" + t)
    return out[:limit]


# ── the rules, enforced ──────────────────────────────────────────────────
PRICE_RX = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")


def _prices(text: str) -> set:
    out = set()
    for m in PRICE_RX.finditer(text or ""):
        v = m.group(1).replace(",", "")
        if v.endswith(".00"):
            v = v[:-3]
        out.add(v)
    return out


def banned_list(raw: str) -> list[str]:
    return [w.strip().lower() for w in re.split(r"[,\n]+", raw or "") if w.strip()]


def violations(text: str, banned: list[str], allowed_text: str) -> list[str]:
    """What in this caption breaks the business's rules. Empty = clean.
    `allowed_text` is everything the business itself wrote (campaign + rules):
    a dollar figure that appears there is real, any other one was invented."""
    low = (text or "").lower()
    out = []
    for w in banned:
        if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low):
            out.append('uses "%s"' % w)
    bad = sorted(_prices(text) - _prices(allowed_text))
    if bad:
        out.append("mentions a price that isn't one of yours ($%s)" % ", $".join(bad))
    return out


# ── the template writer ──────────────────────────────────────────────────
def _season(month: int) -> str:
    return ("winter" if month in (12, 1, 2) else "spring" if month in (3, 4, 5)
            else "summer" if month in (6, 7, 8) else "fall")


def _lc_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s[:2] != s[:2].upper() else s


def template(biz: dict, camp: dict | None, seed: int = 0, month: int = 9) -> str:
    """Always produces something postable, and never a fact that isn't in
    the campaign. Three shapes, rotated by `seed` (the 'write another'
    button)."""
    camp = camp or {}
    name = biz.get("name") or "us"
    city = (biz.get("city") or "").split(",")[0].strip() or "town"
    offer = (camp.get("offer") or biz.get("sells") or "").strip().rstrip(".")
    details = (camp.get("details") or "").strip()
    cta = (camp.get("cta") or "Message us or call - we'll tell you straight if it's worth doing.").strip()
    if cta and cta[-1] not in ".!?":
        cta += "."
    aud = (camp.get("audience") or biz.get("buyer") or "").strip().rstrip(".")
    tags = " ".join(hashtags(camp.get("hashtags") or ""))
    shapes = [
        ("For %s:\n\n%s.\n\n%s\n\n%s" % (_lc_first(aud), offer, details, cta)) if aud else
        ("%s.\n\n%s\n\n%s" % (offer, details, cta)),
        "Quick one for %s: %s.\n\n%s\n\n%s - %s" % (city, _lc_first(offer), details, cta, name),
        "This %s at %s: %s.\n\n%s\n\n%s" % (_season(month), name, _lc_first(offer), details, cta),
    ]
    text = shapes[seed % len(shapes)]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return (text + ("\n\n" + tags if tags else "")).strip()


# ── the AI writer ────────────────────────────────────────────────────────
ANGLES = [
    ("scene", "Open by painting a picture of a moment the reader knows - a specific, "
              "sensory scene from their week - then show how this helps."),
    ("question", "Open with a short question the reader would answer yes to."),
    ("story", "Tell it as a tiny story: the situation, what happened, how it turned out. "
              "Two or three sentences, then the offer."),
    ("tip", "Lead with one useful tip connected to the campaign, then tie it to the offer."),
    ("straight", "Say the offer plainly in the first line, then two lines on why it matters."),
]

# Text models per provider, best first. A 404 on one moves to the next, so a
# model being retired does not break the button. `caption_model` in Settings
# overrides the list.
TEXT_MODELS = {
    "gemini": ["gemini-3.8-flash", "gemini-flash-latest", "gemini-2.5-flash"],
    "meta": ["muse-spark-1.3", "muse-spark-1.2", "muse-spark-1.1"],
    "openai": ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"],
}


def can_write(provider: str) -> bool:
    return provider in TEXT_MODELS


def build_prompt(biz: dict, camp: dict | None, picture: str, rules: str,
                 angle: int, has_image: bool, problem: str = "", brief: str = "") -> str:
    camp = camp or {}
    a_key, a_how = ANGLES[angle % len(ANGLES)]
    if brief:                       # the week planner already chose the idea; the angle follows it
        a_how = "Follow THIS POST'S IDEA below - it decides what the post is about."
    lines = [
        "You write social media captions for %s, a local business in %s."
        % (biz.get("name") or "the business", biz.get("city") or "Texas"),
        "What they sell: %s." % (biz.get("sells") or "local services"),
        "",
    ]
    if camp.get("name"):
        lines += ["THE CAMPAIGN THIS POST IS FOR",
                  "Name: " + camp["name"],
                  "Offer: " + (camp.get("offer") or ""),
                  "Facts you may use (the ONLY facts): " + (camp.get("details") or "(none given)"),
                  "Who it's for: " + (camp.get("audience") or biz.get("buyer") or ""),
                  "Call to action: " + (camp.get("cta") or ""),
                  ""]
    else:
        lines += ["No specific campaign - write a general post about the business for %s."
                  % (biz.get("buyer") or "local customers"), ""]
    if brief:
        lines += ["THIS POST'S IDEA: " + brief, ""]
    if has_image:
        lines.append("The picture that goes with the post is attached. Describe what's in it "
                     "only as far as it helps the post - don't narrate the photo.")
    elif picture:
        lines.append("The picture that goes with the post: " + picture)
    lines += [
        "",
        "HOW TO WRITE IT",
        "- " + a_how,
        "- The first line is the hook and has to land within 125 characters (Instagram cuts off there).",
        "- 50 to 130 words. Warm, plain, local - like the owner talking, not an ad agency.",
        "- End with the call to action.",
        "- Don't include a link; it's added separately.",
        "- At most 2 emojis. No markdown, no quotation marks around the whole thing.",
        "- Last line: 3 to 6 hashtags%s."
        % ((", including " + " ".join(hashtags(camp.get("hashtags") or ""))) if camp.get("hashtags") else ""),
        "- Use only the prices and facts given above. Never invent a price, discount, deadline, "
        "guarantee, statistic, review or customer name.",
        "",
        "BUSINESS RULES (must follow): " + (rules or DEFAULT_RULES),
    ]
    if problem:
        lines += ["", "Your last draft broke a rule: %s. Write it again without that." % problem]
    lines += ["", "Reply with the caption text only."]
    return "\n".join(lines)


def _extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"]
    parts = []
    for item in data.get("output") or []:              # Responses API shape
        if isinstance(item, dict) and item.get("type") == "message":
            for p in item.get("content") or []:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
    if parts:
        return "\n".join(parts)
    for cand in data.get("candidates") or []:          # Gemini generateContent
        for p in ((cand or {}).get("content") or {}).get("parts") or []:
            if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought"):
                parts.append(p["text"])
    if parts:
        return "".join(parts)
    return imagegen._find_text(data)


def _call(provider: str, cfg: dict, model: str, prompt: str, image: bytes | None, http):
    """(status, body_bytes) for one attempt with one model."""
    if provider == "gemini":
        parts = []
        if image:
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(image).decode()}})
        parts.append({"text": prompt})
        return http("POST", "%s/models/%s:generateContent" % (cfg["base_url"], model),
                    {"x-goog-api-key": cfg["api_key"], "Content-Type": "application/json"},
                    json.dumps({"contents": [{"role": "user", "parts": parts}],
                                "generationConfig": {"temperature": 0.9,
                                                     "maxOutputTokens": 1024}}).encode())
    content = [{"type": "input_text", "text": prompt}]
    if image:
        content.append({"type": "input_image",
                        "image_url": "data:image/jpeg;base64," + base64.b64encode(image).decode()})
    payload = {"model": model, "input": [{"type": "message", "role": "user", "content": content}],
               "max_output_tokens": 1024}
    if provider == "meta":
        payload["stream"] = False
    return http("POST", cfg["base_url"] + "/responses",
                {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
                json.dumps(payload).encode())


def ai_draft(cfg: dict, prompt: str, image: bytes | None = None,
             model_override: str = "", http=None) -> tuple[str, str]:
    """(caption, model). Tries each text model in turn; if the provider
    refuses the picture, tries once more without it. Raises ImageGenError
    with the key redacted."""
    if http is None:
        http = imagegen.http
    provider = cfg["provider"]
    if not can_write(provider):
        raise imagegen.ImageGenError("%s makes pictures but doesn't write text."
                                     % imagegen.PROVIDERS[provider]["label"])
    models = [model_override] if model_override else TEXT_MODELS[provider]
    last = ""
    for model in models:
        for img in ([image, None] if image else [None]):
            try:
                status, body = _call(provider, cfg, model, prompt, img, http)
            except Exception as e:
                raise imagegen.ImageGenError("%s: %s" % (type(e).__name__,
                                             imagegen.redact(str(e)[:200], cfg["api_key"])))
            if status in (404,) and not model_override:
                last = "model %s isn't available" % model
                break                                   # next model
            if status == 400 and img is not None:
                last = "the picture was refused"
                continue                                # same model, no picture
            data = imagegen._json(status, body, imagegen.PROVIDERS[provider]["label"], cfg["api_key"])
            text = _extract_text(data)
            if text.strip():
                return text, model
            last = "empty answer"
    raise imagegen.ImageGenError("Couldn't get a caption from %s (%s)."
                                 % (imagegen.PROVIDERS[provider]["label"], last or "no answer"))


def tidy(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^(caption|post)\s*:\s*", "", t, flags=re.I)
    if len(t) > 2 and t[0] in "\"'“" and t[-1] in "\"'”":
        t = t[1:-1].strip()
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)             # no markdown bold on Instagram
    t = re.sub(r"(?m)^#{1,6}\s+", "", t)               # ...or headings
    t = re.sub(r"\n{3,}", "\n\n", t)
    tags = re.findall(r"(?<!\w)#\w+", t)
    if len(tags) > 30:                                 # Instagram rejects over 30
        for extra in tags[30:]:
            t = t.replace(extra, "", 1)
    return t[:MAX_CAPTION].strip()


def write(biz: dict, camp: dict | None, *, cfg: dict | None = None, image: bytes | None = None,
          picture: str = "", rules: str = "", banned: str = "", angle: int = 0,
          model_override: str = "", month: int = 9, http=None, brief: str = "") -> dict:
    """The caption for the post box. Never raises: the worst case is the
    template with a note saying why the AI wasn't used."""
    allowed = " ".join([rules or ""] + [str((camp or {}).get(k) or "") for k in FIELDS])
    ban = banned_list(banned)
    note = ""
    # The business blurb is written for us, not for customers - HomeRepair's
    # says "the free Home Health Score". Don't hand a model a banned word as
    # a fact about the business and then fail it for repeating it.
    biz = dict(biz)
    for k in ("sells", "buyer"):
        v = biz.get(k) or ""
        for w in ban:
            v = re.sub(r"(?i)(?<![\w])" + re.escape(w) + r"(?![\w])\s*", "", v)
        biz[k] = re.sub(r"\s{2,}", " ", v).strip()
    if cfg and can_write(cfg["provider"]):
        problem = ""
        for attempt in range(2):
            prompt = build_prompt(biz, camp, picture, rules, angle, bool(image), problem, brief)
            try:
                raw, model = ai_draft(cfg, prompt, image, model_override, http=http)
            except imagegen.ImageGenError as e:
                note = str(e)
                break
            text = tidy(raw)
            bad = violations(text, ban, allowed)
            if not bad:
                return {"text": text, "source": "ai", "provider": cfg["provider"],
                        "model": model, "angle": ANGLES[angle % len(ANGLES)][0],
                        "saw_picture": bool(image), "note": ""}
            problem = "; ".join(bad)
            note = "The AI's draft %s, twice - this is the template instead." % problem
    elif cfg:
        note = ("%s makes pictures but doesn't write text - this is the template. Switch "
                "to Gemini or Meta for written captions." % imagegen.PROVIDERS[cfg["provider"]]["label"])
    else:
        note = ("No AI key saved yet, so this is the template. Connect Gemini or Meta under "
                "Make a picture and the same key writes captions too.")
    text = template(biz, camp, seed=angle, month=month)
    # A template can only repeat what the business typed, but a rule is a
    # rule: if the campaign itself says "free", say so rather than post it.
    bad = violations(text, ban, allowed)
    if bad:
        note = (note + " " if note else "") + "Heads up: this campaign's own wording %s." % "; ".join(bad)
    return {"text": text, "source": "template", "provider": "", "model": "",
            "angle": ANGLES[angle % len(ANGLES)][0], "saw_picture": False, "note": note}


def slug(name: str) -> str:
    """utm_campaign value for a campaign name."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]
    return s or "campaign-" + hashlib.sha1((name or "").encode()).hexdigest()[:6]
