"""A workspace's configuration, as a file you can carry to the next customer.

GoHighLevel's whole agency business runs on one idea they call a snapshot: a
saved copy of a sub-account's *configuration*, loadable into an empty new
sub-account. What it copies is the work somebody did - workflows, templates,
pipelines. What it refuses to copy is what the account accumulated - contacts,
credentials, phone numbers, billing. That second half is the product. Without
it a snapshot is just a database dump with a nicer name.

Just Grit already had both halves and no name for them. `workspace_registry`
is the empty sub-account; the offer catalog, the call windows and the
follow-up gaps are the things worth carrying. This module is the file format
in between.

**The list of what travels is an allow-list, and that is the whole safety
argument.** A deny-list fails open: the day somebody adds `openai_key` to
settings, every snapshot exported after that quietly carries it, and nothing
errors. An allow-list fails closed - a new setting is simply not in the file
until a person puts it there on purpose. Same instinct as isolating
workspaces by file rather than by a WHERE clause: prefer the leak you cannot
write to the leak you merely try hard to avoid.

What travels:
    the offer catalog        the copy - what you sell and the evidence for it
    call windows             when this kind of business answers the phone
    follow-up gaps           how many days between touches, and when to stop
    reawaken days            how long a cold lead sits before one more try

What does not, and why each one is deliberate:
    prospects, contacts,     another customer's list. Never read - not
    outreach, touches,       filtered out, not scrubbed: this module does not
    deals, feedback          open those tables at all.
    sender_* , legal_entity  who the mail is from. A snapshot that carried it
                             would send the new customer's email as the old
                             customer, which is the worst bug in the system.
    telnyx_*, google_key,    credentials. Obvious, and still worth naming.
    allowed_emails
    public_base_url          points at somebody else's host.
    default_city             a template for clinics is not a template for
                             San Antonio clinics.
    daily_email_cap          the least obvious one. Daniel's is 999999 because
                             he turned it off knowingly on a warmed domain.
                             Carrying that number into a brand-new customer
                             would uncap a cold sending domain on day one -
                             the exact deliverability mistake the rest of the
                             system is built to prevent. A cap is an
                             operational decision about one domain's history,
                             not a piece of a vertical template.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

SNAPSHOT_VERSION = 1
MARKER = "just_grit_snapshot"

# Where each travelling piece lives in a workspace's settings.
SETTING_WINDOWS = "call_windows"
SETTING_GAPS = "followup_gaps"
SETTING_MAX_STEP = "max_email_step"
SETTING_REAWAKEN = "reawaken_days"

FIELDS = {MARKER, "name", "vertical", "note", "created_at", "source_workspace",
          "catalog", SETTING_WINDOWS, SETTING_GAPS, SETTING_MAX_STEP,
          SETTING_REAWAKEN}

_VERTICAL_RX = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")

MAX_VERTICALS = 40
MAX_WINDOWS_PER_VERTICAL = 4
MIN_STEP, MAX_STEP = 2, 10
MIN_GAP_DAYS, MAX_GAP_DAYS = 1, 90


# ── the pieces ───────────────────────────────────────────────────────────

def clean_windows(raw) -> dict:
    """{vertical: [[start_h, start_m, end_h, end_m], ...]}, or {}.

    A window that ends before it starts is dropped rather than repaired. The
    two readings of `[16, 0, 9, 0]` - overnight, or a typo for 9-to-16 - are
    equally plausible, and guessing wrong means calling somebody at 3am. A
    dropped window falls back to the code default, which is merely unhelpful.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for vert, wins in list(raw.items())[:MAX_VERTICALS]:
        if not isinstance(vert, str) or not _VERTICAL_RX.match(vert.strip()):
            continue
        if not isinstance(wins, (list, tuple)):
            continue
        keep = []
        for w in list(wins)[:MAX_WINDOWS_PER_VERTICAL]:
            if not isinstance(w, (list, tuple)) or len(w) != 4:
                continue
            try:
                a, b, c, d = (int(x) for x in w)
            except Exception:
                continue
            if not (0 <= a <= 23 and 0 <= c <= 23 and 0 <= b <= 59 and 0 <= d <= 59):
                continue
            if a * 60 + b >= c * 60 + d:
                continue
            keep.append([a, b, c, d])
        if keep:
            out[vert.strip()] = keep
    return out


