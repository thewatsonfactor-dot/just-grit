# -*- coding: utf-8 -*-
"""What the social tools charge for, minus the subscription.

Three things every scheduler sells to a local business, built here from the
data the workspace already has and nothing else:

  plan_week()    five post ideas for the week - one per pillar (proof, offer,
                 tip, behind the scenes, local) - each with a caption to edit,
                 a matching image prompt for the picture box, and a slot.
                 Deterministic: seeded by the ISO week, so the plan is stable
                 all week and different next week. No model, no key, no bill.
  review_card()  a 1080x1080 branded quote card from a customer review - the
                 "share this review" feature. Drawn with Pillow and bundled
                 fonts so it renders identically on every machine, and never
                 with an image model, because models misspell the customer's
                 words and a review card with a typo in the quote is worse
                 than no post.
  utm()          a tracked link, so a lead that came from a post is a lead
                 that came from a post in Analytics and not "direct".

Pure. No network, no database - webapp stores and logs.
"""
from __future__ import annotations
import datetime as _dt
import hashlib
import io
import re
import textwrap
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

FONTS = Path(__file__).parent / "static" / "fonts"

# ── the week plan ────────────────────────────────────────────────────────
# Local-business posting slots. Not "best time for your audience" - that
# needs the network's own data (Metricool has it, when connected). These are
# the hours a local feed is read: lunch and the after-dinner scroll, with the
# weekend morning for the household decisions.
SLOTS = {
    "proof":   ("Tue", "18:30"),
    "offer":   ("Thu", "11:30"),
    "tip":     ("Wed", "12:00"),
    "behind":  ("Fri", "17:30"),
    "local":   ("Sat", "09:30"),
}

PILLARS = {
    "proof": {
        "label": "Proof - a customer's words",
        "why": ("A stranger trusts another customer before they trust you. One "
                "real review a week outperforms any amount of talking about yourself."),
        "captions": [
            "\"{quote}\" - {reviewer}\n\nThat's the whole job, really. Thank you for "
            "trusting {name}.\n\n{city} - if you need us, the number's in the bio.",
            "This one made our week.\n\n\"{quote}\"\n\nThank you, {reviewer}. {name} is "
            "here whenever you need us, {city}.",
        ],
        "prompts": [
            "A clean, warm photo of a satisfied customer's completed job, no people's "
            "faces, natural light, {city} setting, phone-camera realism, no text",
            "Close-up of hands shaking in front of a finished job, warm afternoon light, "
            "{city}, shallow depth of field, no text in the image",
        ],
        "needs": "a review - paste one into the review card box and this fills itself",
    },
    "offer": {
        "label": "Offer - one thing you sell, plainly",
        "why": ("Most followers have no idea what you actually do. Say one thing, say "
                "what it costs or what it saves, and say how to get it."),
        "captions": [
            "Here's what {name} does for {buyer} in {city}: {sells}.\n\nIf that's been "
            "on your list, this is the sign. Message us or call - we'll tell you "
            "straight if it's worth doing.",
            "Quick one for {city}: {name} handles {sells}.\n\nNo pressure, no upsell. "
            "Tell us what you've got and we'll tell you what it takes.",
        ],
        "prompts": [
            "A tidy, well-lit hero shot of the work {name} does: {sells}. Real-looking, "
            "not stock, {city}, bright morning light, room for a headline, no text",
            "Flat-lay of the tools and materials for {sells}, on a clean workbench, "
            "overhead shot, warm light, no text",
        ],
        "needs": "",
    },
    "tip": {
        "label": "Tip - something useful for free",
        "why": ("Useful beats clever. A tip your buyer can act on this weekend gets saved "
                "and shared, and it puts your name next to being helpful."),
        "captions": [
            "One thing {buyer} in {city} ask us all the time:\n\n[the question]\n\n"
            "Short answer: [your answer in two sentences].\n\nWant the long answer? "
            "Ask below - {name} will tell you what we'd do at our own place.",
            "Free advice from {name}, no strings:\n\n[a tip your buyer can do this "
            "weekend]\n\nIf it turns out to be bigger than that, you know where we are, {city}.",
        ],
        "prompts": [
            "An over-the-shoulder shot of someone doing a simple DIY check related to "
            "{sells}, home setting in {city}, natural light, helpful and calm, no text",
            "A single clear object related to {sells} on a plain background, soft "
            "studio light, educational feel, no text",
        ],
        "needs": "",
    },
    "behind": {
        "label": "Behind the scenes - the people doing the work",
        "why": ("People buy from people. A face, a truck, a Friday afternoon - it is the "
                "post that makes a stranger feel they already know you."),
        "captions": [
            "Friday at {name}.\n\n[who's in the photo and what they were doing]\n\n"
            "This is the crew {city} gets when they call us. Have a good weekend.",
            "Not a polished one today - just us, mid-job.\n\n[one sentence about the job]"
            "\n\n{name}, {city}. Real people, real work.",
        ],
        "prompts": [
            "Candid photo of a small crew in work clothes laughing next to their truck at "
            "the end of a job, golden hour, {city} street, phone-camera look, no text",
            "A workshop or van interior with the day's tools packed up, warm evening light, "
            "lived-in and honest, no text",
        ],
        "needs": "",
    },
    "local": {
        "label": "Local - something about {city}, not about you",
        "why": ("A post about the place you serve gets shared by people who have never "
                "needed you yet. It is how the next customer finds out you exist."),
        "captions": [
            "{city} this weekend: [an event, a spot, a season thing].\n\nWe'll be around "
            "if anything at the house decides to break while you're out. - {name}",
            "Shout-out to another {city} business we love: [name them and why].\n\n"
            "Small businesses keep this town running. Tag one you'd send a friend to.",
        ],
        "prompts": [
            "A recognisable, sunny street scene in {city}, Texas, locals out walking, "
            "flags and storefronts, warm and welcoming, editorial photo, no text",
            "The {city} skyline or riverwalk at golden hour, wide shot, inviting, no text",
        ],
        "needs": "",
    },
}
ORDER = ["proof", "offer", "tip", "behind", "local"]


