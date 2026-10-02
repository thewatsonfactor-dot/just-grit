# -*- coding: utf-8 -*-
"""The inbound-call CRM-logging webhook a Telnyx AI Assistant's Webhook tool
calls: find-or-create by phone, never lands in the outbound cold-call queue,
a requested callback time becomes a same-day Today task, and a wrong token
or junk body never breaks the call the assistant is still on.

Runs against a throwaway sqlite file in a temp dir (webapp.DATA is
redirected there before init_db() creates the schema) - never the live
justgrit.db. Do not remove that redirect: this webhook's whole job is
writing prospects/contacts/touches rows, so unlike the read-mostly tests
elsewhere, an in-memory swap isn't optional here.
"""
import sys, pathlib, tempfile
sys.path.insert(0, '.')

import webapp as W
W.DATA = pathlib.Path(tempfile.mkdtemp(prefix="justgrit-test-"))
W.init_db()

from fastapi.testclient import TestClient
from contextlib import closing

client = TestClient(W.app)
tok = W.telnyx_webhook_token()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))


def prospect(pid):
    with closing(W.db()) as c:
        return dict(c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone())


# ── a wrong token is rejected, same as the voice webhook ──────────────────
r = client.post("/webhooks/telnyx/assistant/wrong-token", json={"caller_phone": "2105551234"})
check("wrong token rejected", r.status_code, 404)

# ── a junk/empty body never errors - nothing to log, but no 500 either ────
r = client.post(f"/webhooks/telnyx/assistant/{tok}", data="not json at all")
check("malformed body doesn't crash the webhook", r.status_code, 200)
check("malformed body reports ok:false (nothing usable to log)", r.json().get("ok"), False)

r = client.post(f"/webhooks/telnyx/assistant/{tok}", json={"caller_name": "No Phone Guy"})
check("missing phone reports ok:false rather than 400ing the assistant's tool call",
      r.json().get("ok"), False)


# ── a normal logged call: new prospect, contact, touch, stays out of queue ─
r = client.post(f"/webhooks/telnyx/assistant/{tok}", json={
    "caller_phone": "(210) 555-9911", "caller_name": "Maria Alvarez",
    "reason": "asking about a company website refresh",
})
check("new inbound call reports ok", r.json().get("ok"), True)
pid = r.json()["prospect_id"]
p = prospect(pid)
check("place_id is the synthetic phone key", p["place_id"], "phone:+12105559911")
check("status is 'inbound', not 'new' (must never enter the cold-call queue)",
      p["status"], "inbound")
check("stage defaults to 'lead'", p["stage"], "lead")
check("next_due stays NULL - a routine logged call is not a Today task",
      p["next_due"], None)

with closing(W.db()) as c:
    contacts = [dict(x) for x in c.execute(
        "SELECT * FROM contacts WHERE prospect_id=?", (pid,)).fetchall()]
    touches = [dict(x) for x in c.execute(
        "SELECT * FROM touches WHERE prospect_id=?", (pid,)).fetchall()]
check("a contact was created for the caller", len(contacts), 1)
check("contact name captured", contacts[0]["name"], "Maria Alvarez")
check("exactly one touch logged", len(touches), 1)
check("touch kind is ai_receptionist", touches[0]["kind"], "ai_receptionist")
check("touch note carries the reason", "website refresh" in touches[0]["note"], True)


# ── calling back rings the SAME prospect, doesn't create a duplicate ──────
r2 = client.post(f"/webhooks/telnyx/assistant/{tok}", json={
    "caller_phone": "2105559911", "caller_name": "Maria Alvarez",
    "reason": "following up",
})
check("same phone number reuses the same prospect id", r2.json()["prospect_id"], pid)
with closing(W.db()) as c:
    n = c.execute("SELECT COUNT(*) c FROM prospects WHERE place_id=?",
                   ("phone:+12105559911",)).fetchone()["c"]
    n_contacts = c.execute("SELECT COUNT(*) c FROM contacts WHERE prospect_id=?", (pid,)).fetchone()["c"]
check("no duplicate prospect row", n, 1)
check("no duplicate contact row for the same name+phone", n_contacts, 1)


# ── a requested callback time becomes a same-day Today task ───────────────
r3 = client.post(f"/webhooks/telnyx/assistant/{tok}", json={
    "caller_phone": "2105552222", "caller_name": "New Guy",
    "reason": "wants a quote", "requested_time": "Thursday at 2pm",
})
pid3 = r3.json()["prospect_id"]
p3 = prospect(pid3)
check("an unconfirmed requested time schedules a same-day callback",
      p3["next_due"] is not None, True)
check("next_action is 'call' so it shows on Today like any other queue card",
      p3["next_action"], "call")

# ── but if the assistant already live-transferred the call, don't also ────
# queue a redundant callback - Daniel is (or was) already on the phone
r4 = client.post(f"/webhooks/telnyx/assistant/{tok}", json={
    "caller_phone": "2105553333", "requested_time": "tomorrow morning",
    "transferred": True,
})
pid4 = r4.json()["prospect_id"]
p4 = prospect(pid4)
check("a transferred call does not also create a redundant Today callback",
      p4["next_due"], None)
with closing(W.db()) as c:
    t = dict(c.execute("SELECT * FROM touches WHERE prospect_id=?", (pid4,)).fetchone())
check("a transferred call is logged with outcome 'transferred'", t["outcome"], "transferred")


# ── a hot/urgent call is flagged in the touch outcome for visibility ──────
r5 = client.post(f"/webhooks/telnyx/assistant/{tok}", json={
    "caller_phone": "2105554444", "urgency": "hot", "reason": "site is down right now",
})
pid5 = r5.json()["prospect_id"]
with closing(W.db()) as c:
    t5 = dict(c.execute("SELECT * FROM touches WHERE prospect_id=?", (pid5,)).fetchone())
check("a hot call is flagged 'hot' in the touch outcome", t5["outcome"], "hot")


print("-" * 70)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
