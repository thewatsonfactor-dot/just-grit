# -*- coding: utf-8 -*-
"""Did the email that went out match the email that was drafted?

The Outbox hands a draft to a `mailto:` link and Apple Mail sends it. Between
those two steps Daniel can rewrite anything, and the app never sees it - the
outreach row keeps the draft, the scoreboard files the reply (or the silence)
under a variant whose copy never actually went out. Every number on that
board is then a little bit fiction, and the amount of fiction is unknown.

The only ground truth is the Sent folder. This module is the pure half of
reading it: given a draft and the message that actually left, say whether
they are the same email. No network, no database - inbox.py fetches, webapp
writes, this decides.

The comparison is on WORDS, not lines. Apple Mail re-wraps plain text at
~76 columns (format=flowed) and may append a signature, so a line-for-line
diff would call every single send "edited". Word order survives wrapping;
a signature is an insertion after the last matched word and is ignored;
a rewritten paragraph is a run of deleted draft words and inserted new ones
in the middle, which is what we are looking for.
"""
import difflib
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

# Anything from one of these lines down is the reader's quoted history or a
# mail-client signature, not the message. Same family as inbox.strip_quoted,
# duplicated here so this module stays import-free of it.
_CUT_AT = [
    re.compile(r"^\s*On .{5,200} wrote:\s*$", re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I | re.M),
    re.compile(r"^\s*Sent from my \w+", re.I | re.M),
    re.compile(r"^\s*From: .+\nSent: .+\nTo: .+", re.M),
]
_SUBJ_PREFIX = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9'’.\-@/$%&+]*", re.I)

# Thresholds. Below MIN_COVERAGE of the draft's words surviving, or more than
# max(MIN_INSERTED, INSERT_FRACTION * draft length) new words appearing
# INSIDE the message (not appended after it), it is a different email.
MIN_COVERAGE = 0.90
MIN_INSERTED = 8
INSERT_FRACTION = 0.10
MATCH_WINDOW_DAYS = 3        # a Sent message this far from sent_at is the same send


def strip_history(body: str) -> str:
    """Everything above the first quoted-reply / signature marker."""
    t = body or ""
    cut = len(t)
    for rx in _CUT_AT:
        m = rx.search(t)
        if m and m.start() < cut:
            cut = m.start()
    t = t[:cut]
    # quoted lines, and the "-- " signature separator with all that follows
    lines = []
    for ln in t.splitlines():
        if ln.startswith(">"):
            continue
        if ln.rstrip() == "--":
            break
        lines.append(ln)
    return "\n".join(lines)


def words(text: str) -> list:
    """Lower-cased word tokens. Whitespace, wrapping and punctuation-only
    tokens vanish, which is the point."""
    return [w.lower().strip(".,;:!?'’\"()") for w in _WORD.findall(text or "")
            if w.strip(".,;:!?'’\"()")]


def norm_subject(s: str) -> str:
    return re.sub(r"\s+", " ", _SUBJ_PREFIX.sub("", s or "")).strip().lower()


def compare(draft_subject: str, draft_body: str,
            sent_subject: str, sent_body: str) -> dict:
    """The verdict, with its working shown.

    Returns {edited, subject_changed, coverage, inserted, deleted, draft_words,
             trailing, reason}. `trailing` is how many words were appended after
    the last matched draft word - a signature, usually - and is NOT held
    against the send.
    """
    d = words(draft_body)
    s = words(strip_history(sent_body))
    subj_changed = norm_subject(draft_subject) != norm_subject(sent_subject)

    if not d:
        return {"edited": subj_changed, "subject_changed": subj_changed,
                "coverage": 1.0, "inserted": 0, "deleted": 0, "draft_words": 0,
                "trailing": len(s),
                "reason": "changed the subject" if subj_changed else "empty draft"}

    sm = difflib.SequenceMatcher(None, d, s, autojunk=False)
    ops = sm.get_opcodes()
    # find where the last 'equal' block ends in the sent text; anything the
    # sent text adds after that is trailing (signature), not an edit
    last_equal_end = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            last_equal_end = j2

    matched = inserted = deleted = trailing = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            matched += i2 - i1
        elif tag == "delete":
            deleted += i2 - i1
        elif tag == "insert":
            if j1 >= last_equal_end:
                trailing += j2 - j1
            else:
                inserted += j2 - j1
        elif tag == "replace":
            deleted += i2 - i1
            if j1 >= last_equal_end:
                trailing += j2 - j1
            else:
                inserted += j2 - j1

    coverage = matched / len(d)
    allowed_inserts = max(MIN_INSERTED, int(INSERT_FRACTION * len(d)))
    edited = subj_changed or coverage < MIN_COVERAGE or inserted > allowed_inserts

    bits = []
    if subj_changed:
        bits.append("changed the subject")
    if deleted:
        bits.append("cut %d word%s" % (deleted, "" if deleted == 1 else "s"))
    if inserted:
        bits.append("added %d word%s" % (inserted, "" if inserted == 1 else "s"))
    reason = ("; ".join(bits) if bits else "went out as drafted")
    if not edited and bits:
        reason = "minor: " + reason
    return {"edited": edited, "subject_changed": subj_changed,
            "coverage": round(coverage, 3), "inserted": inserted,
            "deleted": deleted, "draft_words": len(d), "trailing": trailing,
            "reason": reason}


# ── which outreach row is this Sent message? ────────────────────────────
def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pick_nearest(rows: Iterable[dict], msg_date: str,
                 window_days: int = MATCH_WINDOW_DAYS) -> Optional[dict]:
    """Of several sends to the same address (first email, follow-ups), the
    one whose sent_at is closest to the message's Date - inside the window,
    or nothing. A Sent message with no parseable date matches the most
    recent row rather than guessing further."""
    when = _parse_dt(msg_date)
    rows = list(rows)
    if not rows:
        return None
    if when is None:
        return max(rows, key=lambda r: r.get("sent_at") or "")
    best, best_gap = None, None
    for r in rows:
        at = _parse_dt(r.get("sent_at") or "")
        if at is None:
            continue
        gap = abs((at - when).total_seconds())
        if gap <= window_days * 86400 and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


# ── which IMAP folder is "Sent"? ─────────────────────────────────────────
_LIST_LINE = re.compile(r'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)\s*$')
COMMON_SENT_NAMES = ["Sent", "Sent Items", "Sent Messages", "INBOX.Sent",
                     "INBOX/Sent", "[Gmail]/Sent Mail", "Sent Mail"]


def parse_list_line(line: str) -> Optional[dict]:
    """One line of an IMAP LIST response -> {attrs: set, name: str}."""
    m = _LIST_LINE.match((line or "").strip())
    if not m:
        return None
    attrs = {a.strip().lower() for a in m.group("attrs").split() if a.strip()}
    name = m.group("name")
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return {"attrs": attrs, "name": name}


def find_sent_folder(list_lines: Iterable[str]) -> Optional[str]:
    """RFC 6154 first (the server SAYS which one is Sent), then the usual
    names. None if nothing plausible - never a guess that reads the wrong
    folder and calls every draft edited."""
    folders = [f for f in (parse_list_line(l) for l in list_lines) if f]
    for f in folders:
        if "\\sent" in f["attrs"]:
            return f["name"]
    names = {f["name"].lower(): f["name"] for f in folders}
    for cand in COMMON_SENT_NAMES:
        if cand.lower() in names:
            return names[cand.lower()]
    return None
