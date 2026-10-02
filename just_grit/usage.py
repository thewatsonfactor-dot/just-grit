# -*- coding: utf-8 -*-
"""Usage communications: telling each customer what Just Grit can do for them,
based on what they have and haven't used, in the product and by email.

Every workspace is a customer. Each one leaves a trail in its own database -
sites audited, emails the AI wrote, emails autopilot sent, replies caught,
missed calls texted back, pictures generated. This module reads that trail as
a *snapshot*, decides where the customer is (new, adopting, power user, going
quiet), and picks at most one thing worth saying:

    in the product  a single card at the top of the app, pointing at the one
                    feature that would help them most right now
    by email        the fallback for people who aren't opening the app, plus
                    a weekly recap of the work the AI did for them

The pure decisions live here - no database, no clock, no network - so they
can be tested exhaustively. webapp.py owns the reads, writes and the send.

Rules this is built on, all enforced below:

  * **Their numbers, not ours.** Every message is built from the customer's
    own data ("3 callers hung up on you this week"). A generic "have you tried
    text-back?" is an ad; a count of their own missed calls is a reason.
  * **One thing at a time.** One in-app card per workspace, highest priority
    first. Two cards is a to-do list nobody asked for.
  * **In-app first, email as fallback.** A nudge that has both channels only
    emails if the in-app card has been up for EMAIL_FALLBACK_DAYS and nobody
    has seen it - i.e. they aren't logging in. Somebody using the product does
    not also need an email about it.
  * **Frequency caps.** At most one email per MIN_EMAIL_GAP_DAYS and
    MAX_EMAILS_PER_WEEK per workspace, weekday working hours only, and each
    nudge has a lifetime cap. One reminder, not a carousel.
  * **Stop when it worked.** Every nudge has a goal - the signal that means the
    customer did the thing. Once the goal is true the nudge is retired and the
    message is marked converted. That is also how it is measured.
  * **No open tracking.** Same reason triggers.py gives: Apple Mail Privacy
    Protection pre-fetches every image, so open rates are invented. We measure
    what customers *do* (the goal) and what they *click* in the product.
  * **An honest control group.** A configurable holdout gets the same
    decision recorded but nothing delivered, so "converted after a message"
    can be compared with "converted anyway". With a handful of customers that
    comparison is noise, and the report says so rather than printing a lift.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

# ── channel orchestration ────────────────────────────────────────────────
EMAIL_FALLBACK_DAYS = 3        # in-app card unseen this long -> email may follow
MIN_EMAIL_GAP_DAYS = 3         # never two product emails closer than this
MAX_EMAILS_PER_WEEK = 2
IN_APP_TTL_DAYS = 14           # a card nobody acted on retires itself
DISMISS_COOLDOWN_DAYS = 7      # "not now" means not for a week
CONVERSION_WINDOW_DAYS = 21    # a goal met later than this isn't credited
MIN_N_FOR_LIFT = 30            # below this per arm, lift is not reported

MODES = ("", "in_app", "email")   # off | in-app only | in-app + email


# ── features: what "using Just Grit" means ───────────────────────────────
# `ai` marks the features where a model does the work. Adoption breadth is
# the share of these a workspace has actually used, not merely switched on.
FEATURES = [
    {"id": "audit",      "name": "AI site audits",      "ai": True,  "tab": "analyzer",
     "used": lambda s: s.get("scans_30", 0) > 0},
    {"id": "writer",     "name": "AI email writer",     "ai": True,  "tab": "outbox",
     "used": lambda s: s.get("drafts_30", 0) > 0},
    {"id": "autopilot",  "name": "Email autopilot",     "ai": True,  "tab": "outbox",
     "used": lambda s: bool(s.get("autopilot_mode")) and s.get("autopilot_sent_30", 0) > 0},
    {"id": "replies",    "name": "Reply detection",     "ai": True,  "tab": "setup",
     "used": lambda s: bool(s.get("mailbox"))},
    {"id": "textback",   "name": "Missed-call text-back", "ai": False, "tab": "setup",
     "used": lambda s: bool(s.get("textback_on")) and bool(s.get("telnyx_ready"))},
    {"id": "images",     "name": "AI images",           "ai": True,  "tab": "social",
     "used": lambda s: s.get("images_30", 0) > 0},
    {"id": "social",     "name": "Social publishing",   "ai": False, "tab": "social",
     "used": lambda s: s.get("posts_30", 0) > 0},
    {"id": "competitors","name": "Competitor watch",    "ai": False, "tab": "watch",
     "used": lambda s: s.get("competitors", 0) > 0},
]


def features_used(snap: dict) -> dict:
    return {f["id"]: bool(f["used"](snap)) for f in FEATURES}


def adoption_score(snap: dict) -> int:
    """Breadth, 0-100. AI features count double - they are the product."""
    used = features_used(snap)
    total = sum(2 if f["ai"] else 1 for f in FEATURES)
    got = sum((2 if f["ai"] else 1) for f in FEATURES if used[f["id"]])
    return round(100 * got / total)


def ai_actions(snap: dict, window: str = "30") -> int:
    """Depth: how many things the AI did for them in the window."""
    keys = ("scans", "drafts", "autopilot_sent", "images", "textbacks", "replies")
    return sum(int(snap.get("%s_%s" % (k, window), 0) or 0) for k in keys)


# ── lifecycle ────────────────────────────────────────────────────────────
STAGES = {
    "new":      "New — first two weeks",
    "adopting": "Adopting",
    "power":    "Power user",
    "at_risk":  "Going quiet",
    "dormant":  "Dormant",
}


def _dt(raw):
    """Tolerant ISO parse -> aware UTC datetime, or None."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip().replace(" ", "T")
    for cut in (len(s), 19, 10):
        try:
            d = datetime.fromisoformat(s[:cut])
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def days_since(raw, now: datetime):
    d = _dt(raw)
    return None if d is None else max(0.0, (now - d).total_seconds() / 86400)


