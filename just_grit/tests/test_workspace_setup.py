# -*- coding: utf-8 -*-
"""Can a customer finish their own setup, in their own workspace, from the
app itself?

That is the whole question this file answers, because the answer used to be
no: `public_base_url` was read in a dozen places - the Telnyx webhooks, the
media URLs Meta fetches, the review cards - and settable in none of them.
A new customer workspace could be created, opened, and then got stuck, with
the UI telling them to "set public_base_url in Setup" where no such field
existed. It took a SQL client, which is not a setup step you can put in a
customer's hands.

So: create a workspace the way the app does, fill it in the way a customer
would, and check that missed-call text-back comes out the far end ready -
with that customer's own Telnyx account, and nothing of theirs visible to
anybody else.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-wssetup-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
for _s in list(ws.WORKSPACES):
    _t = ws.CURRENT.set(_s)
    try:
        W.init_db()
    finally:
        ws.CURRENT.reset(_t)

OWNER = "daniel@thewatsonfactor.dev"
CUSTOMER = "front@blancocafe.com"

class Req:
    headers = {}; cookies = {}
    class client: host = "10.0.0.9"       # not loopback, so auth is real

_real_caller = W.caller_email

def as_(email):
    W.caller_email = lambda r: email

def in_ws(slug, fn, *a, **kw):
    tok = ws.CURRENT.set(slug)
    try:
        return fn(*a, **kw)
    finally:
        ws.CURRENT.reset(tok)

as_(OWNER)
in_ws(ws.PRIMARY, W.set_setting, "allowed_emails", OWNER)

# ── the owner onboards a customer: a row and a file, no code edit ────────
W.create_workspace(Req(), W.NewWorkspaceBody(
    slug="blancocafe", name="Blanco Cafe", host="grit.blancocafe.com"))
check("the workspace exists after one call", ws.exists("blancocafe"), True)
check("  with its own database file",
      ws.WORKSPACES["blancocafe"]["db"] != ws.WORKSPACES[ws.PRIMARY]["db"], True)

# the customer is given a login to their own workspace and nobody else's
in_ws("blancocafe", W.set_setting, "workspace_emails", CUSTOMER)

# ── before they touch anything ───────────────────────────────────────────
as_(CUSTOMER)
me = in_ws("blancocafe", W.me, Req())
check("text-back reports itself not ready yet", me["textback"]["ready"], False)
check("  and not enabled", me["textback"]["enabled"], False)
check("the webhook address is already known, from the registered hostname",
      me["public_base"], "https://grit.blancocafe.com")
check("  so Setup can show a Telnyx voice webhook URL on day one",
      in_ws("blancocafe", W.telnyx_webhook_url).startswith(
          "https://grit.blancocafe.com/webhooks/telnyx/voice/"), True)
check("  and an SMS webhook URL, which used to be blank forever",
      in_ws("blancocafe", W.telnyx_sms_webhook_url).startswith(
          "https://grit.blancocafe.com/webhooks/telnyx/sms/"), True)
check("the two webhook URLs share one token",
      in_ws("blancocafe", W.telnyx_webhook_url).rsplit("/", 1)[-1],
      in_ws("blancocafe", W.telnyx_sms_webhook_url).rsplit("/", 1)[-1])
check("the token is this workspace's own, not the account's",
      in_ws("blancocafe", W.telnyx_webhook_token) !=
      in_ws(ws.PRIMARY, W.telnyx_webhook_token), True)

# ── the customer fills in their own Telnyx account, from the app ─────────
in_ws("blancocafe", W.save_settings, Req(), W.Settings(
    telnyx_api_key="KEY_blanco_secret_0001",
    telnyx_connection_id="2000111222",
    telnyx_from_number="(830) 555-0100",
    telnyx_rep_number="(830) 555-0199",
    telnyx_messaging_profile_id="40017788",
    textback_enabled="1",
    textback_message="Sorry we missed your call at {business} - how can we help?",
    textback_forward_to="(830) 555-0111",
    textback_cooldown_hours="12"))

st = in_ws("blancocafe", W.textback_status)
check("after one save, text-back is ready", st["ready"], True)
check("  and on", st["enabled"], True)
check("  with their own message", "how can we help" in st["message"], True)
check("  their own cooldown", st["cooldown_hours"], 12)
check("  and a messaging profile on file", st["has_messaging_profile"], True)

# ── nothing of theirs leaks, in either direction ─────────────────────────
check("the customer's Telnyx key is not in the account's workspace",
      in_ws(ws.PRIMARY, W.setting, "telnyx_api_key", ""), "")
check("their text-back stays off for everyone else",
      in_ws(ws.PRIMARY, W.textback_status)["enabled"], False)
me = in_ws("blancocafe", W.me, Req())
check("/api/me never hands back the API key itself",
      "KEY_blanco_secret_0001" in repr(me), False)

# a guest cannot reach past their own business
try:
    in_ws("blancocafe", W.save_settings, Req(), W.Settings(allowed_emails="attacker@x.com"))
    got = "allowed"
except Exception as e:
    got = getattr(e, "status_code", None)
check("a customer cannot edit the account-wide owner list", got, 403)
check("  and it is unchanged", in_ws(ws.PRIMARY, W.setting, "allowed_emails", ""), OWNER)

# ── the address can be overridden, and is checked ───────────────────────
in_ws("blancocafe", W.save_settings, Req(),
      W.Settings(public_base_url="https://sms.blancocafe.com/"))
check("a typed address wins over the registered hostname",
      in_ws("blancocafe", W.public_base), "https://sms.blancocafe.com")
check("  trailing slash dropped, so no // in the URL given to a carrier",
      in_ws("blancocafe", W.telnyx_sms_webhook_url).startswith(
          "https://sms.blancocafe.com/webhooks/"), True)
in_ws("blancocafe", W.save_settings, Req(), W.Settings(public_base_url="grit.blancocafe.com"))
check("a bare hostname is accepted and made https",
      in_ws("blancocafe", W.public_base), "https://grit.blancocafe.com")
for bad in ("not a host", "http://", "localhost"):
    try:
        in_ws("blancocafe", W.save_settings, Req(), W.Settings(public_base_url=bad))
        got = "allowed"
    except Exception as e:
        got = getattr(e, "status_code", None)
    check("  %r is refused with a sentence, not stored" % bad, got, 400)
check("  and the good one survived the refusals",
      in_ws("blancocafe", W.public_base), "https://grit.blancocafe.com")

# ── the same address feeds the picture URLs, not a second setting ───────
in_ws("blancocafe", W.save_settings, Req(),
      W.Settings(imagegen_provider="meta", imagegen_api_key="sk-blanco-0002"))
check("media URLs use the one address too",
      in_ws("blancocafe", W.media_check, Req())["base_url"], "https://grit.blancocafe.com")

W.caller_email = _real_caller
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
