# -*- coding: utf-8 -*-
"""Inbound leads, sorted into four buckets, each with a reply drafted.

From the Small Business plugin's speed-to-lead skill (qualification.md and
response_patterns.md, 2026-09-15), which Daniel handed over on 2026-09-25.
The rules that survive the port:

  hot          - fits and shows urgency -> a human is told now, the reply
                 says you're calling in the next few minutes
  qualified    - fits, no time pressure -> answer what they asked, confirm
                 you can help, offer two real times
  unclear      - not enough to sort -> ONE question, the one whose answer
                 changes what happens next (not a form)
  out of scope - wrong service -> still answer, honestly, in thirty seconds

Urgency beats fit for routing. When torn between two buckets take the more
attentive one. Every reply under 100 words, no "this is an automated
response", no invented familiarity - reference only what they wrote.

The sort is keyword-based on purpose: it runs on a text reply or the
receptionist's notes with no model in the loop, so it is fast and it never
invents a detail. It only ever drafts; Daniel sends.
"""
import re
from datetime import datetime, timedelta

HOT_WORDS = [
    "urgent", "asap", "emergency", "right now", "right away", "today", "tonight", "this morning",
    "this afternoon", "same day", "same-day", "this week", "leak", "leaking", "flood", "flooding",
    "no ac", "ac is out", "ac went out", "no cooling", "not cooling", "no heat", "no hot water",
    "not working", "stopped working", "broke", "broken", "sewage", "backing up", "backed up",
    "sparking", "burning smell", "smell gas", "gas smell", "water everywhere", "won't turn on",
    "before closing", "under contract", "option period", "inspection is",
]

# What each business does not do. Anything here is out of scope however it
# is phrased; the reply says so and points on when Daniel has someone to
# point to (the `referrals` setting), and still replies when he doesn't.
OUT_OF_SCOPE = {
    "homerepair": [
        ("pool", "pool work"), ("new roof", "a roof replacement"), ("roof replacement", "a roof replacement"),
        ("re-roof", "a roof replacement"), ("foundation repair", "foundation repair"), ("foundation work", "foundation repair"),
        ("remodel", "a remodel"), ("renovation", "a renovation"), ("addition", "a room addition"),
        ("new construction", "new construction"), ("septic", "septic work"), ("tree removal", "tree work"),
        ("tree trim", "tree work"), ("pest", "pest control"), ("termite", "pest control"),
        ("mold remediation", "mold remediation"), ("solar", "solar"), ("commercial kitchen", "commercial kitchen equipment"),
    ],
    "watson": [
        ("job opening", "a job"), ("hiring", "a job"), ("resume", "a job"), ("guest post", "a guest post"),
        ("backlink", "link building"), ("link building", "link building"), ("we offer seo", "a vendor pitch"),
        ("our agency", "a vendor pitch"),
    ],
}

# Words that say what they want from us - enough to call the message
# qualified rather than unclear.
SERVICE_WORDS = {
    "homerepair": ["plan", "membership", "subscription", "visit", "maintenance", "handyman", "repair", "fix",
                   "gutter", "filter", "water heater", "ac ", "a/c", "hvac", "plumb", "faucet", "toilet",
                   "drain", "door", "fence", "paint", "caulk", "dryer vent", "smoke detector", "audit",
                   "score", "homepassport", "passport", "inspection", "rental", "property", "properties",
                   "hoa", "board", "building", "tenant", "quote", "estimate", "price", "cost", "how much",
                   "call me", "give me a call", "schedule", "book"],
    "watson": ["website", "site", "reviews", "google", "calls", "phone", "receptionist", "ordering",
               "app", "seo", "ads", "marketing", "leads", "customers", "social", "facebook", "instagram",
               "price", "cost", "how much", "quote", "call me", "give me a call", "schedule", "book"],
}

ONE_QUESTION = {
    "homerepair": "Is this for a house you live in, a rental you own, or a building you manage? That changes who I send and what it runs.",
    "watson": "What kind of business is it, and what's the one thing you want more of - calls, reviews, or orders?",
}

BUCKET_LABEL = {"hot": "Call now", "qualified": "Reply and book", "unclear": "One question",
                "out_of_scope": "Not a fit - answer anyway"}


def _slug(workspace: str) -> str:
    return "homerepair" if (workspace or "").startswith("homerepair") else "watson"