def stage(snap: dict, now: datetime) -> str:
    quiet = days_since(snap.get("last_active_at"), now)
    age = snap.get("age_days")
    if age is not None and age <= 14 and ai_actions(snap) < 5:
        return "new"
    if quiet is None or quiet > 30:
        return "dormant"
    if quiet >= 10:
        return "at_risk"
    if adoption_score(snap) >= 60 and ai_actions(snap) >= 40:
        return "power"
    return "adopting"


# ── plays ────────────────────────────────────────────────────────────────
# A play is one thing worth saying. `when` decides eligibility from the
# snapshot; `goal` is the signal that means it worked (None = no goal, so it
# is a report, not a nudge, and nothing is credited). `channels` lists where
# it may go; `email_first` plays skip the in-app-first rule (a recap or a
# win-back is *for* people who aren't in the app). Lower priority = sooner.

def _n(s, k):
    return int(s.get(k, 0) or 0)


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


PLAYS = [
    {
        "id": "win_back", "priority": 5, "channels": ("email", "in_app"),
        "email_first": True, "cooldown_days": 21, "max_sends": 2,
        "label": "Win-back: going quiet",
        "when": lambda s, now: stage(s, now) in ("at_risk", "dormant")
                               and s.get("last_active_at") is not None,
        "goal": lambda s, now: (days_since(s.get("last_active_at"), now) or 99) < 2,
        "in_app": lambda s: {
            "title": "Welcome back — here's what piled up",
            "body": pile(s) + " Today's list is already in the order worth calling.",
            "cta": "Open today's list", "tab": "today"},
        "email": lambda s, ctx: {
            "subject": "%s: %d leads are waiting on you" % (ctx["business"], _n(s, "due_today")),
            "body": "Hi %s,\n\n"
                    "It's been %d days since anyone worked %s in Just Grit, and the list "
                    "kept moving without you:\n\n"
                    "  - %d %s due a call today\n"
                    "  - %d %s already written and waiting for a yes\n"
                    "  - %d %s nobody has followed up on\n\n"
                    "The fastest way back in is the Today tab - it's in the order "
                    "worth calling:\n%s\n\n"
                    "If the timing's wrong, just reply and tell me.\n" % (
                        ctx["first_name"], int(days_since(s.get("last_active_at"), ctx["now"]) or 0),
                        ctx["business"],
                        _n(s, "due_today"), _plural(_n(s, "due_today"), "lead"),
                        _n(s, "drafts_waiting"), _plural(_n(s, "drafts_waiting"), "email"),
                        _n(s, "replies_unworked"), _plural(_n(s, "replies_unworked"), "reply", "replies"),
                        ctx["link"])},
    },
    {
        "id": "connect_mailbox", "priority": 10, "channels": ("in_app", "email"),
        "cooldown_days": 7, "max_sends": 2,
        "label": "Connect your mailbox",
        "when": lambda s, now: not s.get("mailbox") and _n(s, "drafts_30") + _n(s, "sent_30") > 0,
        "goal": lambda s, now: bool(s.get("mailbox")),
        "in_app": lambda s: {
            "title": "Connect your mailbox so replies stop the sequence",
            "body": "Just Grit can't see who wrote back yet, so it can't pull a reply out of "
                    "the follow-up line - and autopilot stays off until it can. "
                    "About two minutes in Setup.",
            "cta": "Connect it in Setup", "tab": "setup"},
        "email": lambda s, ctx: {
            "subject": "One password and Just Grit can see your replies",
            "body": "Hi %s,\n\n"
                    "You've sent %d %s from Just Grit this month, but it can't read the "
                    "replies yet. That means a follow-up can still go to somebody who "
                    "already wrote back, and autopilot can't switch on.\n\n"
                    "Connecting takes about two minutes: Setup -> Your mailbox, type "
                    "the password, Save.\n%s\n" % (
                        ctx["first_name"], _n(s, "sent_30"), _plural(_n(s, "sent_30"), "email"),
                        ctx["link"] + "#setup")},
    },
    {
        "id": "missed_calls", "priority": 15, "channels": ("in_app", "email"),
        "cooldown_days": 7, "max_sends": 2,
        "label": "Missed calls → turn on text-back",
        "when": lambda s, now: _n(s, "calls_missed_7") >= 1 and not s.get("textback_on"),
        "goal": lambda s, now: bool(s.get("textback_on")),
        "in_app": lambda s: {
            "title": "%d %s hung up without reaching you this week" % (
                _n(s, "calls_missed_7"), _plural(_n(s, "calls_missed_7"), "caller")),
            "body": "Missed-call text-back would have texted each of them within a minute, "
                    "from your number, before they called the next business on Google.",
            "cta": "Turn on text-back", "tab": "setup"},
        "email": lambda s, ctx: {
            "subject": "%d missed %s this week" % (_n(s, "calls_missed_7"),
                                                   _plural(_n(s, "calls_missed_7"), "call")),
            "body": "Hi %s,\n\n"
                    "%d %s rang %s this week and hung up without reaching anyone. "
                    "Most people who don't get an answer call the next business on the list.\n\n"
                    "Text-back sends each of them a short text from your own number within "
                    "a minute - it's already wired to your line, it just needs switching on "
                    "in Setup:\n%s\n" % (
                        ctx["first_name"], _n(s, "calls_missed_7"),
                        _plural(_n(s, "calls_missed_7"), "caller"), ctx["business"],
                        ctx["link"] + "#setup")},
    },
    {
        "id": "drafts_waiting", "priority": 20, "channels": ("in_app",),
        "cooldown_days": 5, "max_sends": 3,
        "label": "Drafts waiting → send or autopilot",
        "when": lambda s, now: _n(s, "drafts_waiting") >= 3 and (s.get("oldest_draft_days") or 0) >= 2
                               and not s.get("autopilot_mode"),
        "goal": lambda s, now: _n(s, "drafts_waiting") == 0 or bool(s.get("autopilot_mode")),
        "in_app": lambda s: {
            "title": "%d emails are written and waiting on you" % _n(s, "drafts_waiting"),
            "body": "The oldest has been sitting %d days. Send them from the Outbox - or let "
                    "autopilot send the follow-ups for you, weekdays only, capped per hour." % int(
                        s.get("oldest_draft_days") or 0),
            "cta": "Open the Outbox", "tab": "outbox"},
    },
    {
        "id": "first_audit", "priority": 30, "channels": ("in_app", "email"),
        "cooldown_days": 7, "max_sends": 1,
        "label": "First AI site audit",
        "when": lambda s, now: _n(s, "scans_total") == 0,
        "goal": lambda s, now: _n(s, "scans_total") > 0,
        "in_app": lambda s: {
            "title": "Run your first site audit",
            "body": "Paste any business's website. In about 20 seconds you get what's broken, "
                    "in words an owner uses - which is the opening line of the call.",
            "cta": "Open the Analyzer", "tab": "analyzer"},
        "email": lambda s, ctx: {
            "subject": "The 20-second opener for your next call",
            "body": "Hi %s,\n\n"
                    "The fastest way to see what Just Grit does: paste one prospect's website "
                    "into the Analyzer. It reads the site the way a customer would and tells "
                    "you what's broken, in plain words - no score to explain, just the thing "
                    "to say when they pick up.\n\n%s\n" % (ctx["first_name"], ctx["link"] + "#analyzer")},
    },
    {
        "id": "try_autopilot", "priority": 40, "channels": ("in_app", "email"),
        "cooldown_days": 10, "max_sends": 2,
        "label": "Heavy manual sender → autopilot",
        "when": lambda s, now: _n(s, "manual_sent_30") >= 10 and not s.get("autopilot_mode")
                               and bool(s.get("mailbox")),
        "goal": lambda s, now: bool(s.get("autopilot_mode")),
        "in_app": lambda s: {
            "title": "You sent %d emails by hand this month" % _n(s, "manual_sent_30"),
            "body": "Autopilot can send the 2nd and 3rd follow-ups for you. It reads your inbox "
                    "first so it never writes to someone who replied, and only sends on "
                    "weekdays during working hours.",
            "cta": "Set up autopilot", "tab": "outbox"},
        "email": lambda s, ctx: {
            "subject": "%d emails by hand this month - want the follow-ups off your plate?" % _n(s, "manual_sent_30"),
            "body": "Hi %s,\n\n"
                    "You've pressed send on %d emails in Just Grit this month. The follow-ups "
                    "- the 2nd and 3rd touch - are the ones people forget, and they're where "
                    "most replies come from.\n\n"
                    "Autopilot sends those for you: weekdays, working hours, a few an hour, "
                    "and it checks your inbox first so nobody who replied gets another email. "
                    "Outbox -> Autopilot -> Follow-ups only.\n%s\n" % (
                        ctx["first_name"], _n(s, "manual_sent_30"), ctx["link"] + "#outbox")},
    },
    {
        "id": "ai_images", "priority": 60, "channels": ("in_app",),
        "cooldown_days": 14, "max_sends": 1,
        "label": "Social connected, no AI images",
        "when": lambda s, now: bool(s.get("social_connected")) and _n(s, "images_total") == 0,
        "goal": lambda s, now: _n(s, "images_total") > 0,
        "in_app": lambda s: {
            "title": "Your social accounts are connected - give them pictures",
            "body": "Describe the post and Just Grit draws the image to go with it, sized for "
                    "each network. About a cent a picture.",
            "cta": "Open Social", "tab": "social"},
    },
    {
        "id": "competitors", "priority": 70, "channels": ("in_app",),
        "cooldown_days": 14, "max_sends": 1,
        "label": "No competitors watched",
        "when": lambda s, now: _n(s, "competitors") == 0 and (s.get("age_days") is None or s["age_days"] >= 7),
        "goal": lambda s, now: _n(s, "competitors") > 0,
        "in_app": lambda s: {
            "title": "Watch three competitors",
            "body": "Pick the businesses you lose jobs to. Just Grit re-reads their sites and "
                    "reviews every week and tells you when something changes.",
            "cta": "Pick competitors", "tab": "watch"},
    },
    {
        "id": "weekly_recap", "priority": 90, "channels": ("email",),
        "email_first": True, "cooldown_days": 6, "max_sends": 10**6, "weekday": 0,
        "label": "Weekly AI recap (Mondays)",
        "when": lambda s, now: ai_actions(s, "7") > 0,
        "goal": None,
        "email": lambda s, ctx: {
            "subject": "What Just Grit did for %s last week" % ctx["business"],
            "body": "Hi %s,\n\nHere's the work Just Grit did for %s in the last 7 days:\n\n%s\n\n"
                    "%s\n\nEverything's on the Today tab:\n%s\n" % (
                        ctx["first_name"], ctx["business"], recap_lines(s),
                        recap_next(s), ctx["link"])},
    },
]

