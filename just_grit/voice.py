# -*- coding: utf-8 -*-
"""The owner's voice, learned from their own emails and applied to every
draft. From the Small Business plugin's shared voice profile (26 Sep
2026): "friendly and professional describes every business email ever
written" - so this captures the imitable specifics instead: greeting
form, sign-off, sentence length, contractions, exclamation habit, the
phrases they really use, and the words absent from every sample.

Pure code. Learns from 3+ pasted emails or the mailbox's Sent folder;
never invents a trait it didn't see. Applying it changes only the
greeting line and the sign-off line of a draft - the message stays the
composer's, the voice on either end becomes the owner's - and adds one
check to the slop test: a sentence far longer than the owner ever writes.
"""
import json, re
from collections import Counter

GREET_RX = re.compile(r"^\s*(hi|hello|hey|dear|good (?:morning|afternoon|evening)|howdy)\b[\s,]*([A-Z][\w'.-]*)?\s*([,—–-]?)\s*$", re.I)
NAME_DASH_RX = re.compile(r"^\s*([A-Z][\w'.-]*)\s*[—–-]\s*$")
SIGNOFF_WORDS = ["thanks", "thank you", "many thanks", "best", "all the best", "cheers", "regards", "kind regards",
                 "best regards", "talk soon", "appreciate it", "take care", "sincerely", "much appreciated"]
SIGNOFF_RX = re.compile(r"^\s*(%s)\s*[,!.]?\s*$" % "|".join(re.escape(w) for w in SIGNOFF_WORDS), re.I)
CONTRACTION_RX = re.compile(r"\b\w+(?:'ll|'ve|n't|'re|'m|'d|'s)\b", re.I)
STOP = set("the a an and or of to in on for with at by from is are was were be been it this that these those i you we they he she "
           "my your our their his her me us them as if so do does did not no yes will would can could should just there here about".split())
PARTICLES = {"by", "up", "in", "on", "out", "back", "over", "off", "through", "along"}
NEVER_CANDIDATES = ["reach out", "reaching out", "circle back", "touch base", "hope this finds you well", "i wanted to", "excited to",
                    "leverage", "solutions", "synergy", "just following up", "at your earliest convenience", "please don't hesitate",
                    "i'd love to", "quick question", "value", "streamline", "utilize"]


def _strip_quoted(text: str) -> str:
    """Drop quoted replies and forwarded blocks so we learn only their words."""
    out = []
    for line in (text or "").replace("\r", "").split("\n"):
        if re.match(r"^\s*(>|On .+ wrote:|-{2,}\s*Original Message|From: .+|Sent from my )", line):
            break
        out.append(line)
    return "\n".join(out).strip()


def _parts(sample: str):
    """(greeting_line, body_lines, signoff_line, name_line) from one email."""
    lines = [l.rstrip() for l in _strip_quoted(sample).split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "", [], "", ""
    greet = ""
    if GREET_RX.match(lines[0]) or NAME_DASH_RX.match(lines[0]):
        greet = lines[0].strip(); lines = lines[1:]
    # a signature block: name line (1-3 words, capitalised) possibly followed by phone/site
    name = ""
    tail = [l for l in lines[-4:] if l.strip()]
    for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1):
        l = lines[i].strip()
        if l and len(l.split()) <= 3 and l[0].isupper() and not l.endswith((".", "?", "!")) and not SIGNOFF_RX.match(l):
            name = l; lines = lines[:i]; break
    signoff = ""
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        if SIGNOFF_RX.match(lines[i]):
            signoff = lines[i].strip(); lines = lines[:i]
        break
    body = [l for l in lines if l.strip()]
    return greet, body, signoff, name


def _greet_form(greet: str) -> str:
    """'Hi Dana,' -> 'Hi {first},'  ·  'Dana —' -> '{first} —'  ·  'Hey there' -> 'Hey {first}'"""
    m = NAME_DASH_RX.match(greet)
    if m:
        return "{first} —"
    m = GREET_RX.match(greet)
    if not m:
        return ""
    word = m.group(1)
    word = word[0].upper() + word[1:].lower()
    punct = m.group(3) or ""
    return ("%s {first}%s" % (word, punct)) if m.group(2) else ("%s%s" % (word, punct))