def _pick(seq: list, seed: int) -> str:
    return seq[seed % len(seq)] if seq else ""


def _fill(t: str, biz: dict) -> str:
    out = t
    for k in ("name", "city", "sells", "buyer", "quote", "reviewer"):
        out = out.replace("{%s}" % k, biz.get(k) or "[%s]" % k)
    return out


def plan_week(biz: dict, week: str | None = None) -> list[dict]:
    """Five posts for the week. `biz` = {name, city, sells, buyer} and,
    when there is one, {quote, reviewer} for the proof post. `week` is an
    ISO 'YYYY-Www' - defaults to this week - and is the only randomness."""
    week = week or _dt.date.today().strftime("%G-W%V")
    seed = int(hashlib.sha1(week.encode()).hexdigest()[:8], 16)
    b = {k: (biz.get(k) or "").strip() for k in
         ("name", "city", "sells", "buyer", "quote", "reviewer")}
    b["city"] = b["city"].split(",")[0].strip() or b["city"]      # "San Antonio, TX" -> "San Antonio"
    out = []
    for i, key in enumerate(ORDER):
        p = PILLARS[key]
        day, at = SLOTS[key]
        s = seed + i
        needs = p["needs"]
        if key == "proof" and b["quote"]:
            needs = ""
        out.append({
            "kind": key,
            "label": _fill(p["label"], b),
            "why": p["why"],
            "day": day, "time": at,
            "caption": _fill(_pick(p["captions"], s), b),
            "image_prompt": _fill(_pick(p["prompts"], s), b),
            "needs": needs,
            "fill_in": bool(re.search(r"\[[^\]]+\]", _fill(_pick(p["captions"], s), b))),
        })
    return out


# ── tracked links ────────────────────────────────────────────────────────
def utm(link: str, network: str, campaign: str = "") -> str:
    """Add utm_source/medium/campaign without clobbering ones already there.
    Empty link -> empty string; a link with no scheme gets https."""
    link = (link or "").strip()
    if not link:
        return ""
    if not re.match(r"^https?://", link, re.I):
        link = "https://" + link
    u = urlparse(link)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.setdefault("utm_source", (network or "social").lower())
    q.setdefault("utm_medium", "social")
    if campaign:
        q.setdefault("utm_campaign", re.sub(r"[^a-z0-9_-]+", "-", campaign.lower()).strip("-")[:60])
    return urlunparse(u._replace(query=urlencode(q)))


# ── the review card ──────────────────────────────────────────────────────
def _hex(c: str, fallback=(232, 163, 61)) -> tuple:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (c or "").strip())
    if not m:
        return fallback
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def clean_quote(text: str, max_chars: int = 320) -> str:
    """The review, tidied for a card: whitespace collapsed, wrapped in real
    quotes, cut at a sentence end if it is too long. Never reworded."""
    t = re.sub(r"\s+", " ", (text or "")).strip().strip("\"'“”")
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[:end + 1] if end > max_chars * 0.5 else cut.rstrip() + "…")
    return t


