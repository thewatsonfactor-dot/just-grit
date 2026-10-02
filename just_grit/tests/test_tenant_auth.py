# -*- coding: utf-8 -*-
"""A customer can open their own workspace and nobody else's.

`allowed_emails` is account-level — the people who RUN this. Fine while both
workspaces belonged to Daniel. Give a client a login and one global list means
the address added so ChiroCare can use ChiroCare is equally valid on Watson
Factor's own leads, with nothing between them but a hand-configured Cloudflare
Access policy per hostname. An external config being load-bearing for tenant
isolation is the same class of mistake as a forgotten WHERE clause.

These call require_auth() itself — not a copy of its rules.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-auth-")
os.environ["JUST_GRIT_DATA"] = TMP
sys.path.insert(0, '.')
import webapp as W, workspaces as ws
W.DATA = pathlib.Path(TMP)

# every workspace needs its schema, the way startup builds them
for _s in list(ws.WORKSPACES):
    _t = ws.CURRENT.set(_s)
    try:
        W.init_db()
    finally:
        ws.CURRENT.reset(_t)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

OWNER = "daniel@thewatsonfactor.dev"
GUEST = "aliciana@chirocaresa.com"
OTHER = "front@stampschiropractic.com"

class Req:
    headers = {}; cookies = {}
    class client: host = "10.0.0.9"        # not localhost, so auth is enforced

_real_caller_email = W.caller_email

def opens(workspace, email) -> bool:
    """Does require_auth actually let this person into this workspace?"""
    tok = ws.CURRENT.set(workspace)
    W.caller_email = lambda r: email
    try:
        W.require_auth(Req())
        return True
    except Exception:
        return False
    finally:
        W.caller_email = _real_caller_email
        ws.CURRENT.reset(tok)

def seed(workspace, key, value):
    tok = ws.CURRENT.set(workspace)
    try:
        W.set_setting(key, value)
    finally:
        ws.CURRENT.reset(tok)

seed(ws.PRIMARY, "allowed_emails", OWNER)       # account owners

# two customers, each with their own guest list
W.caller_email = lambda r: OWNER
W.create_workspace(Req(), W.NewWorkspaceBody(slug="chirocare", name="ChiroCare",
                                             host="app.chirocaresa.com"))
W.create_workspace(Req(), W.NewWorkspaceBody(slug="stampschiro", name="Stamps Chiropractic"))
W.caller_email = _real_caller_email
seed("chirocare", "workspace_emails", GUEST)
seed("stampschiro", "workspace_emails", OTHER)

# ── the isolation that has to hold ──────────────────────────────────────
check("the guest opens the workspace she was given", opens("chirocare", GUEST), True)
check("the SAME guest cannot open Watson Factor", opens("watson", GUEST), False)
check("  nor HomeRepair", opens("homerepair", GUEST), False)
check("  nor the other customer", opens("stampschiro", GUEST), False)
check("customer B opens only their own", opens("stampschiro", OTHER), True)
check("  and cannot reach customer A", opens("chirocare", OTHER), False)

# ── owners keep working everywhere ──────────────────────────────────────
check("the owner opens a customer's workspace", opens("chirocare", OWNER), True)
check("the owner opens his own", opens("watson", OWNER), True)
check("  and HomeRepair", opens("homerepair", OWNER), True)

# ── strangers get nothing ───────────────────────────────────────────────
check("a stranger opens no customer workspace", opens("chirocare", "nobody@example.com"), False)
check("  and none of Daniel's either", opens("watson", "nobody@example.com"), False)

# ── the built-ins behave exactly as before ──────────────────────────────
def guests_in(workspace):
    tok = ws.CURRENT.set(workspace)
    try:
        return W.workspace_emails()
    finally:
        ws.CURRENT.reset(tok)

check("workspace_emails is NOT account-level",
      "workspace_emails" in ws.GLOBAL_SETTINGS, False)
check("Watson has no guest list, so it behaves as it always did",
      guests_in("watson"), set())
check("  and the customer's list never leaked to HomeRepair",
      guests_in("homerepair"), set())
check("  each customer's list stays its own",
      (guests_in("chirocare"), guests_in("stampschiro")), ({GUEST}, {OTHER}))

# ── a guest cannot write herself onto the owner list ────────────────────
# require_auth admits owners and guests alike and nothing downstream used to
# tell them apart. `allowed_emails` and `free_request_cap` write the PRIMARY
# file whichever workspace is on screen, so a guest posting the owner list
# from her own workspace was, one request later, an owner of every workspace
# and in charge of the Google bill.
def as_guest(workspace, email, fn, *a):
    """Run a route as this person on this workspace; return the HTTP status
    it refused with, or None if it went through."""
    tok = ws.CURRENT.set(workspace)
    W.caller_email = lambda r: email
    try:
        fn(Req(), *a)
        return None
    except W.HTTPException as e:
        return e.status_code
    finally:
        W.caller_email = _real_caller_email
        ws.CURRENT.reset(tok)

def stored(workspace, key):
    tok = ws.CURRENT.set(workspace)
    try:
        return W.setting(key, "")
    finally:
        ws.CURRENT.reset(tok)

def primary(key):
    return stored(ws.PRIMARY, key)

seed(ws.PRIMARY, "free_request_cap", "500")
check("guest posting the owner list is refused",
      as_guest("chirocare", GUEST, W.save_settings,
               W.Settings(allowed_emails=OWNER + "," + GUEST, free_request_cap="0")), 403)
check("  the owner list is untouched", primary("allowed_emails"), OWNER)
check("  and so is the Google cap", primary("free_request_cap"), "500")
check("  she still cannot open Watson Factor", opens("watson", GUEST), False)
check("  nor HomeRepair", opens("homerepair", GUEST), False)
check("guest changing the cap alone is refused too",
      as_guest("chirocare", GUEST, W.save_settings, W.Settings(free_request_cap="0")), 403)
check("guest cannot create a workspace",
      as_guest("chirocare", GUEST, W.create_workspace,
               W.NewWorkspaceBody(slug="evil", name="Evil")), 403)
check("  so none was made", ws.exists("evil"), False)
check("guest cannot clear the account's Google key",
      as_guest("chirocare", GUEST, W.clear_google_key), 403)

# Both UIs round-trip every Setup field on save. A guest was shown a blank
# owner list, so a blank comes back - that must be "left alone", not
# "cleared" (an empty list makes everyone an owner) and not a refusal (she
# could never save her own sender name).
check("guest saving her own settings with the blank round-trip works",
      as_guest("chirocare", GUEST, W.save_settings,
               W.Settings(sender_name="Aliciana", allowed_emails="")), None)
check("  her sender name landed", stored("chirocare", "sender_name"), "Aliciana")
check("  and the owner list survived the blank", primary("allowed_emails"), OWNER)

W.caller_email = lambda r: GUEST
_tok = ws.CURRENT.set("chirocare")
try:
    _me = W.me(Req())
finally:
    ws.CURRENT.reset(_tok); W.caller_email = _real_caller_email
check("/api/me tells a guest she is not an owner", _me["owner"], False)
check("  and does not show her the owner list", _me["allowed_emails"], "")

# ── owners keep the keys ────────────────────────────────────────────────
check("the owner still edits the owner list",
      as_guest("chirocare", OWNER, W.save_settings,
               W.Settings(allowed_emails=OWNER + ", ops@thewatsonfactor.dev")), None)
check("  and it took", "ops@thewatsonfactor.dev" in W.allowed_emails(), True)
check("the owner still creates workspaces",
      as_guest("watson", OWNER, W.create_workspace,
               W.NewWorkspaceBody(slug="third", name="Third")), None)
check("local use (no identity, loopback) is the owner", W.is_owner(Req()), True)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
