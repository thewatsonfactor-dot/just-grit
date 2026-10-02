# -*- coding: utf-8 -*-
"""The slop test - does this email read like a bot wrote it?

Lifted from Anthropic's Small Business plugin (outreach-composer's
"slop test", 2026-09-15), which Daniel handed over on 2026-09-25 with
"use what would be helpful for Just Grit". The checks owners recognise
instantly because their own inboxes are full of them: the compliment
opener, "I hope this finds you well", "reach out", "just following up",
em dashes everywhere, three neat bullets, a 120-word cold email that
circles the ask. Plus the owner's own never-say list from Setup.

Runs on every draft in the Outbox (live, as he edits), on the AI caption
writer's output, and in the tests over every template we ship. It warns;
it never rewrites and never blocks a send - the words are his.
"""
import re

# Openers that read as generated regardless of what follows.
BANNED_OPENERS = [
    "i hope this email finds you well", "i hope this finds you well", "hope this finds you well",
    "i hope you're doing well", "i hope you are doing well", "hope you're doing well",
    "i wanted to reach out", "i'm reaching out", "i am reaching out",
    "i came across your", "i came across you", "i've been following your", "i have been following your",
    "quick question for you", "i'll keep this brief", "i will keep this brief",
    "i know you're busy", "i know you are busy",
]

# Filler that occupies space without carrying meaning.
BANNED_FILLER = [
    "reach out", "reaching out", "circle back", "touch base", "leverage", "utilize", "utilise",
    "at your earliest convenience", "i'd love to", "i would love to", "excited to",
    "synergy", "synergies", "unlock", "just following up", "just bumping", "bumping this",
    "let me know if you have any questions", "don't hesitate to", "do not hesitate to",
    "i'd be happy to discuss", "add value", "value-add", "game-changer", "game changer",
    "cutting-edge", "best-in-class", "world-class", "seamless", "robust", "streamline",
    "take your .* to the next level", "in today's fast-paced",
]

# The compliment opener - a fact beats a compliment.
COMPLIMENT_RX = re.compile(r"\b(impressed (by|with)|love what you('re| are) (doing|building)|great work you('re| are) doing|"
                           r"admire (what|the work))", re.I)

# Word ceilings by touch. Ours carry a little more than the playbook's
# 120/60/80/40 because the follow-ups explain the service and the day-11
# touch carries a checklist; the ceilings still catch a draft that has
# started to ramble.
CEILING = {1: 130, 2: 90, 3: 115, 4: 65}
DEFAULT_CEILING = 90


def _strip(body: str) -> str:
    """Words that count: the message, not the signature or footer."""
    b = body or ""
    # cut at the signature block: a line that is just a name, "Thanks," etc. is
    # hard to spot generically, so cut at the first "--"/"—" divider or the
    # footer's "You're receiving this" line, whichever comes first
    for marker in ("\n—\n", "\n--\n", "\nYou're receiving this", "\nYou are receiving this", "\nReply \"stop\""):
        i = b.find(marker)
        if i >= 0:
            b = b[:i]
    return b