PLAY_BY_ID = {p["id"]: p for p in PLAYS}


def pile(s: dict) -> str:
    """'7 leads due a call, 4 emails waiting on you.' - zeros left out,
    because "0 replies" is noise dressed as information."""
    bits = [(_n(s, "due_today"), "lead due a call", "leads due a call"),
            (_n(s, "drafts_waiting"), "email written and waiting", "emails written and waiting"),
            (_n(s, "replies_unworked"), "reply nobody has answered", "replies nobody has answered")]
    words = ["%d %s" % (n, one if n == 1 else many) for n, one, many in bits if n]
    if not words:
        return "Nothing's overdue."
    return (", ".join(words[:-1]) + " and " + words[-1] if len(words) > 1 else words[0]) + "."


def recap_lines(s: dict) -> str:
    rows = [
        (_n(s, "scans_7"), "website audited", "websites audited"),
        (_n(s, "drafts_7"), "email written", "emails written"),
        (_n(s, "autopilot_sent_7"), "email sent by autopilot", "emails sent by autopilot"),
        (_n(s, "replies_7"), "reply caught", "replies caught"),
        (_n(s, "textbacks_7"), "missed call texted back", "missed calls texted back"),
        (_n(s, "images_7"), "image generated", "images generated"),
    ]
    out = ["  - %d %s" % (n, one if n == 1 else many) for n, one, many in rows if n]
    return "\n".join(out) or "  - nothing yet"