def review_caption(quote: str, reviewer: str, stars: int, name: str, city: str = "") -> str:
    r = (reviewer or "").strip() or "a customer"
    s = "★" * max(1, min(5, int(stars or 5)))
    lines = ["%s from %s" % (s, r), "", "\"%s\"" % clean_quote(quote), "",
             "Thank you for trusting %s." % (name or "us")]
    if city:
        lines.append("%s - the number's in the bio if you need us." % city.split(",")[0].strip())
    return "\n".join(lines)


def review_card(quote: str, reviewer: str, stars: int, name: str,
                accent: str = "#e8a33d", size: int = 1080, fonts_dir: Path = FONTS) -> bytes:
    """1080x1080 JPEG. Dark ground, accent rule and stars, the quote in a
    readable serif-less face, the reviewer and the business at the foot.
    Raises RuntimeError with a plain sentence if Pillow is missing."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow is not installed, so the review card cannot be drawn "
                           "(pip install pillow).")
    W = H = size
    ground = (12, 12, 11)
    ink = (243, 239, 232)
    ink2 = (200, 194, 184)
    acc = _hex(accent)
    im = Image.new("RGB", (W, H), ground)
    d = ImageDraw.Draw(im)

    def font(file, px):
        p = Path(fonts_dir) / file
        try:
            return ImageFont.truetype(str(p), px)
        except Exception:
            return ImageFont.load_default()

    q = clean_quote(quote)
    n = max(1, min(5, int(stars or 5)))
    pad = int(W * 0.09)

    # accent bar top-left, the brand's stripe motif
    d.rectangle([pad, pad, pad + int(W * 0.18), pad + 10], fill=acc)

    # stars - drawn, not typed. The bundled faces have no star glyph and a
    # fallback font's box character on a review card is not an option.
    import math
    r_out = int(W * 0.030)
    r_in = int(r_out * 0.45)
    cy = pad + 34 + r_out
    for i in range(5):
        cx = pad + r_out + i * int(r_out * 2.35)
        pts = []
        for k in range(10):
            r = r_out if k % 2 == 0 else r_in
            ang = -math.pi / 2 + k * math.pi / 5
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        if i < n:
            d.polygon(pts, fill=acc)
        else:
            d.polygon(pts, outline=acc, width=3)

    # the quote - pick the biggest size that fits the box
    box_top = pad + 34 + int(W * 0.12)
    box_bot = H - pad - int(W * 0.20)
    maxw = W - 2 * pad
    body = None
    for px in (int(W * 0.062), int(W * 0.055), int(W * 0.048), int(W * 0.042), int(W * 0.036)):
        f = font("IBMPlexSans-SemiBold.ttf", px)
        chars = max(18, int(maxw / (px * 0.52)))
        lines = []
        for para in ("“" + q + "”").split("\n"):
            lines += textwrap.wrap(para, width=chars) or [""]
        lh = int(px * 1.28)
        if len(lines) * lh <= box_bot - box_top:
            body = (f, lines, lh)
            break
    if body is None:
        f = font("IBMPlexSans-SemiBold.ttf", int(W * 0.036))
        lines = textwrap.wrap("“" + q + "”", width=44)[:12]
        body = (f, lines, int(W * 0.036 * 1.28))
    f, lines, lh = body
    y = box_top
    for ln in lines:
        d.text((pad, y), ln, font=f, fill=ink)
        y += lh

    # foot: reviewer, then the business name in the display face
    foot_y = H - pad - int(W * 0.15)
    d.rectangle([pad, foot_y, pad + 60, foot_y + 6], fill=acc)
    rf = font("IBMPlexSans-Regular.ttf", int(W * 0.034))
    d.text((pad, foot_y + 22), "- " + ((reviewer or "").strip() or "a customer"), font=rf, fill=ink2)
    nf = font("Anton-Regular.ttf", int(W * 0.052))
    nm = (name or "").strip().upper()
    if nm:
        tw = d.textlength(nm, font=nf)
        d.text((W - pad - tw, H - pad - int(W * 0.06)), nm, font=nf, fill=ink)

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


# ── preview helpers ──────────────────────────────────────────────────────
IG_FOLD = 125          # Instagram shows about this many characters before "... more"


def fold(text: str, at: int = IG_FOLD) -> tuple[str, str]:
    """(shown, hidden) the way Instagram cuts a caption in the feed."""
    t = (text or "").strip()
    if len(t) <= at:
        return t, ""
    cut = t[:at]
    sp = cut.rfind(" ")
    if sp > at * 0.6:
        cut = cut[:sp]
    return cut, t[len(cut):]
