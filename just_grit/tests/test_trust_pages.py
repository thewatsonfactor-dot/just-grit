# -*- coding: utf-8 -*-
"""Terms, privacy, help, what's new, status; export as a zip; delete a
customer workspace with its name typed back."""
import os, pathlib, sys, tempfile, io, zipfile, json
TMP = tempfile.mkdtemp(prefix="jg-trust-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws
from fastapi.testclient import TestClient

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.set_setting("legal_entity", "The Watson Factor LLC"); W.set_setting("support_email", "help@example.com")
ws.CURRENT.reset(tok)
app = TestClient(W.app)
for path, must in [("/terms", "Terms of service"), ("/privacy", "Privacy policy"), ("/help", "five-minute tour"),
                   ("/changelog", "Sign in with a code"), ("/status", "Status")]:
    r = app.get(path)
    check("%s renders without sign-in" % path, (r.status_code, must in r.text, "The Watson Factor LLC" in r.text, "help@example.com" in r.text), (200, True, True, True))
r = app.get("/terms")
check("terms name Texas and the grace period", ("Texas" in r.text, "grace period" in r.text), (True, True))
check("no raw markdown leaks", "**" in r.text or "\n- " in r.text.split("<main>")[1].split("</main>")[0], False)
r = app.get("/health")
h = r.json()
check("/health is machine-readable with a version", (r.status_code, h["ok"], h["db"], isinstance(h["version"], str)), (200, True, True, True))

# export
W.require_auth = lambda r: "local"; W.require_owner = lambda r: "local"
class Req:
    headers = {}; cookies = {}; query_params = {}
    class client: host = "127.0.0.1"
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.set_setting("imap_password", "s3cret"); W.set_setting("sender_name", "Daniel")
with W.closing(W.db()) as c:
    c.execute("INSERT INTO prospects (company, domain, added_at) VALUES ('Edwards PM','edwardspm.com',?)", (W.now(),)); c.commit()
z = zipfile.ZipFile(io.BytesIO(W.export_zip(Req()).body))
names = sorted(z.namelist())
check("export zip has the CSVs and settings", names, ["README.txt", "activity.csv", "campaigns.csv", "contacts.csv", "deals.csv", "emails.csv", "leads.csv", "posts.csv", "settings.json"])
check("  leads.csv carries the row", "Edwards PM" in z.read("leads.csv").decode())
st = json.loads(z.read("settings.json"))
check("  passwords and keys are left out", ("imap_password" in st, st.get("sender_name")), (False, "Daniel"))
ws.CURRENT.reset(tok)

# delete: built-ins refused, wrong name refused, right name moves the file
try:
    W.workspace_delete(Req(), W.DeleteWorkspaceBody(slug="homerepair", confirm_name="HomeRepair Tech")); check("built-in refused", False)
except W.HTTPException as e:
    check("a built-in workspace can't be deleted", e.status_code, 400)
tok = ws.CURRENT.set(ws.PRIMARY)
r = W.start_business(Req(), W.StartBody(name="Blanco Cafe", city="New Braunfels, TX", sells="coffee", buyer="people", trade="Cafe",
                                        sender_name="Ann", sender_email="ann@blancocafe.com", sender_phone="1", sender_address="1 Main St"))
ws.CURRENT.reset(tok)
slug = r["slug"]
check("a customer workspace exists to delete", slug in ws.WORKSPACES)
try:
    W.workspace_delete(Req(), W.DeleteWorkspaceBody(slug=slug, confirm_name="blanco")); check("wrong name refused", False)
except W.HTTPException as e:
    check("the name must be typed exactly", "exactly" in e.detail)
dbfile = W.DATA / ws.info(slug)["db"]
check("  its file is there before", dbfile.exists())
out = W.workspace_delete(Req(), W.DeleteWorkspaceBody(slug=slug, confirm_name="Blanco Cafe"))
check("typed right: gone from the registry, file parked", (slug in ws.WORKSPACES, dbfile.exists(), out["kept_as"].startswith(slug)), (False, False, True))
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