def recap_next(s: dict) -> str:
    """One suggestion, the same one the in-app card would make."""
    if _n(s, "replies_unworked"):
        return "Start with the %d %s nobody has answered yet." % (
            _n(s, "replies_unworked"), _plural(_n(s, "replies_unworked"), "reply", "replies"))
    if _n(s, "due_today"):
        return "%d %s due a call today." % (_n(s, "due_today"), _plural(_n(s, "due_today"), "lead is", "leads are"))
    return "Nothing's overdue - a good week to add new prospects."


# ── decisions ────────────────────────────────────────────────────────────

def in_holdout(slug: str, play_id: str, pct: int) -> bool:
    """Deterministic: the same workspace is always in or out of a play's
    control group, so a re-run can't shuffle it."""
    if not pct:
        return False
    h = int(hashlib.sha256(("%s:%s" % (slug, play_id)).encode()).hexdigest()[:8], 16)
    return (h % 100) < max(0, min(50, int(pct)))


def _rows(history, play=None, channel=None):
    return [h for h in (history or [])
            if (play is None or h.get("play") == play)
            and (channel is None or h.get("channel") == channel)]


def _last(rows, key):
    ds = [d for d in (_dt(r.get(key)) for r in rows) if d]
    return max(ds) if ds else None


def eligible(play: dict, snap: dict, history: list, now: datetime) -> tuple:
    """(ok, reason). Reason is a sentence for the owner's report."""
    pid = play["id"]
    try:
        if play["goal"] and play["goal"](snap, now):
            return False, "already done"
        if not play["when"](snap, now):
            return False, "doesn't apply"
    except Exception as e:                  # a bad snapshot must never take down the loop
        return False, "error: %s" % e
    mine = _rows(history, pid)
    dismissed = _last(mine, "dismissed_at")
    if dismissed and now - dismissed < timedelta(days=DISMISS_COOLDOWN_DAYS):
        return False, "dismissed recently"
    last = _last(mine, "created_at")
    if last and now - last < timedelta(days=play["cooldown_days"]):
        return False, "cooling down"
    if "weekday" in play and now.weekday() != play["weekday"]:
        return False, "only on Mondays"
    return True, "ready"


