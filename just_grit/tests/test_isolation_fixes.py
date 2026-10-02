# -*- coding: utf-8 -*-
"""Three defects a review found that the fixers never reached.

QT-3  A customer's "dead" or "corporate" mark appended to ONE process-wide
      suppression file that gates scanning, importing and drafting in every
      workspace - so a guest's tap silently blocked that domain for the owner.
QT-4  create_workspace accepted a hostname already owned by another
      workspace, reported it live, and routed that customer's traffic to the
      older one.
DJS-2 The queue card shows - and drafts write to - the primary contact's
      phone/email, but Edit saved only the prospect row, so correcting a
      bounced address on the card looked saved and changed nothing.

Temp data dir, no network, never touches the live databases or the real
suppression file.
"""
import os, pathlib, sqlite3, sys, tempfile
sys.path.insert(0, '.')

TMP = tempfile.mkdtemp(prefix="jg-iso-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["GRIT_SUPPRESSION_FILE"] = os.path.join(TMP, "account-suppressed.txt")
import importlib
from grit_analyzer import guard
importlib.reload(guard)                      # pick up the env override
import webapp as W
import workspaces as ws
W.DATA = pathlib.Path(TMP)
for _slug in list(ws.WORKSPACES):
    _t = ws.CURRENT.set(_slug)
    try: W.init_db()
    finally: ws.CURRENT.reset(_t)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

class Req:
    headers = {}; cookies = {}
W.require_auth = lambda r: "test@example.com"

# ── QT-3: suppression is per workspace, reads fall through to the account ──
W.create_workspace(Req(), W.NewWorkspaceBody(slug="custa", name="Customer A"))
W.create_workspace(Req(), W.NewWorkspaceBody(slug="custb", name="Customer B"))

tok = ws.CURRENT.set("custa")
try:
    wpath, rpaths = W.suppression_paths()
    check("a customer workspace writes its OWN suppression file",
          wpath.endswith("suppressed_domains-custa.txt"), True)
    check("  and reads its own plus the account's", len(rpaths), 2)
    W.suppress("stampschiro.example")
    check("  a domain it marks is suppressed for it", W.is_suppressed("stampschiro.example"), True)
finally:
    ws.CURRENT.reset(tok)

tok = ws.CURRENT.set("custb")
try:
    check("another customer does NOT see that mark", W.is_suppressed("stampschiro.example"), False)
finally:
    ws.CURRENT.reset(tok)

tok = ws.CURRENT.set(ws.PRIMARY)
try:
    check("the owner does not see it either", W.is_suppressed("stampschiro.example"), False)
    wpath, rpaths = W.suppression_paths()
    check("the owner writes the account list", wpath, guard.SUPPRESSION_FILE)
    W.suppress("never-scan.example")
finally:
    ws.CURRENT.reset(tok)

for slug in ("custa", "custb"):
    tok = ws.CURRENT.set(slug)
    try:
        check("the owner's account-level mark still holds in %s" % slug,
              W.is_suppressed("never-scan.example"), True)
        check("  including subdomains", W.is_suppressed("shop.never-scan.example"), True)
    finally:
        ws.CURRENT.reset(tok)

acct = open(guard.SUPPRESSION_FILE).read()
check("the customer's domain never landed in the account file",
      "stampschiro" in acct, False)

# the real path: a corporate touch in a customer workspace
tok = ws.CURRENT.set("custa")
try:
    c = W.db(); c.row_factory = sqlite3.Row
    c.execute("INSERT INTO prospects (company,domain,status,next_due,next_action) "
              "VALUES ('Wash Tub','washtub.example','working',date('now'),'call')")
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit(); c.close()
    W.log_touch(Req(), pid, W.TouchBody(outcome="corporate"))
    check("a corporate mark suppresses the domain in that workspace",
          W.is_suppressed("washtub.example"), True)
finally:
    ws.CURRENT.reset(tok)
tok = ws.CURRENT.set(ws.PRIMARY)
try:
    check("  and NOT for the owner", W.is_suppressed("washtub.example"), False)
finally:
    ws.CURRENT.reset(tok)

# ── QT-4: a hostname routes to exactly one workspace ──
W.create_workspace(Req(), W.NewWorkspaceBody(slug="hosted", name="Hosted", host="app.hosted.example"))
dup = ""
try:
    W.create_workspace(Req(), W.NewWorkspaceBody(slug="hosted2", name="Hosted Two",
                                                 host="APP.Hosted.example"))
except Exception as e:
    dup = str(e)
check("a duplicate hostname is refused", "already routes" in dup, True)
check("  case-insensitively", "hosted" in dup, True)
check("  and the second workspace was not created", ws.exists("hosted2"), False)
check("  so the name still routes to the first", ws.from_host("app.hosted.example"), "hosted")
built_in_host = ws.WORKSPACES["homerepair"]["hosts"][0]
clash = ""
try:
    W.create_workspace(Req(), W.NewWorkspaceBody(slug="squat", name="Squat", host=built_in_host))
except Exception as e:
    clash = str(e)
check("a built-in workspace's hostname cannot be taken either", "already routes" in clash, True)

# ── DJS-2: editing the card edits what the card shows ──
tok = ws.CURRENT.set(ws.PRIMARY)
try:
    c = W.db(); c.row_factory = sqlite3.Row
    c.execute("INSERT INTO prospects (company,domain,email,phone,status) "
              "VALUES ('Kirby Vet','kirby.example','','','working')")
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO contacts (prospect_id,name,email,phone,is_primary,added_at) "
              "VALUES (?,?,?,?,1,?)", (pid, "Maria", "maria@oldco.example", "(210) 555-0100", W.now()))
    c.commit()
    out = W.set_fields(Req(), pid, W.FieldsBody(fields={"email": "owner@newco.example"}))
    check("the prospect row is updated",
          c.execute("SELECT email FROM prospects WHERE id=?", (pid,)).fetchone()["email"],
          "owner@newco.example")
    check("  and so is the contact the card was actually showing",
          c.execute("SELECT email FROM contacts WHERE prospect_id=?", (pid,)).fetchone()["email"],
          "owner@newco.example")
    check("  the response says so", out["contact_updated"], ["email"])
    check("  so the next draft goes where the owner typed",
          W.best_recipient(W.contact_rows(c, pid), {"email": "owner@newco.example"}, set())[0],
          "owner@newco.example")
    check("  the contact's phone was left alone",
          c.execute("SELECT phone FROM contacts WHERE prospect_id=?", (pid,)).fetchone()["phone"],
          "(210) 555-0100")
    c.execute("INSERT INTO prospects (company,domain,email,status) VALUES ('Solo','solo.example','a@solo.example','working')")
    pid2 = c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit()
    out2 = W.set_fields(Req(), pid2, W.FieldsBody(fields={"email": "b@solo.example"}))
    check("with no contact on file only the row changes, and it says so",
          out2["contact_updated"], [])
    c.close()
finally:
    ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