def learn(samples: list, owner_first: str = "") -> dict:
    """Build the profile. Needs 3 samples; fewer returns {'error'}."""
    samples = [s for s in (samples or []) if _strip_quoted(s).strip()]
    if len(samples) < 3:
        return {"error": "Paste at least three emails you were happy with - a profile from fewer than that is a guess."}
    greets, signoffs, names = Counter(), Counter(), Counter()
    sentences, contractions, words_total, excl, asks = [], 0, 0, 0, []
    grams = Counter(); per_sample_grams = []
    texts = []
    for s in samples:
        g, body, so, name = _parts(s)
        if g:
            greets[_greet_form(g)] += 1
        if so:
            signoffs[so.rstrip(",!.") + ("," if so.endswith(",") else "")] += 1
        if name:
            names[name] += 1
        text = " ".join(body)
        texts.append(text.lower())
        sents = [x for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
        sentences += [len(x.split()) for x in sents]
        words_total += len(text.split())
        contractions += len(CONTRACTION_RX.findall(text))
        excl += text.count("!")
        for i, line in enumerate(body):
            if "?" in line:
                asks.append(i + 1); break
        toks = [t for t in re.findall(r"[a-z']+", text.lower())]
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(toks) - n + 1):
                g_ = tuple(toks[i:i + n])
                if all(t in STOP for t in g_):
                    continue
                seen.add(" ".join(g_))
        per_sample_grams.append(seen)
    for s in per_sample_grams:
        grams.update(s)
    n = len(samples)
    cands = [p for p, c in grams.most_common(200) if c >= 2 and len(p) > 6
             and p.split()[0] not in STOP and (p.split()[-1] not in STOP or p.split()[-1] in PARTICLES)]
    cands.sort(key=lambda p: (-grams[p], -len(p)))
    phrases = []
    for p in cands:
        if not any(p in k or k in p for k in phrases):
            phrases.append(p)
        if len(phrases) >= 8:
            break
    avg = round(sum(sentences) / len(sentences), 1) if sentences else 0
    longest = max(sentences) if sentences else 0
    all_text = " ".join(texts)
    never = [w for w in NEVER_CANDIDATES if w not in all_text]
    ex_rate = excl / max(1, n)
    prof = {
        "built": "", "sources": "%d emails" % n,
        "greeting": greets.most_common(1)[0][0] if greets else "",
        "signoff": signoffs.most_common(1)[0][0] if signoffs else "",
        "name_line": names.most_common(1)[0][0] if names else (owner_first or ""),
        "avg_sentence": avg, "longest_sentence": longest,
        "length": "short" if avg and avg <= 13 else "medium" if avg and avg <= 20 else "long" if avg else "",
        "contractions": (contractions / max(1, words_total)) > 0.008,
        "exclamations": "never" if excl == 0 else "rare" if ex_rate < 0.5 else "frequent",
        "ask_line": round(sum(asks) / len(asks)) if asks else None,
        "phrases": phrases, "never": never[:10],
        "sample": min(samples, key=lambda s: len(_strip_quoted(s))).strip()[:600],
    }
    return prof


def summary(p: dict) -> str:
    """The profile in the playbook's card form, for the Setup page."""
    if not p or p.get("error"):
        return ""
    lines = [
        "Greeting:      %s" % (p.get("greeting") or "(none seen)"),
        "Sign-off:      %s %s" % (p.get("signoff") or "(none)", p.get("name_line") or ""),
        "Sentences:     %s, average %s words" % (p.get("length") or "?", p.get("avg_sentence")),
        "Contractions:  %s" % ("yes" if p.get("contractions") else "no"),
        "Exclamations:  %s" % p.get("exclamations"),
        "Ask timing:    line %s" % (p.get("ask_line") or "?"),
    ]
    if p.get("phrases"):
        lines.append("Uses:          " + " · ".join('"%s"' % x for x in p["phrases"]))
    if p.get("never"):
        lines.append("Never uses:    " + " · ".join(p["never"]))
    return "\n".join(lines)


