# -*- coding: utf-8 -*-
"""Getting started: six steps at the top of Today until they're done.

Phase 1 of the commercial plan (2026-09-26): a new business should see
what to do next, not eleven tabs. No network.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-gs-")
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
tok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.require_auth = lambda r: "local"
W.is_owner = lambda r: True
W.google_key = lambda: ""          # the Mac has a real key on file; the test shouldn't see it
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

g = W.getting_started(Req())
check("a fresh workspace has six steps; only 'what you sell' is done (Watson's catalog ships with the app)",
      ([x["key"] for x in g["steps"]], [x["done"] for x in g["steps"]], g["complete"]),
      (["business", "offers", "mailbox", "maps", "leads", "autopilot"], [False, True, False, False, False, False], False))
check("  every undone step says what to do and where to go", all(x["note"] and x["go"]["tab"] for x in g["steps"]))
check("  setup steps jump to their section", [x["go"].get("sec") for x in g["steps"][:4]], ["business", "offers", "mailbox", "maps"])
for k, v in dict(sender_name="D", sender_phone="1", sender_email="d@x.com", sender_address="1 Main").items():
    W.set_setting(k, v)
g = W.getting_started(Req())
check("filling in the business ticks it", [x["done"] for x in g["steps"]][0])
with W.closing(W.db()) as c:
    c.execute("INSERT INTO prospects (company, status, stage, added_at) VALUES ('Blanco Cafe','new','lead',?)", (W.now(),)); c.commit()
g = W.getting_started(Req())
check("the first lead ticks 'leads found'", ([x for x in g["steps"] if x["key"] == "leads"][0]["done"], [x for x in g["steps"] if x["key"] == "leads"][0]["note"]), (True, "1 lead on file."))
W.set_setting("autopilot_mode", "all")
check("autopilot on ticks the last step", [x for x in W.getting_started(Req())["steps"] if x["key"] == "autopilot"][0]["done"])

# the page: four tabs up front, the rest under More, every deep link intact
html = open("static/dashboard.html").read()
import re
nav = html[html.index('<nav id="nav">'):html.index('</nav>')]
tabs = re.findall(r'<button data-tab="(\w+)"', nav)
groups = re.findall(r'<div class="mh">([^<]+)</div>', nav)
check("the sidebar is grouped", groups, ["Overview", "Outreach", "Marketing", "Insights", "Account"])
check("  Today and Calendar first, Setup last", (tabs[:2], tabs[-1]), (["today", "calendar"], "setup"))
check("  every section has a button", sorted(tabs), sorted(re.findall(r'<section id="tab-(\w+)"', html)))
check("no 'Scout Engine' left on the header", "Scout Engine · v0.1" in html, False)
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