def active_in_app(history: list, now: datetime):
    """The card currently up, if any."""
    for r in sorted(_rows(history, channel="in_app"),
                    key=lambda r: r.get("created_at") or "", reverse=True):
        if r.get("state") != "active":
            continue
        made = _dt(r.get("created_at"))
        if made and now - made > timedelta(days=IN_APP_TTL_DAYS):
            continue
        return r
    return None


def plan_in_app(snap: dict, history: list, now: datetime):
    """The one play to show in the product, or None. Leaves an existing card
    alone - replacing a card someone is halfway through reading is rude."""
    if active_in_app(history, now):
        return None
    # "Not now" on one card is not an invitation to show the next one.
    if (d := _last(_rows(history, channel="in_app"), "dismissed_at")) and now - d < timedelta(days=1):
        return None
    for play in sorted(PLAYS, key=lambda p: p["priority"]):
        if "in_app" not in play["channels"]:
            continue
        mine = _rows(history, play["id"], "in_app")
        if len(mine) >= play["max_sends"]:
            continue
        ok, _ = eligible(play, snap, mine, now)      # cooldowns are per play and channel
        if ok:
            return play
    return None


def email_budget(history: list, now: datetime) -> tuple:
    """(ok, reason) for the workspace as a whole."""
    sent = [r for r in _rows(history, channel="email") if r.get("state") == "sent"]
    last = _last(sent, "sent_at")
    if last and now - last < timedelta(days=MIN_EMAIL_GAP_DAYS):
        return False, "emailed within the last %d days" % MIN_EMAIL_GAP_DAYS
    week = [r for r in sent if (_dt(r.get("sent_at")) or now) > now - timedelta(days=7)]
    if len(week) >= MAX_EMAILS_PER_WEEK:
        return False, "already had %d emails this week" % MAX_EMAILS_PER_WEEK
    return True, "ready"