def apply(body: str, p: dict, first: str = "", sender_name: str = "") -> str:
    """Put the owner's greeting form and sign-off on a draft. Only the
    greeting line and the line before the signature name change."""
    if not p or p.get("error") or not body:
        return body
    lines = body.replace("\r", "").split("\n")
    # greeting
    form = p.get("greeting") or ""
    if form and lines:
        i = next((k for k, l in enumerate(lines) if l.strip()), None)
        if i is not None:
            m = GREET_RX.match(lines[i])
            if m:
                cur_name = m.group(2) or ""
                # keep "Hi Blanco Cafe team," / "Hi Misty Oaks board," as they are - there is no first name to put in the form
                rest = lines[i].strip()
                if cur_name and re.fullmatch(r"(hi|hello|hey|dear|good \w+|howdy)\s+%s\s*[,—–-]?" % re.escape(cur_name), rest, re.I) \
                        and cur_name.lower() not in ("there", "team", "all", "folks"):
                    lines[i] = form.replace("{first}", cur_name)
                elif not cur_name and "{first}" not in form:
                    lines[i] = form
    # sign-off: insert or replace the closing word right before the signature name line
    so = p.get("signoff") or ""
    if so and sender_name:
        name_idx = next((k for k, l in enumerate(lines) if l.strip() == sender_name.strip()), None)
        if name_idx is not None:
            j = name_idx - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0 and SIGNOFF_RX.match(lines[j]):
                lines[j] = so
            elif j >= 0:
                # body ends here; put the sign-off on its own line before the name
                lines.insert(name_idx, so)
                if name_idx > 0 and lines[name_idx - 1].strip():
                    lines.insert(name_idx, "")
    return "\n".join(lines)


def long_sentence_warning(body: str, p: dict):
    """One slop warning: a sentence far longer than the owner writes."""
    if not p or p.get("error") or not p.get("avg_sentence"):
        return None
    cap = max(22, int(p["avg_sentence"] * 2), int(p.get("longest_sentence") or 0) + 4)
    _, lines, _, _ = _parts(re.sub(r"\{\{.*?\}\}", "", body or ""))
    text = " ".join(lines)
    for s in re.split(r"(?<=[.!?])\s+", text):
        n = len(s.split())
        if n > cap:
            return "a %d-word sentence - you write in %s ones (average %s). Break it in two." % (n, p.get("length") or "shorter", p["avg_sentence"])
    return None


# ── learning from edits ──────────────────────────────────────────────────
def cut_phrases(original: str, edited: str) -> list:
    """Word runs (2-4 words) present in the original and gone from the
    edit - what the owner struck out."""
    def words(t):
        _, body, _, _ = _parts(t)
        return re.findall(r"[a-z']+", " ".join(body).lower())
    o = words(original)
    e = " " + " ".join(words(edited)) + " "
    out, seen = [], set()
    for n in (2, 3):
        for i in range(len(o) - n + 1):
            g = o[i:i + n]
            if g[0] in STOP or (g[-1] in STOP and g[-1] not in PARTICLES) or all(len(t) < 3 for t in g):
                continue
            ph = " ".join(g)
            if (" " + ph + " ") not in e and ph not in seen:
                seen.add(ph); out.append(ph)
    return out[:20]


def suggestions(pairs: list, never: list, dismissed: list, min_count: int = 3) -> list:
    """pairs: [(original, edited)] across recent edits → phrases cut at
    least `min_count` times that aren't already on the never-say list."""
    c = Counter()
    for o, e in pairs:
        for ph in set(cut_phrases(o, e)):
            c[ph] += 1
    nev = {x.strip().lower() for x in (never or [])}
    dis = {x.strip().lower() for x in (dismissed or [])}
    out = []
    for ph, n in c.most_common(30):
        if n < min_count or ph in nev or ph in dis or any(ph in x or x in ph for x in nev):
            continue
        out.append({"phrase": ph, "times": n})
    # shortest distinct phrases first; drop a longer one that contains a shorter one already listed
    out.sort(key=lambda x: (-x["times"], len(x["phrase"])))
    keep = []
    for x in out:
        if not any(k["phrase"] in x["phrase"] for k in keep):
            keep.append(x)
    return keep[:2]


def load(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}
