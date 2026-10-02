"""Click-to-call through Telnyx's Call Control API.

This module has exactly one calling mode: dial the rep's own phone, and only
once the rep has actually picked up, transfer that live call to the
prospect. There is no function here that dials a prospect's number without a
human already being live on the line - no queue, no list, no schedule. That
is deliberate, not an oversight: the TCPA's safe harbor for calling cell
phones without prior written consent covers calls a person dials one at a
time, on purpose. An autodialer - anything that works down a list on its
own - loses that protection. Do not add a bulk-dial function to this file;
if the business ever needs one, it needs a signed consent trail first, and
that is a different, much bigger decision than adding a function.

Flow:
  1. place_bridge_call() dials the rep. Telnyx calls back to
     WEBHOOK_URL/{token} with call.answered once the rep picks up.
  2. The webhook handler (in webapp.py) reads client_state off that event -
     it carries the prospect's number, base64'd, because Telnyx will not
     remember anything for us - and calls transfer_to_prospect(), which
     dials the prospect and bridges the two together in one step.
  3. Either party hanging up ends the whole thing; Telnyx tears down both
     legs. No further action needed for a v1.

Nothing in this file talks to Just Grit's database. It only knows how to
place and steer calls; webapp.py decides which prospect and when.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

import httpx

API_BASE = "https://api.telnyx.com/v2"
_TIMEOUT = 10


class TelnyxError(RuntimeError):
    """Telnyx rejected the request or the response didn't look right."""


def to_e164(raw: str) -> str:
    """Best-effort US phone -> E.164. Returns '' if it doesn't look like one.

    Just Grit is a San Antonio / New Braunfels shop with no non-US numbers
    in it anywhere else in the codebase (_tidy_phone in webapp.py makes the
    same assumption) - so "10 digits, assume +1" is consistent with how
    every other phone field here already behaves, not a new limitation.
    """
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return ""
    return "+1" + d


def encode_state(**fields) -> str:
    return base64.b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode()


def decode_state(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception:
        return {}


def _post(path: str, api_key: str, body: dict) -> dict:
    try:
        r = httpx.post(f"{API_BASE}{path}", json=body, timeout=_TIMEOUT,
                        headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise TelnyxError(f"couldn't reach Telnyx ({type(e).__name__})") from e
    if r.status_code >= 400:
        detail = r.text[:400]
        try:
            errs = r.json().get("errors") or []
            if errs:
                detail = "; ".join(e.get("detail") or e.get("title") or "" for e in errs)
        except Exception:
            pass
        raise TelnyxError(f"Telnyx said no ({r.status_code}): {detail}")
    try:
        return r.json().get("data") or {}
    except Exception:
        return {}


def place_bridge_call(*, api_key: str, connection_id: str, from_number: str,
                       rep_number: str, webhook_url: str, pid: int,
                       prospect_number: str) -> str:
    """Dial the rep. The prospect is NOT called yet - see the module docstring.

    Returns Telnyx's call_control_id for the rep's leg.
    """
    to = to_e164(rep_number)
    frm = to_e164(from_number)
    if not to:
        raise TelnyxError(f"your own number ({rep_number!r}) doesn't look like a US phone number")
    if not frm:
        raise TelnyxError(f"the Telnyx caller-ID number ({from_number!r}) doesn't look like a US phone number")
    data = _post("/calls", api_key, {
        "connection_id": connection_id,
        "to": to,
        "from": frm,
        "webhook_url": webhook_url,
        "timeout_secs": 25,
        "client_state": encode_state(v=1, leg="rep", pid=pid, prospect=prospect_number),
    })
    ccid = data.get("call_control_id")
    if not ccid:
        raise TelnyxError("Telnyx accepted the call but didn't return a call_control_id")
    return ccid


def transfer_to_prospect(*, api_key: str, call_control_id: str, from_number: str,
                          prospect_number: str, pid: int) -> None:
    """The rep answered - dial the prospect into the same call.

    Telnyx's transfer action dials `to` and bridges it into the existing
    call the moment it's answered; there's no separate bridge step needed.
    """
    _post(f"/calls/{call_control_id}/actions/transfer", api_key, {
        "to": prospect_number,
        "from": to_e164(from_number),
        "client_state": encode_state(v=1, leg="bridged", pid=pid),
    })


def hangup(*, api_key: str, call_control_id: str) -> None:
    try:
        _post(f"/calls/{call_control_id}/actions/hangup", api_key, {})
    except TelnyxError:
        pass   # already down is not a failure worth surfacing