def plan_email(snap: dict, history: list, now: datetime) -> tuple:
    """(play or None, reason). In-app first: a play that also has an in-app
    card only emails once that card has been up EMAIL_FALLBACK_DAYS unseen."""
    ok, why = email_budget(history, now)
    if not ok:
        return None, why
    reasons = []
    for play in sorted(PLAYS, key=lambda p: p["priority"]):
        if "email" not in play["channels"]:
            continue
        mine_email = _rows(history, play["id"], "email")
        if len([r for r in mine_email if r.get("state") == "sent"]) >= play["max_sends"]:
            continue
        ok, why = eligible(play, snap, mine_email, now)
        if not ok:
            continue
        if not play.get("email_first") and "in_app" in play["channels"]:
            cards = _rows(history, play["id"], "in_app")
            card = max(cards, key=lambda r: r.get("created_at") or "", default=None)
            if not card:
                reasons.append("%s: in-app card goes first" % play["id"])
                continue
            if card.get("shown_at"):
                reasons.append("%s: they saw it in the app" % play["id"])
                continue
            up = days_since(card.get("created_at"), now) or 0
            if up < EMAIL_FALLBACK_DAYS:
                reasons.append("%s: card up %.1f of %d days" % (play["id"], up, EMAIL_FALLBACK_DAYS))
                continue
        return play, "ready"
    return None, ("; ".join(reasons) or "nothing to say")


def render_in_app(play: dict, snap: dict) -> dict:
    return dict(play["in_app"](snap))


FOOTER = ("\n--\n{sender}\n{company}\n{address}\n\n"
          "You're getting this because {business} uses Just Grit. These are product tips, "
          "not marketing - at most {per_week} a week. Reply \"stop\" and they stop.\n")


