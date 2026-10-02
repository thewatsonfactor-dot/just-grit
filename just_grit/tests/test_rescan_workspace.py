# -*- coding: utf-8 -*-
"""Rescan works the caller's workspace, not the primary one.

POST /api/prospect/{pid}/rescan marks the row pending in the caller's file and
then hands scan_one to the thread pool. A pool worker does not inherit the
request's ContextVar (see ws.carry), so a bare submit wakes up in the default
workspace and scans/overwrites the SAME id in The Watson Factor's database —
a different business — while the customer's row sits at 'pending' forever.

queue_scans and the sweep thread already wrap with ws.carry; rescan must too.
These go through the real handler and the real POOL, not a copy of them.
"""
import os, pathlib, sys, tempfile
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
TMP = tempfile.mkdtemp(prefix="jg-rescan-")
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

class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"       # off this Mac: no auth needed

# Never touch the network from a test: a scan that "loads" is a fixed report.
W.analyze = lambda dom, deep=False: {"ok": True, "overall": 61, "grade": "C",
                                     "findings": [], "stack": {}}
W.discover_name = lambda dom: None

def seed(workspace, company, domain):
    tok = ws.CURRENT.set(workspace)
    try:
        with closing(W.db()) as c:
            c.execute("INSERT INTO prospects (company, domain, scan_state, added_at) "
                      "VALUES (?,?,?,?)", (company, domain, "done", W.now()))
            c.commit()
            return c.execute("SELECT id FROM prospects WHERE company=?",
                             (company,)).fetchone()["id"]
    finally:
        ws.CURRENT.reset(tok)

def row(workspace, pid):
    with closing(W.db(workspace)) as c:
        return c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()

def rescan_as(workspace, pid):
    """Press rescan while working `workspace`, then wait for the pool."""
    tok = ws.CURRENT.set(workspace)
    try:
        W.rescan(Req(), pid)
    finally:
        ws.CURRENT.reset(tok)
    W.POOL.shutdown(wait=True)
    W.POOL = ThreadPoolExecutor(max_workers=4)

# The same id in both files, deliberately: that is what the bug overwrites.
pid_h = seed("homerepair", "Alamo Roofing", "")            # domainless -> no_site
pid_w = seed(ws.PRIMARY, "Stamps Chiropractic", "stampschiro.com")
check("both workspaces hold the same prospect id", pid_h == pid_w, True)
pid = pid_h

# -- a domainless prospect: the scan should end in the caller's file --------
rescan_as("homerepair", pid)
h, w = row("homerepair", pid), row(ws.PRIMARY, pid)
check("HomeRepair row finished (no_site), not stuck pending", h["scan_state"], "no_site")
check("HomeRepair row got a scanned_at", bool(h["scanned_at"]), True)
check("Watson row untouched: state", w["scan_state"], "done")
check("Watson row untouched: company", w["company"], "Stamps Chiropractic")
check("Watson row untouched: no scanned_at", w["scanned_at"], None)

# -- a prospect with a domain: the report lands in the caller's file --------
tok = ws.CURRENT.set("homerepair")
try:
    with closing(W.db()) as c:
        c.execute("UPDATE prospects SET domain='alamoroofing.com' WHERE id=?", (pid,))
        c.commit()
finally:
    ws.CURRENT.reset(tok)
rescan_as("homerepair", pid)
h, w = row("homerepair", pid), row(ws.PRIMARY, pid)
check("HomeRepair row scanned: done", h["scan_state"], "done")
check("HomeRepair row scanned: score", h["score"], 61)
check("Watson row still untouched: score", w["score"], None)
check("Watson row still untouched: no scanned_at", w["scanned_at"], None)

# -- and from the primary workspace itself, nothing changes -----------------
rescan_as(ws.PRIMARY, pid)
h, w = row("homerepair", pid), row(ws.PRIMARY, pid)
check("Watson rescan lands in Watson's file", w["score"], 61)
check("  and leaves HomeRepair's row alone", h["company"], "Alamo Roofing")

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
