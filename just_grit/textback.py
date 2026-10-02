"""Missed-call text-back: the business misses a call, the caller gets a text.

The single most-sold feature on GoHighLevel, and the cheapest thing in this
codebase that recovers real money for a customer. A single-operator trade
business misses 20-40% of inbound calls during the day and nearly all of them
after hours; 30-50% of people text back; the worked example is an electrician
recovering about $3,100 a month on $450 jobs. It sits UNDER the AI
receptionist as the smaller yes on the same problem, and the upsell path is
text-back -> receptionist.

── The line this module will not cross ─────────────────────────────────────
Telnyx_Calling_and_SMS_Scope.md sets the standing rule: Just Grit never sends
SMS without prior express written consent. That rule was written for cold
outbound to a prospect list, and it still holds - nothing here can text a
prospect. A text-back is a different instrument: the caller dialled the
business first, gave their number by doing so, and gets one informational
message in direct reply. That is the same basis Podium, Birdeye and every GHL
agency operate on. It is NOT a licence to market.

So the gate is structural, not a comment:

  - `reply_to_missed_call` is the only public sender, and it refuses unless
    handed an inbound-call record that is incoming, unanswered, hung up, and
    RECENT. There is no generic send function to misuse; do not add one.
  - One text per caller per cooldown window, and never after STOP.
  - The template is informational. It says who missed the call and invites
    a reply. It is rendered from settings the customer controls, so it cannot
    be policed for content here - the Setup card says what it is for.
  - Off by default, per workspace.

Two things a lawyer should look at before the first clinic turns it on, in
the same spirit as the barratry line in the ChiroCare doc: (1) whether
"reply to an inbound call" is sufficient consent basis in Texas for the
customer's vertical, and (2) for a healthcare customer, whether a text that
names the clinic is PHI under HIPAA. The default template names the business
and nothing else, on purpose. None of this is legal advice.

Also required before anything sends: the number on a Telnyx messaging
profile with an approved 10DLC campaign. Carriers filter unregistered A2P
traffic, and a filtered number can take the calling down with it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

import telnyx_calls as telnyx

COOLDOWN_HOURS_DEFAULT = 24
MIN_COOLDOWN_HOURS, MAX_COOLDOWN_HOURS = 1, 24 * 14
RECENT_MINUTES = 15          # a webhook replayed hours later must not text
MAX_MESSAGE_CHARS = 320      # two segments; longer reads like a brochure

DEFAULT_MESSAGE = ("Sorry we missed your call at {business} - how can we help? "
                   "Reply here and we'll get right back to you.")

# What carriers and the CTIA treat as an opt-out. Telnyx honours these at the
# network level for 10DLC too; we record them so the app never even tries.
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
HELP_WORDS = {"help", "info"}


def is_stop(text: str) -> bool:
    first = re.sub(r"[^a-z]", "", (text or "").strip().lower().split(" ")[0] if text else "")
    return first in STOP_WORDS


def is_help(text: str) -> bool:
    first = re.sub(r"[^a-z]", "", (text or "").strip().lower().split(" ")[0] if text else "")
    return first in HELP_WORDS


def clean_cooldown(raw, default=COOLDOWN_HOURS_DEFAULT) -> int:
    try:
        n = int(raw)
    except Exception:
        return default
    return n if MIN_COOLDOWN_HOURS <= n <= MAX_COOLDOWN_HOURS else default


def render(template: str, business: str) -> str:
    """The message, with the business name in and the length capped."""
    t = (template or "").strip() or DEFAULT_MESSAGE
    t = t.replace("{business}", (business or "us").strip() or "us")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_MESSAGE_CHARS:
        t = t[:MAX_MESSAGE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return t


def _dt(raw):
    try:
        return datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except Exception:
        return None


def should_text(call: dict, *, now=None, last_texted_at=None, opted_out=False,
                cooldown_hours=COOLDOWN_HOURS_DEFAULT, business_numbers=()) -> tuple:
    """Does this inbound-call record earn a text? Returns (bool, reason).

    Every condition is a reason a text would be wrong, not a heuristic:
    the call has to be theirs, has to have failed, and has to have just
    happened. `call` is a row from inbound_calls.
    """
    now = now or datetime.now(timezone.utc)
    if (call.get("direction") or "") != "incoming":
        return False, "not an inbound call"
    if call.get("answered_at"):
        return False, "the call was answered"
    if not call.get("hung_up_at"):
        return False, "the call has not ended"
    if call.get("texted_at"):
        return False, "already texted for this call"
    frm = telnyx.to_e164(call.get("from_number") or "")
    if not frm:
        return False, "caller has no usable number (anonymous or non-US)"
    own = {telnyx.to_e164(n) for n in business_numbers if n}
    if frm in own:
        return False, "the caller is the business's own number"
    if opted_out:
        return False, "this number said STOP"
    started = _dt(call.get("started_at")) or _dt(call.get("hung_up_at"))
    if not started or (now - started) > timedelta(minutes=RECENT_MINUTES):
        return False, "the call is not recent (webhook replay?)"
    last = _dt(last_texted_at) if last_texted_at else None
    if last and (now - last) < timedelta(hours=cooldown_hours):
        return False, "texted this number within the cooldown window"
    return True, "ok"


def _send(*, api_key: str, from_number: str, to_number: str, text: str,
          messaging_profile_id: str = "") -> str:
    """Private on purpose. The only caller is reply_to_missed_call."""
    body = {"from": telnyx.to_e164(from_number), "to": telnyx.to_e164(to_number),
            "text": text}
    if messaging_profile_id:
        body["messaging_profile_id"] = messaging_profile_id
    data = telnyx._post("/messages", api_key, body)
    return (data or {}).get("id") or ""


def reply_to_missed_call(call: dict, *, api_key: str, from_number: str,
                         template: str, business: str, messaging_profile_id: str = "",
                         now=None, last_texted_at=None, opted_out=False,
                         cooldown_hours=COOLDOWN_HOURS_DEFAULT,
                         business_numbers=()) -> dict:
    """Text the caller back - if, and only if, should_text says so.

    Returns {"sent": bool, "reason": str, "message_id": str, "text": str}.
    Never raises on a refusal; raises TelnyxError only if Telnyx itself does.
    """
    ok, why = should_text(call, now=now, last_texted_at=last_texted_at,
                          opted_out=opted_out, cooldown_hours=cooldown_hours,
                          business_numbers=business_numbers)
    if not ok:
        return {"sent": False, "reason": why, "message_id": "", "text": ""}
    text = render(template, business)
    mid = _send(api_key=api_key, from_number=from_number,
                to_number=call["from_number"], text=text,
                messaging_profile_id=messaging_profile_id)
    return {"sent": True, "reason": "ok", "message_id": mid, "text": text}
