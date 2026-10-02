# -*- coding: utf-8 -*-
"""The Setup page, as a customer meets it - and as the owner does.

Daniel's own words after a day in GoHighLevel: "the way we are set up makes
my product seem like you have to be a developer to understand it", and
"the products you add can't be deleted". The second one was literal: the
two built-in workspaces had their offers marked *lives in code*, so the
owner of the product could not remove an offer from his own product. These
pin the fixes: offers are editable and deletable everywhere, the built-in
copy is a default rather than a lock, and the page has a checklist it can
draw a "4 of 7" from instead of eleven forms.
"""
import json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-setup-")
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
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"

# ── offers on the owner's own workspace ─────────────────────────────────
cat = W.get_catalog(Req())
check("the built-in workspace's offers are shown", len(cat["offers"]) >= 3, True)
check("  and marked EDITABLE, not 'lives in code'", cat["editable"], True)
check("  the built-in copy is the default until something is saved", cat["customised"], False)
check("  each built-in offer carries its question and impact lines too",
      all("question" in o and "impact" in o for o in cat["offers"].values()), True)

# delete one, save the rest
names = sorted(cat["offers"])
keep = {k: v for k, v in cat["offers"].items() if k != names[0]}
r = W.save_catalog(Req(), W.CatalogBody(catalog={"offers": keep}))
check("removing a built-in offer and saving is allowed", r["ok"], True)
check("  the removed one is gone from the catalog the app drafts from",
      names[0] in W.offer_copy_map(), False)
check("  and the kept ones are what the app drafts from now",
      sorted(W.offer_copy_map()) == sorted(keep), True)
check("  /api/catalog now reports 'customised'", W.get_catalog(Req())["customised"], True)

# add a new one alongside
keep["Missed-Call Text-Back"] = {"tail": "the calls you're missing after five",
                                 "cost": "The caller books with whoever picks up.",
                                 "fix": "I put a text on the line that answers every missed call in a minute."}
W.save_catalog(Req(), W.CatalogBody(catalog={"offers": keep}))
check("a brand-new offer on the built-in workspace is used for drafting",
      "Missed-Call Text-Back" in W.offer_copy_map(), True)

# reset
r = W.reset_catalog(Req())
check("reset goes back to the built-in copy", sorted(W.offer_copy_map()), names)
check("  and the page says so", W.get_catalog(Req())["customised"], False)

# a half-written offer is refused, not half-shipped
try:
    W.save_catalog(Req(), W.CatalogBody(catalog={"offers": {"Half": {"tail": "x", "cost": "", "fix": ""}}}))
    got = "allowed"
except Exception as e:
    got = getattr(e, "status_code", None)
check("an offer missing its sentences is refused with a 400", got, 400)

# ── the checklist ───────────────────────────────────────────────────────
W.set_setting("sender_name", ""); W.set_setting("sender_phone", "")
W.set_setting("sender_email", ""); W.set_setting("sender_address", "")
st = W.setup_status(Req())
by = {s["key"]: s for s in st["sections"]}
check("business with nothing filled in is 'todo'", by["business"]["state"], "todo")
check("  and says what is missing", "name" in by["business"]["missing"], True)
check("offers on the built-in workspace count as done", by["offers"]["state"], "done")
check("phone with no Telnyx is 'todo'", by["phone"]["state"], "todo")
check("the two required sections are business and offers",
      sorted(s["key"] for s in st["sections"] if s["required"]), ["business", "offers"])
check("not ready to work until both required are done", st["ready_to_work"], False)
W.set_setting("sender_name", "Daniel"); W.set_setting("sender_phone", "830")
st = W.setup_status(Req()); by = {s["key"]: s for s in st["sections"]}
check("half the business fields -> 'partial'", by["business"]["state"], "partial")
W.set_setting("sender_email", "d@x.com"); W.set_setting("sender_address", "1 Main St")
st = W.setup_status(Req()); by = {s["key"]: s for s in st["sections"]}
check("all business fields -> 'done', and the workspace is ready to work",
      (by["business"]["state"], st["ready_to_work"]), ("done", True))
check("the owner sees a Customers section", "customers" in by, True)
check("'x of y' counts only sections that can be finished",
      st["total"], len([s for s in st["sections"] if s["state"] != "info"]))

W.set_setting("telnyx_api_key", "K"); W.set_setting("telnyx_connection_id", "1")
W.set_setting("telnyx_from_number", "+18305550100"); W.set_setting("telnyx_rep_number", "+12105550199")
by = {s["key"]: s for s in W.setup_status(Req())["sections"]}
check("Telnyx connected but text-back off -> 'partial', and it says text-back is off",
      (by["phone"]["state"], "off" in by["phone"]["missing"]), ("partial", True))
W.set_setting("textback_enabled", "1")
by = {s["key"]: s for s in W.setup_status(Req())["sections"]}
check("  text-back on -> 'done'", by["phone"]["state"], "done")

W.set_setting("fb_page_id", "1"); W.set_setting("fb_page_token", "t")
by = {s["key"]: s for s in W.setup_status(Req())["sections"]}
check("Facebook without Instagram -> 'partial', names the gap",
      (by["social"]["state"], "Instagram" in by["social"]["missing"]), ("partial", True))

# ── the Facebook page picker never returns page tokens ──────────────────
W.fb.pages = lambda tok: [{"id": "111", "name": "Blanco Cafe", "access_token": "SECRET_PAGE_TOKEN"},
                          {"id": "222", "name": "Blanco Catering", "access_token": "SECRET_TOO"}]
r = W.facebook_pages(Req(), W.FbPagesBody(user_token="EAAB..."))
check("the page picker lists names and ids", [p["name"] for p in r["pages"]], ["Blanco Cafe", "Blanco Catering"])
check("  and never the page tokens", "SECRET" in json.dumps(r), False)

# ── the guest list is settable by the owner and shown to nobody else ────
W.caller_email = lambda r: "daniel@thewatsonfactor.dev"
W.set_setting("allowed_emails", "daniel@thewatsonfactor.dev")
W.save_settings(Req(), W.Settings(workspace_emails="front@blancocafe.com"))
check("the owner can set this workspace's own logins from Setup",
      W.setting("workspace_emails", ""), "front@blancocafe.com")
me = W.me(Req())
check("  and /api/me shows them to the owner", me["workspace_emails"], "front@blancocafe.com")
W.caller_email = lambda r: "front@blancocafe.com"
W.set_setting("workspace_emails", "front@blancocafe.com")
me = W.me(Req())
check("  a guest gets a blank, not the list", me["workspace_emails"], "")

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
