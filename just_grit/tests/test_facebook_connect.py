# -*- coding: utf-8 -*-
"""Connecting a Facebook Page that belongs to a business portfolio.

2026-09-24, Homerepair.tech: the token had every permission, but "Find my
Pages" said "That token manages no Pages". me/accounts leaves out Pages a
person reaches through a business portfolio; GET /<page id> with the same
token returned the Page, its Instagram @homerepair.tech, and a Page token.
Also: a Page key made from the one-hour Explorer token dies in an hour.
"""
import os, pathlib, sys, tempfile, time
TMP = tempfile.mkdtemp(prefix="jg-fb-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import facebook as fb
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP); tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

CALLS = []
SHORT = int(time.time()) + 3600
def fake_get(path, params):
    CALLS.append(path)
    t = params.get("access_token") or params.get("fb_exchange_token")
    if path == "me/accounts":
        return {"data": []}                                    # business-owned: not listed
    if path == "me/businesses":
        return {"data": []}                                    # no business_management
    if path == "1311864045336201":
        if "instagram_business_account" in params.get("fields", ""):
            return {"instagram_business_account": {"id": "17841434300816427", "username": "homerepair.tech"}}
        return {"id": "1311864045336201", "name": "Homerepair.tech",
                "access_token": "PAGE-FROM-" + t}
    if path == "debug_token":
        it = params["input_token"]
        return {"data": {"is_valid": True, "expires_at": 0 if ("LONG" in it) else SHORT}}
    if path == "oauth/access_token":
        return {"access_token": "LONG-" + params["fb_exchange_token"]}
    raise fb.FacebookError("unexpected " + path)
fb._get = fake_get

r = W.facebook_pages(Req(), W.FbPagesBody(user_token="USER"))
check("with no Page id to go on, nothing is found (and the page asks for one)", r["pages"], [])
check("  and it knows the token is the one-hour kind", r["short_lived"], True)
r = W.facebook_pages(Req(), W.FbPagesBody(user_token="USER", page_id="1311864045336201"))
check("with the Page id, the business-owned Page is found", r["pages"], [{"id": "1311864045336201", "name": "Homerepair.tech"}])

x = W.facebook_exchange(Req(), W.FbExchangeBody(user_token="USER", page_id="1311864045336201"))
check("connecting it stores the Page and its Instagram", (W.setting("fb_page_id", ""), W.setting("ig_user_id", "")),
      ("1311864045336201", "17841434300816427"))
check("  without an app secret it warns the key runs out", ("WARNING" in x["note"], x["expires_at"] == SHORT), (True, True))
check("  and the status says when", W.facebook_status(Req())["expires_at"], SHORT)

W.set_setting("fb_app_id", "1613428640192398"); W.set_setting("fb_app_secret", "s3cret")
x = W.facebook_exchange(Req(), W.FbExchangeBody(user_token="USER", page_id="1311864045336201"))
check("with the app id + secret, the token is stretched first", W.setting("fb_page_token", ""), "PAGE-FROM-LONG-USER")
check("  and the Page key doesn't expire", ("doesn't expire" in x["note"], x["expires_at"]), (True, 0))
r = W.facebook_pages(Req(), W.FbPagesBody(user_token="USER"))
check("next time, the Page it was connected to is looked up by itself", [p["id"] for p in r["pages"]], ["1311864045336201"])

W.save_settings(Req(), W.Settings(fb_app_secret=""))
check("saving Setup with the secret box blank keeps the saved secret", W.setting("fb_app_secret", ""), "s3cret")

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
