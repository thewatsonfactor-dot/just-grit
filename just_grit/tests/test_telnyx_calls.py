# -*- coding: utf-8 -*-
"""Telnyx click-to-call: the phone-number formatting, the client_state
round-trip that carries a prospect's number across Telnyx's webhook (Telnyx
itself remembers nothing for us), and the webhook handler's leg-routing logic
- rep answers -> transfer to prospect; anything else -> no-op.

No real Telnyx call is ever placed here. httpx.post is monkeypatched so
these run offline and cost nothing.
"""
import sys
sys.path.insert(0, '.')
import telnyx_calls as telnyx
import webapp as W
from fastapi.testclient import TestClient

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))


# ── to_e164 ──────────────────────────────────────────────────────────────
check("10-digit number gets +1", telnyx.to_e164("2108107476"), "+12108107476")
check("formatted number strips punctuation", telnyx.to_e164("(210) 810-7476"), "+12108107476")
check("11-digit with leading 1 works", telnyx.to_e164("12108107476"), "+12108107476")
check("too short is rejected", telnyx.to_e164("12345"), "")
check("empty is rejected", telnyx.to_e164(""), "")
check("None-ish is rejected", telnyx.to_e164(None), "")


# ── client_state round-trip ──────────────────────────────────────────────
state = telnyx.encode_state(v=1, leg="rep", pid=7, prospect="+12105551234")
decoded = telnyx.decode_state(state)
check("state round-trips leg", decoded.get("leg"), "rep")
check("state round-trips pid", decoded.get("pid"), 7)
check("state round-trips prospect number", decoded.get("prospect"), "+12105551234")
check("garbage state decodes to {}", telnyx.decode_state("not-base64-json!!"), {})
check("empty state decodes to {}", telnyx.decode_state(None), {})


# ── place_bridge_call / transfer_to_prospect talk to Telnyx correctly ────
class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
    def json(self):
        return self._body

posts = []
def fake_post(url, json, timeout, headers):
    posts.append((url, json, headers))
    if url.endswith("/calls"):
        return FakeResp(200, {"data": {"call_control_id": "ccid-123"}})
    return FakeResp(200, {"data": {}})

telnyx.httpx.post = fake_post

ccid = telnyx.place_bridge_call(
    api_key="KEY1", connection_id="CONN1", from_number="2105550100",
    rep_number="2108107476", webhook_url="https://x/webhooks/telnyx/voice/tok",
    pid=9, prospect_number="+12105551234",
)
check("place_bridge_call returns Telnyx's call_control_id", ccid, "ccid-123")
url, body, headers = posts[-1]
check("dials the REP's number, not the prospect's", body["to"], "+12108107476")
check("caller ID is the Telnyx number", body["from"], "+12105550100")
check("carries the prospect's number in client_state, not in the dial itself",
      telnyx.decode_state(body["client_state"]).get("prospect"), "+12105551234")
check("auth header carries the API key", headers["Authorization"], "Bearer KEY1")

posts.clear()
telnyx.transfer_to_prospect(api_key="KEY1", call_control_id="ccid-123",
                             from_number="2105550100", prospect_number="+12105551234", pid=9)
url, body, headers = posts[-1]
check("transfer hits the transfer action on the right call leg",
      url.endswith("/calls/ccid-123/actions/transfer"))
check("transfer finally dials the prospect", body["to"], "+12105551234")


def fake_post_error(url, json, timeout, headers):
    return FakeResp(422, {"errors": [{"detail": "connection_id is invalid"}]})
telnyx.httpx.post = fake_post_error
threw = False
try:
    telnyx.place_bridge_call(api_key="k", connection_id="bad", from_number="2105550100",
                              rep_number="2108107476", webhook_url="https://x", pid=1, prospect_number="+1")
except telnyx.TelnyxError as e:
    threw = "connection_id is invalid" in str(e)
check("a Telnyx error surfaces Telnyx's own detail message", threw)


# ── the webhook handler itself: only the rep's answer triggers a transfer ─
transfer_calls = []
W.telnyx.transfer_to_prospect = lambda **kw: transfer_calls.append(kw)
tok = W.telnyx_webhook_token()
client = TestClient(W.app)

rep_state = W.telnyx.encode_state(v=1, leg="rep", pid=42, prospect="+12105551234")
r = client.post(f"/webhooks/telnyx/voice/{tok}", json={"data": {
    "event_type": "call.answered",
    "payload": {"call_control_id": "ccid-abc", "client_state": rep_state}}})
check("webhook acks the rep-answered event", r.status_code, 200)
check("rep answering triggers exactly one transfer", len(transfer_calls), 1)
check("the transfer carries the right pid", transfer_calls[0]["pid"], 42)
check("the transfer carries the right prospect number", transfer_calls[0]["prospect_number"], "+12105551234")

transfer_calls.clear()
bridged_state = W.telnyx.encode_state(v=1, leg="bridged", pid=42)
client.post(f"/webhooks/telnyx/voice/{tok}", json={"data": {
    "event_type": "call.answered",
    "payload": {"call_control_id": "ccid-abc", "client_state": bridged_state}}})
check("the prospect answering does NOT trigger another transfer (already bridged)",
      len(transfer_calls), 0)

client.post(f"/webhooks/telnyx/voice/{tok}", json={"data": {
    "event_type": "call.hangup",
    "payload": {"call_control_id": "ccid-abc", "client_state": rep_state}}})
check("a hangup event never triggers a transfer", len(transfer_calls), 0)

r_bad = client.post("/webhooks/telnyx/voice/wrong-token", json={"data": {}})
check("a wrong webhook token is rejected", r_bad.status_code, 404)

r_junk = client.post(f"/webhooks/telnyx/voice/{tok}", data="not json at all")
check("a malformed body doesn't 500 the webhook", r_junk.status_code, 200)


print("-" * 70)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