def word_count(body: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$'’-]+", re.sub(r"\{\{.*?\}\}", "", _strip(body))))


def check(subject: str, body: str, step: int = 1, never=None,
          signature: str = "", ceiling: int = 0) -> list:
    """What in this draft reads like a bot. Empty = clean. Each item is a
    short line for the Outbox card; the first word says what kind."""
    out = []
    text = _strip(body or "")
    if signature:
        # the sign-off block (name, business, phone) is not the message
        i = text.find("\n\n%s\n" % signature.strip())
        if i > 0:
            text = text[:i]
    paras = [p.strip() for p in text.lower().split("\n\n") if p.strip()]
    # the greeting line is not the message - and company names live there
    # ("Hi Team Housing Solutions team,") which the filler list must not read
    if paras and re.match(r"^(hi|hello|hey|dear|good (morning|afternoon|evening))\b", paras[0]) and len(paras[0]) < 60:
        paras = paras[1:]
    low = "\n\n".join(paras)
    first_para = paras[0] if paras else ""
    for w in BANNED_OPENERS:
        if w in first_para:
            out.append("opens with \"%s\" - the most recognisable line in cold email. Open with the reason you're writing." % w)
            break
    if COMPLIMENT_RX.search(first_para):
        out.append("opens with a compliment about their business - a fact beats a compliment, and proves someone looked.")
    hits = []
    for w in BANNED_FILLER:
        if re.search(r"(?<![\w])" + w + r"(?![\w])", low):
            hits.append(w.replace(".*", "..."))
    for w in (never or []):
        w = (w or "").strip().lower()
        if w and re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low) and w not in hits:
            hits.append(w)
    if hits:
        out.append("uses %s - words you don't say out loud." % ", ".join('"%s"' % h for h in hits[:4]))
    n = len(re.findall(r"[A-Za-z0-9$'’-]+", re.sub(r"\{\{.*?\}\}", "", text)))
    custom = bool(ceiling)
    ceiling = ceiling or CEILING.get(int(step or 1), DEFAULT_CEILING)
    if n > ceiling:
        out.append("%d words - %s reads best under %d. Cut the opener or the credentials first."
                   % (n, "this one" if custom else {1: "a first email", 3: "the no-ask touch", 4: "a close-out"}.get(int(step or 1), "a follow-up"), ceiling))
    em = text.count("—")
    if em >= 3:
        out.append("%d em dashes - people write with commas and periods." % em)
    bullets = re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S", text)
    if len(bullets) == 3:
        out.append("three neat bullets - a generation tell. One thing, or a messy list.")
    # The tells the first pass missed (Daniel, 2026-09-28, on the HOA emails:
    # "these are not following the no chat bot sounding rule"). None is a
    # banned word - they are shapes a model reaches for.
    if re.search(r"(?i)\b(isn't|is not|aren't|are not|wasn't)\b[^.!?\n]{0,90}[.!?]\s+(it's|it is|that's|they're)\b", low) \
            or re.search(r"(?i)\bthe (hard|real|tricky|important) part\b[^.!?\n]{0,60}\b(isn't|is not)\b", low):
        out.append("\"it isn't X. It's Y\" - the flip-the-script line is a bot tell. Just say the Y.")
    if re.search(r"(?i)\b(most|every|a lot of|many)\s+\w+(\s+\w+)?\s+i\s+(talk|speak|work|meet)\s+(to|with)\b", low):
        out.append("\"most ___ I talk to\" - an invented generalization. Say why you're writing to them.")
    if re.search(r"(—|–| - )[^.!?\n]*,[^.!?\n]*,[^.!?\n]*\band\b", text):
        out.append("a list hung off a dash - read it out loud; nobody talks in inventories.")
    if re.search(r"(?i)\b(the only (window|time|way) (where|when)|it turns [^.!?]{3,60} into|here's the thing|let's be honest|"
                 r"at the end of the day|the bottom line is|we do the \w+ing\.)", low):
        out.append("a slogan line - it sounds written, not said. Say the plain version.")
    if re.search(r"(?i)\b(this is an automated|automated (message|response|reply))", low):
        out.append("says it's automated - that undoes the whole point of writing as you.")
    s = (subject or "").lower()
    if s.startswith("re:") and "following up" in s:
        out.append("subject says \"following up\" - name the angle of this message instead.")
    return out


def score(subject: str, body: str, step: int = 1, never=None, signature: str = "", ceiling: int = 0) -> dict:
    w = check(subject, body, step, never, signature, ceiling)
    return {"warnings": w, "words": word_count(body), "clean": not w}


def never_list(raw: str) -> list:
    """The owner's never-say box: commas or newlines, blanks dropped."""
    return [x.strip() for x in re.split(r"[,\n]", raw or "") if x.strip()]
