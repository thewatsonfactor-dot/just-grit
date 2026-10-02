"""Things the system should do on its own when something happens.

Just Grit had exactly one trigger before this: the follow-up day-gap timer.
Everything else was a queue a person works by hand. Apollo's template library
is sixty variations on one idea - when X happens, do Y - and three of them
translate to a local-business list without needing a single new data source,
because the events they fire on are ones this system already detects and then
throws away.

    a reply        someone wrote back, and the card fell out of the queue
    a bounce       the address is dead, and nobody tried the next person
    going cold     the ladder finished, and the lead was never seen again

The pure decisions live here so they can be tested without a mailbox, a
database or a clock. webapp owns the writes.

One that is deliberately NOT here: "call when the email is opened". Apollo
ships it and it is the most-copied template in the category, but it needs a
tracking pixel. Apple Mail Privacy Protection pre-fetches images on every
message it delivers, so a large share of those opens would be invented - and
the pixel itself costs deliverability on a domain that took a month to warm.
A reply is the honest version of the same signal, and it is already detected.
"""
from __future__ import annotations

from datetime import date, datetime
from research import is_placeholder_email

# What the inbox scan calls a human answer it refuses to score, plus the two
# scored kinds a person can tap in the Outbox. All three mean the same thing
# for the queue: stop the machine, put a person on it today.
TALKING_TO_US = {"human", "positive", "negative"}

# One second chance, not a carousel. A prospect who finished the ladder gets
# re-approached once; after that the answer was no. Apollo's equivalent
# template loops forever, which is how a list becomes a nuisance.
REAWAKEN_DAYS_DEFAULT = 90
MIN_REAWAKEN_DAYS, MAX_REAWAKEN_DAYS = 14, 365


def wants_callback(kind: str) -> bool:
    """Does this reply mean a person should ring them today?"""
    return (kind or "").strip().lower() in TALKING_TO_US


def clean_reawaken_days(raw, default=REAWAKEN_DAYS_DEFAULT) -> int:
    try:
        n = int(raw)
    except Exception:
        return default
    return n if MIN_REAWAKEN_DAYS <= n <= MAX_REAWAKEN_DAYS else default


def usable_addresses(contacts, prospect_email: str = "", dead=None) -> list:
    """Every address worth trying for one business, best first.

    Same ranking rule as best_recipient - a named human beats a shared inbox,
    because "Hi Maria" and "Hi there" are not the same email - with dead
    addresses removed. Returns [(email, name), ...].
    """
    dead = {(d or "").strip().lower() for d in (dead or set())}
    named, anon, seen, out = [], [], set(), []
    for ct in (contacts or []):
        addr = ((ct or {}).get("email") or "").strip()
        low = addr.lower()
        if not addr or low in dead or low in seen or is_placeholder_email(low):
            continue
        seen.add(low)
        (named if ((ct.get("name") or "").strip()) else anon).append(
            (addr, (ct.get("name") or "").strip()))
    out.extend(named)
    out.extend(anon)
    fallback = (prospect_email or "").strip()
    if fallback and fallback.lower() not in dead and fallback.lower() not in seen \
            and not is_placeholder_email(fallback):
        out.append((fallback, ""))
    return out


def _as_date(raw):
    """Best-effort date out of whatever the column holds. None if unreadable."""
    s = (raw or "").strip()
    if not s:
        return None
    for cut in (10, 19, len(s)):
        try:
            return datetime.fromisoformat(s[:cut]).date()
        except Exception:
            continue
    return None


def due_for_reawaken(status: str, touched_at, reawakened_at,
                     days: int = REAWAKEN_DAYS_DEFAULT, today=None) -> bool:
    """Has this cold lead been cold long enough to earn one more approach?

    A row that has never been touched is not reawakened. "Cold with no history"
    is a lead the ladder never actually ran on - the fix for that is to work
    it, not to re-work it, and re-approaching somebody who was never
    approached would open with a lie.
    """
    if (status or "").strip().lower() != "cold":
        return False
    if (reawakened_at or "").strip():
        return False                       # already had its second chance
    last = _as_date(touched_at)
    if last is None:
        return False
    today = today or date.today()
    return (today - last).days >= max(1, int(days))
