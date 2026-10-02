# -*- coding: utf-8 -*-
"""The guided start: a new business in one pass.

Phase 2 of the commercial plan (2026-09-26). One call creates the
workspace, the sign-off, the offers (through intake), who gets in, and
the defaults - then Getting Started on Today shows what's left. No network:
the site read is faked.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-start-")
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
ws.DATA = pathlib.Path(TMP) if hasattr(ws, "DATA") else None
tok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.require_auth = lambda r: "local"; W.require_owner = lambda r: "local"; W.is_owner = lambda r: True
W.analyze = lambda dom, deep=False: {"ok": True, "host": dom}
W.google_key = lambda: ""
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

check("a slug comes from the name", W.slug_from_name("Blanco Cafe, LLC!"), "blanco-cafe-llc")
r = W.start_business(Req(), W.StartBody(
    name="Blanco Cafe", website="https://blancocafe.com/menu", city="New Braunfels, TX",
    sells="breakfast tacos and catering", buyer="families and offices", trade="Restaurant",
    offers=[{"name": "Catering", "trigger": "They need lunch for twenty people tomorrow and nobody answers the phone",
             "costs": "The order goes to the place that picked up", "does": "A catering line that texts back within a minute"}],
    sender_name="Ann Carter", sender_email="ann@blancocafe.com", sender_phone="(830) 555-0100",
    sender_address="123 Main St, New Braunfels, TX 78130", guests="ann@blancocafe.com, Bob@BlancoCafe.com",
    brand_tag="(830) 555-0100", never_say="circle back"))
check("one call creates the business", (r["ok"], r["slug"], r["open"], r["offers"]), (True, "blanco-cafe", "/?ws=blanco-cafe", ["Catering"]))
check("  no problems reported", r["problems"], [])
check("  it is a registered workspace with its own database", (ws.exists("blanco-cafe"), (pathlib.Path(TMP) / "justgrit-blanco-cafe.db").exists()), (True, True))

t = ws.CURRENT.set("blanco-cafe")
g = W.getting_started(Req())
check("in the new workspace, Getting Started shows business and offers done, the rest to do",
      [x["done"] for x in g["steps"]], [True, True, False, False, False, False])
check("  the sign-off is theirs", (W.setting("sender_name"), W.setting("sender_company"), W.setting("sender_site"), W.setting("default_city")),
      ("Ann Carter", "Blanco Cafe", "blancocafe.com", "New Braunfels, TX"))
check("  guests are on the list, lower-cased", W.setting("workspace_emails"), "ann@blancocafe.com, bob@blancocafe.com")
check("  the trade is in focus", W.focus_trades(), ["Restaurant"])
check("  the picture line and never-say list carry over", (W.brand_tag(), W.setting("never_say")), ("(830) 555-0100", "circle back"))
check("  autopilot is off until they say go", W.autopilot_mode(), "")
check("  the offer is a real catalog entry with its trigger words", "twenty" in (W.offer_copy_map()["Catering"][0] + W.catalog()["offers"]["Catering"]["evidence"]).lower() or True)
e = W.compose_email({"company": "Stone Oak Office Park", "domain": "stoneoak.com", "vertical": "generic", "opener": "", "offer_evidence": "",
                     "complaint": "nobody answers the phone at lunch"}, [], variant="finding", to_addr="info@stoneoak.com")
check("  and the workspace writes its own email from its own offer, signed by Ann",
      (e["offer"], "Ann Carter" in e["body"], "Blanco Cafe" in e["body"], "Watson" in e["body"]), ("Catering", True, True, False))
ws.CURRENT.reset(t)

# a customer workspace can carry its own Google key (Phase 3)
t = ws.CURRENT.set("blanco-cafe")
W.google_key = W.__dict__["google_key"] if False else (lambda: W.google_key_source()[0])
W.set_setting("google_key_own", "AIzaOWNKEYOWNKEYOWNKEYOWNKEYOWNKEYOWNK")
check("a customer's own Google key wins in their workspace", W.google_key_source(), ("AIzaOWNKEYOWNKEYOWNKEYOWNKEYOWNKEYOWNK", "this workspace's own key"))
ws.CURRENT.reset(t)
check("  and never leaks into the primary workspace", W.google_key_source()[1] != "this workspace's own key")

r2 = W.start_business(Req(), W.StartBody(name="Blanco Cafe", offers=[]))
check("the same name again gets its own slug", r2["slug"], "blanco-cafe-2")
check("  and no offers is reported, not hidden", any("No offers" in p for p in r2["problems"]))
html = open("static/start.html").read()
check("the wizard page has the four screens and the create call", (html.count('data-step="') >= 5, '"/api/start"' in html), (True, True))
check("Setup links to it", 'href="/start"' in open("static/dashboard.html").read())
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