def sort(text: str, *, urgency: str = "", requested_time: str = "", repeat: bool = False,
         workspace: str = "") -> dict:
    """{"bucket", "why", "topic", "question"} for one inbound message."""
    slug = _slug(workspace)
    raw = (text or "").strip()
    low = " " + re.sub(r"\s+", " ", raw.lower()) + " "
    topic = ""
    for needle, name in OUT_OF_SCOPE.get(slug, []):
        if needle in low:
            topic = name
            break
    hot_hit = next((w for w in HOT_WORDS if w in low), "")
    if (urgency or "").lower() == "hot" or hot_hit or requested_time.strip() or repeat:
        # urgency beats fit for routing - even an out-of-scope emergency gets
        # a human, who can point them somewhere fast
        why = ("the receptionist marked it hot" if (urgency or "").lower() == "hot" else
               'they said "%s"' % hot_hit if hot_hit else
               "they asked for a time (%s)" % requested_time.strip() if requested_time.strip() else
               "a repeat customer")
        return {"bucket": "hot", "why": why, "topic": topic, "question": ""}
    if topic:
        return {"bucket": "out_of_scope", "why": "they asked about %s" % topic, "topic": topic, "question": ""}
    has_service = any(w in low for w in SERVICE_WORDS.get(slug, []))
    if not has_service:
        return {"bucket": "unclear", "why": "not enough to tell what they need", "topic": "", "question": ONE_QUESTION[slug]}
    return {"bucket": "qualified", "why": "fits, no time pressure", "topic": "", "question": ""}


def snippet(text: str, n: int = 9) -> str:
    """Their own words, the first few, so the reply proves someone read it."""
    words = re.sub(r"\s+", " ", (text or "").strip()).split(" ")
    s = " ".join(words[:n]).rstrip(".,;:!?")
    return s + ("..." if len(words) > n else "")


def next_slots(local_now: datetime, times=("10:00", "15:00"), count: int = 2) -> list:
    """The next `count` call-back slots from Daniel's own times, weekdays,
    at least an hour out. 'Tue 10:00 AM' / 'Tue 3:00 PM'."""
    out = []
    parsed = []
    for t in times:
        m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", str(t).strip(), re.I)
        if not m:
            continue
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        ap = (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        parsed.append((h, mi))
    if not parsed:
        parsed = [(10, 0), (15, 0)]
    d = local_now.replace(second=0, microsecond=0)
    for day in range(0, 10):
        base = (d + timedelta(days=day))
        if base.weekday() >= 5:
            continue
        for h, mi in sorted(parsed):
            cand = base.replace(hour=h, minute=mi)
            if cand < local_now + timedelta(hours=1):
                continue
            label = ("today " if cand.date() == local_now.date() else "tomorrow " if cand.date() == (local_now + timedelta(days=1)).date()
                     else cand.strftime("%a ")) + cand.strftime("%I:%M %p").lstrip("0")
            out.append(label)
            if len(out) >= count:
                return out
    return out


def draft(sorted_: dict, *, text: str, name: str, business: str, sender: str,
          slots: list, workspace: str = "", referral: str = "", channel: str = "sms") -> str:
    """The reply, under 100 words, in the four shapes. Plain text, one
    paragraph for a text message, short paragraphs for email."""
    slug = _slug(workspace)
    first = (name or "").strip().split(" ")[0]
    hi = "Hi %s, " % first if first and first.lower() not in ("inbound", "caller") else "Hi, "
    who = "%s with %s. " % (sender, business) if sender else "%s here. " % business
    said = snippet(text)
    b = sorted_["bucket"]
    if b == "hot":
        body = ("Got your message%s - that sounds like a same-day thing. I'm calling you in the next few minutes to get the address and what's going on."
                % (' about "%s"' % said if said else ""))
    elif b == "qualified":
        when = (" I can call you %s or %s - which works?" % (slots[0], slots[1]) if len(slots) >= 2
                else " I can call you %s - does that work?" % slots[0] if slots else " When's a good time for a quick call?")
        short = len(re.findall(r"[a-z0-9']+", (text or "").lower())) < 5
        body = ("Got your message - happy to help.%s" % when if short else
                "Got your message%s - yes, that's what we do.%s" % (' about "%s"' % said if said else "", when))
    elif b == "unclear":
        body = "Got your message - happy to help. One thing first: %s" % sorted_["question"]
    else:
        topic = sorted_.get("topic") or "that"
        body = ("Straight answer: %s isn't something we take on, so I'd be doing you a disservice taking it. %s"
                % (topic, referral.strip() if referral.strip() else "Didn't want to leave you hanging, though - good luck with it."))
    sign = "" if channel == "sms" else "\n\n%s" % (sender or business)
    out = hi + who + body + sign
    return out.strip()


def word_count(s: str) -> int:
    return len(re.findall(r"[A-Za-z0-9'$-]+", s or ""))
