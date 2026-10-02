# -*- coding: utf-8 -*-
"""Onboarding a customer without editing code.

Adding a workspace used to mean editing workspaces.py, hand-making a database,
wiring a hostname and redeploying — a development task bolted onto every sale,
which is the difference between a tool and a product.

What must NOT change: isolation is still one database file per customer, and a
new workspace still refuses to draft outreach until it has offer copy of its
own, so it can never borrow another business's pitch.

Temp data dir, no network, never touches the live databases.
"""
import json, os, pathlib, sqlite3, sys, tempfile
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-reg-")
os.environ["JUST_GRIT_DATA"] = TMP
import webapp as W
import workspaces as ws
W.DATA = pathlib.Path(TMP)
W.init_db()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

from fastapi.testclient import TestClient
import webapp
client = TestClient(W.app)

# auth is on; drive the functions directly instead of faking a session
class Req:
    headers = {}
    cookies = {}
W.require_auth = lambda r: "test@example.com"

# ── slug safety: this becomes a filename ────────────────────────────────
check("a normal slug is allowed", ws.valid_slug("chirocare"), True)
check("path traversal is refused", ws.valid_slug("../../etc/passwd"), False)
check("a slash is refused", ws.valid_slug("a/b"), False)
check("uppercase is refused", ws.valid_slug("ChiroCare"), False)
check("a single character is refused", ws.valid_slug("a"), False)
check("an empty slug is refused", ws.valid_slug(""), False)

# ── onboarding a customer ───────────────────────────────────────────────
body = W.NewWorkspaceBody(slug="chirocare", name="ChiroCare Injury Rehab",
                          short="ChiroCare", product="ChiroCare OS",
                          host="app.chirocaresa.com", sells="injury rehab",
                          buyer="chiropractic groups")
out = W.create_workspace(Req(), body)
check("the workspace is created", out["ok"], True)
check("  it gets its OWN database file", out["db"], "justgrit-chirocare.db")
check("  that file really exists on disk",
      (pathlib.Path(TMP) / "justgrit-chirocare.db").exists(), True)
check("  and it is a different file from the primary",
      out["db"] != ws.WORKSPACES[ws.PRIMARY]["db"], True)
check("  it is live immediately, no restart", ws.exists("chirocare"), True)
check("  the hostname routes to it", ws.from_host("app.chirocaresa.com"), "chirocare")

# ── isolation is the thing that must not regress ────────────────────────
tok = ws.CURRENT.set("chirocare")
try:
    c = W.db(); c.row_factory = sqlite3.Row
    c.execute("INSERT INTO prospects (company,status,vertical) VALUES ('Chiro Lead','new','chiro')")
    c.commit(); c.close()
finally:
    ws.CURRENT.reset(tok)
c2 = W.db(ws.PRIMARY); c2.row_factory = sqlite3.Row
leaked = c2.execute("SELECT COUNT(*) n FROM prospects WHERE company='Chiro Lead'").fetchone()["n"]
c2.close()
check("a customer's lead never appears in the primary database", leaked, 0)

# ── a new customer cannot inherit somebody else's pitch ─────────────────
tok = ws.CURRENT.set("chirocare")
try:
    check("a fresh workspace has no catalog", W.has_catalog(), False)
    refused = False
    try:
        W.catalog_ready("draft outreach")
    except Exception as e:
        refused = "no offer catalog" in str(e)
    check("  so drafting outreach is refused outright", refused, True)
    # A real catalog, not just any string - an offer needs the three sentences
    # an email is built from or it would ship half-written.
    W.set_setting(W.CATALOG_SETTING, json.dumps({"offers": {"After-Hours Line": {
        "tail": "the calls coming in after you close",
        "cost": "They book with whoever picks up.",
        "fix": "I put something on your line that answers every time."}}}))
    check("  once they write their own offers, it opens up", W.has_catalog(), True)
    W.set_setting(W.CATALOG_SETTING, "just a note, not a catalog")
    check("  but a note in that field is not a catalog", W.has_catalog(), False)
finally:
    ws.CURRENT.reset(tok)

check("the built-ins still have their catalog in code",
      all(W.has_catalog(s) for s in ("watson", "homerepair")), True)

# ── duplicates and collisions ───────────────────────────────────────────
dupe = False
try:
    W.create_workspace(Req(), W.NewWorkspaceBody(slug="chirocare", name="Again"))
except Exception as e:
    dupe = "already" in str(e)
check("the same slug twice is refused", dupe, True)

bad = False
try:
    W.create_workspace(Req(), W.NewWorkspaceBody(slug="../evil", name="Evil"))
except Exception:
    bad = True
check("a traversal slug is refused by the route too", bad, True)

noname = False
try:
    W.create_workspace(Req(), W.NewWorkspaceBody(slug="nameless", name="  "))
except Exception:
    noname = True
check("a workspace with no name is refused", noname, True)

# ── the registry survives a reload, and a broken one never kills boot ───
ws.WORKSPACES.pop("chirocare", None)
ws.reload_registry()
check("a restart re-reads the customer from the database", ws.exists("chirocare"), True)

c3 = sqlite3.connect(pathlib.Path(TMP) / ws.WORKSPACES[ws.PRIMARY]["db"])
c3.execute("INSERT INTO workspace_registry (slug,name,db,hosts) VALUES ('../bad','Bad','x.db','[]')")
c3.execute("INSERT INTO workspace_registry (slug,name,db,hosts) VALUES ('okcust','OK','justgrit-okcust.db','not json')")
c3.commit(); c3.close()
ws.reload_registry()
check("a malformed slug in the table is ignored, not loaded", ws.exists("../bad"), False)
check("  a row with broken JSON still loads, just hostless", ws.exists("okcust"), True)
check("  and the built-ins are untouched", ws.exists("watson") and ws.exists("homerepair"), True)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