def render_email(play: dict, snap: dict, ctx: dict) -> dict:
    out = dict(play["email"](snap, ctx))
    sign = (ctx.get("signoff") or "").strip()
    out["body"] = out["body"].rstrip() + ("\n\n" + sign if sign else "") + "\n" + FOOTER.format(
        sender=ctx.get("sender", ""), company=ctx.get("company", ""),
        address=ctx.get("address", ""), business=ctx["business"], per_week=MAX_EMAILS_PER_WEEK)
    return out


def goals_met(history: list, snap: dict, now: datetime) -> list:
    """Message ids whose play goal is now true, inside the window, not yet
    credited. Holdout rows count too - that is the control."""
    out = []
    for r in history or []:
        play = PLAY_BY_ID.get(r.get("play"))
        if not play or not play["goal"] or r.get("converted_at"):
            continue
        if r.get("state") in ("error",):
            continue
        made = _dt(r.get("created_at"))
        if not made or now - made > timedelta(days=CONVERSION_WINDOW_DAYS):
            continue
        try:
            if play["goal"](snap, now):
                out.append(r["id"])
        except Exception:
            continue
    return out


# ── reporting ────────────────────────────────────────────────────────────

def funnel(rows: list) -> list:
    """Per play and channel: delivered -> seen/clicked -> converted, plus the
    holdout arm. Rates are None when there's nothing to divide by."""
    agg = {}
    for r in rows or []:
        key = (r.get("play"), r.get("channel"))
        a = agg.setdefault(key, {"play": key[0], "channel": key[1], "delivered": 0, "seen": 0,
                                 "clicked": 0, "dismissed": 0, "converted": 0,
                                 "holdout": 0, "holdout_converted": 0, "errors": 0})
        st = r.get("state")
        if st == "holdout":
            a["holdout"] += 1
            a["holdout_converted"] += bool(r.get("converted_at"))
            continue
        if st == "error":
            a["errors"] += 1
            continue
        a["delivered"] += 1
        a["seen"] += bool(r.get("shown_at")) if key[1] == "in_app" else 0
        a["clicked"] += bool(r.get("clicked_at"))
        a["dismissed"] += bool(r.get("dismissed_at"))
        a["converted"] += bool(r.get("converted_at"))
    out = []
    for a in agg.values():
        p = PLAY_BY_ID.get(a["play"]) or {}
        a["label"] = p.get("label", a["play"])
        a["has_goal"] = bool(p.get("goal"))
        a["conv_rate"] = (a["converted"] / a["delivered"]) if a["delivered"] and a["has_goal"] else None
        a["holdout_rate"] = (a["holdout_converted"] / a["holdout"]) if a["holdout"] else None
        enough = a["delivered"] >= MIN_N_FOR_LIFT and a["holdout"] >= MIN_N_FOR_LIFT
        a["lift"] = (a["conv_rate"] - a["holdout_rate"]) if enough and a["conv_rate"] is not None \
            and a["holdout_rate"] is not None else None
        a["lift_note"] = "" if enough else "too few to measure lift (need %d per arm)" % MIN_N_FOR_LIFT
        out.append(a)
    out.sort(key=lambda a: ((PLAY_BY_ID.get(a["play"]) or {}).get("priority", 999), a["channel"]))
    return out


def summarize(snap: dict, now: datetime) -> dict:
    """What the owner's Adoption tab shows for one workspace."""
    used = features_used(snap)
    return {"stage": stage(snap, now), "stage_label": STAGES[stage(snap, now)],
            "adoption": adoption_score(snap), "ai_actions_30": ai_actions(snap, "30"),
            "ai_actions_7": ai_actions(snap, "7"),
            "features": [{"id": f["id"], "name": f["name"], "ai": f["ai"], "used": used[f["id"]]}
                         for f in FEATURES],
            "last_active_at": snap.get("last_active_at"),
            "days_quiet": days_since(snap.get("last_active_at"), now)}