def clean_gaps(raw) -> dict:
    """{step: days_since_previous_send}. Steps are ints; JSON makes them strings."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for step, days in raw.items():
        try:
            s, d = int(step), int(days)
        except Exception:
            continue
        if MIN_STEP <= s <= MAX_STEP and MIN_GAP_DAYS <= d <= MAX_GAP_DAYS:
            out[s] = d
    return out


def clean_max_step(raw, default=3) -> int:
    try:
        n = int(raw)
    except Exception:
        return default
    return n if 1 <= n <= MAX_STEP else default


MIN_REAWAKEN_DAYS, MAX_REAWAKEN_DAYS = 14, 365


def clean_reawaken(raw, default=90) -> int:
    """How long a finished lead stays cold before one more approach.

    Floored at two weeks because anything shorter is not a second chance,
    it is the same campaign with a pause in it.
    """
    try:
        n = int(raw)
    except Exception:
        return default
    return n if MIN_REAWAKEN_DAYS <= n <= MAX_REAWAKEN_DAYS else default


# ── the file ─────────────────────────────────────────────────────────────

def build(*, name: str, catalog: dict, windows=None, gaps=None, max_step=3,
          reawaken=90, vertical: str = "", note: str = "",
          source_workspace: str = "") -> dict:
    """Assemble a snapshot. Pure - takes already-read values, opens nothing."""
    offers = (catalog or {}).get("offers") or {}
    snap = {
        MARKER: SNAPSHOT_VERSION,
        "name": (name or "Untitled snapshot").strip(),
        "vertical": (vertical or "").strip().lower(),
        "note": (note or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_workspace": (source_workspace or "").strip(),
        "catalog": {"offers": offers},
        SETTING_WINDOWS: clean_windows(windows or {}),
        SETTING_GAPS: {str(k): v for k, v in clean_gaps(gaps or {}).items()},
        SETTING_MAX_STEP: clean_max_step(max_step),
        SETTING_REAWAKEN: clean_reawaken(reawaken),
    }
    assert set(snap) == FIELDS, "snapshot grew a field nobody allow-listed"
    return snap


def parse(raw, catalog_cleaner) -> dict:
    """Read a snapshot. Returns {} for anything unusable; never raises.

    `catalog_cleaner` is webapp's parse_catalog, passed in rather than
    imported so this module stays testable on its own and webapp keeps
    owning what a valid offer looks like.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    try:
        version = int(raw.get(MARKER) or 0)
    except Exception:
        return {}
    if version < 1 or version > SNAPSHOT_VERSION:
        return {}
    try:
        catalog = catalog_cleaner(json.dumps(raw.get("catalog") or {}))
    except Exception:
        catalog = {}
    out = {
        MARKER: version,
        "name": str(raw.get("name") or "Untitled snapshot").strip()[:120],
        "vertical": str(raw.get("vertical") or "").strip().lower()[:31],
        "note": str(raw.get("note") or "").strip()[:500],
        "created_at": str(raw.get("created_at") or "")[:40],
        "source_workspace": str(raw.get("source_workspace") or "").strip()[:40],
        "catalog": {"offers": (catalog or {}).get("offers") or {}},
        SETTING_WINDOWS: clean_windows(raw.get(SETTING_WINDOWS)),
        SETTING_GAPS: {str(k): v for k, v in clean_gaps(raw.get(SETTING_GAPS)).items()},
        SETTING_MAX_STEP: clean_max_step(raw.get(SETTING_MAX_STEP)),
        SETTING_REAWAKEN: clean_reawaken(raw.get(SETTING_REAWAKEN)),
    }
    if not out["vertical"] or not _VERTICAL_RX.match(out["vertical"]):
        out["vertical"] = ""
    if not out["catalog"]["offers"]:
        return {}          # a snapshot with no offers is an empty box
    return out


# ── the belt to go with the braces ───────────────────────────────────────
# The allow-list stops settings from travelling. It cannot stop a person from
# typing an API key into an offer's copy, which is a real thing people do when
# a form says "what would you do about it". This does not refuse the export -
# a guess that blocks you with no override is worse than the thing it prevents
# - it just says where to look before the file leaves the building.

_SECRET_RX = [
    (re.compile(r"\bKEY[0-9A-F]{8,}", re.I), "Telnyx-style key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "OpenAI-style key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "Google API key"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.I), "bearer token"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "long opaque string"),
]


def suspect_secrets(snap: dict) -> list:
    """Field paths whose text looks like a credential. Advisory only."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k) if path else str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
        elif isinstance(node, str):
            for rx, what in _SECRET_RX:
                if rx.search(node):
                    found.append("%s looks like it contains a %s" % (path, what))
                    return

    walk(snap, "")
    return found
