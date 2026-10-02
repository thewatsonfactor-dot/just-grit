"""Run me:  just_grit/.venv/bin/python tests/test_workspaces.py

Does a request in one workspace actually stay in its own database?

This exercises the real mechanism from webapp.py - the same middleware, the
same db() helper shape, the same ws.carry() for threads - against the real workspaces.py.
The question it answers is not "did I write
the code" but "can HomeRepair's write ever land in Watson's file".
"""
import sys, pathlib, sqlite3, threading, time
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "just_grit"))
import workspaces as ws

DATA = pathlib.Path(__file__).resolve().parent / "_tmp_dbs"; DATA.mkdir(exist_ok=True)
POOL = ThreadPoolExecutor(max_workers=4)

def db(workspace: Optional[str] = None):
    c = sqlite3.connect(DATA / ws.db_filename(workspace), timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db(workspace=None):
    with closing(db(workspace)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS prospects (id INTEGER PRIMARY KEY, company TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
        # the CRM half: who, what they're worth, and every touch
        c.execute("CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY, title TEXT, value REAL, mrr REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS touches (id INTEGER PRIMARY KEY, note TEXT)")
        c.commit()

for slug in ws.WORKSPACES: init_db(slug)

app = FastAPI()

@app.middleware("http")
async def pick_workspace(request: Request, call_next):
    slug = ws.from_host(request.headers.get("host",""))
    sticky=None
    if not slug:
        asked=(request.query_params.get("ws") or "").strip().lower()
        if asked and ws.exists(asked): slug=sticky=asked
        else:
            cookie=(request.cookies.get("jg_ws") or "").strip().lower()
            slug=cookie if ws.exists(cookie) else ws.PRIMARY
    ws.use(slug)
    resp=await call_next(request)
    resp.headers["x-workspace"]=slug
    if sticky: resp.set_cookie("jg_ws",sticky,max_age=99999,samesite="lax")
    return resp

@app.post("/add")
def add(company: str):
    with closing(db()) as c:
        c.execute("INSERT INTO prospects(company) VALUES(?)",(company,)); c.commit()
    return {"ws": ws.current()}

@app.get("/list")
def lst():
    with closing(db()) as c:
        return {"ws": ws.current(),
                "companies":[r["company"] for r in c.execute("SELECT company FROM prospects")]}

@app.post("/crm")
def crm(name: str, deal: str, value: float, mrr: float, note: str):
    with closing(db()) as c:
        c.execute("INSERT INTO contacts(name) VALUES(?)",(name,))
        c.execute("INSERT INTO deals(title,value,mrr) VALUES(?,?,?)",(deal,value,mrr))
        c.execute("INSERT INTO touches(note) VALUES(?)",(note,))
        c.commit()
    return {"ws": ws.current()}

@app.get("/crm")
def crm_read():
    """What the Pipeline header shows: people, open value, MRR, history."""
    with closing(db()) as c:
        one=lambda q:(c.execute(q).fetchone()[0] or 0)
        return {"ws": ws.current(),
                "contacts":[r["name"] for r in c.execute("SELECT name FROM contacts")],
                "deals":[r["title"] for r in c.execute("SELECT title FROM deals")],
                "touches":[r["note"] for r in c.execute("SELECT note FROM touches")],
                "open_value": one("SELECT SUM(value) FROM deals"),
                "mrr": one("SELECT SUM(mrr) FROM deals")}

@app.post("/bg")
def bg(company: str):
    """Queue the write on a background thread, exactly like queue_scans does."""
    def work():
        with closing(db()) as c:
            c.execute("INSERT INTO prospects(company) VALUES(?)",(company,)); c.commit()
    POOL.submit(ws.carry(work))
    return {"queued_in": ws.current()}

c = TestClient(app)
W = {"host":"justgrit.thewatsonfactor.dev"}
H = {"host":"justgrit.homerepair.tech"}
fails=[]
def check(name, got, want):
    ok = got==want
    print(("  PASS  " if ok else "  FAIL  ")+name)
    if not ok:
        print("          got :",got); print("          want:",want); fails.append(name)

print("\n1. the hostname decides, and the request keeps it through the endpoint")
check("watson host -> watson",     c.post("/add?company=Blanco+Cafe", headers=W).json()["ws"], "watson")
check("homerepair host -> homerepair", c.post("/add?company=Alamo+Property+Mgmt", headers=H).json()["ws"], "homerepair")

print("\n2. neither one can see the other's rows")
check("watson sees only its own",     c.get("/list",headers=W).json()["companies"], ["Blanco Cafe"])
check("homerepair sees only its own", c.get("/list",headers=H).json()["companies"], ["Alamo Property Mgmt"])

print("\n3. a background thread writes to the workspace that queued it")
c.post("/bg?company=Hill+Country+Rentals", headers=H)
time.sleep(0.6)
check("thread wrote to homerepair", sorted(c.get("/list",headers=H).json()["companies"]),
      ["Alamo Property Mgmt","Hill Country Rentals"])
check("watson untouched by that thread", c.get("/list",headers=W).json()["companies"], ["Blanco Cafe"])

print("\n4. a stale cookie cannot override a known hostname")
c2 = TestClient(app); c2.cookies.set("jg_ws","watson")
check("homerepair host beats watson cookie", c2.get("/list",headers=H).json()["ws"], "homerepair")

print("\n5. on this Mac (no known host) ?ws= switches and sticks")
c3 = TestClient(app)
check("default is watson", c3.get("/list",headers={"host":"127.0.0.1:8080"}).json()["ws"], "watson")
check("?ws=homerepair switches", c3.get("/list?ws=homerepair",headers={"host":"127.0.0.1:8080"}).json()["ws"], "homerepair")
check("and is remembered",       c3.get("/list",headers={"host":"127.0.0.1:8080"}).json()["ws"], "homerepair")
check("a bogus ?ws= falls back", c3.get("/list?ws=evil",headers={"host":"127.0.0.1:8080"}).json()["ws"], "homerepair")

print("\n6. the files really are separate on disk")
files=sorted(p.name for p in DATA.glob("*.db"))
check("two database files", files, ["justgrit-homerepair.db","justgrit.db"])
raw={}
for f in files:
    with closing(sqlite3.connect(DATA/f)) as x:
        raw[f]=sorted(r[0] for r in x.execute("SELECT company FROM prospects"))
check("justgrit.db holds only Watson's",  raw["justgrit.db"], ["Blanco Cafe"])
check("homerepair file holds only its own", raw["justgrit-homerepair.db"],
      ["Alamo Property Mgmt","Hill Country Rentals"])

print("\n7. the CRM is two CRMs - people, deals and history never mix")
c.post("/crm?name=Maria+at+Blanco+Cafe&deal=AI+Vision+build&value=24000&mrr=750&note=Called+Tuesday",headers=W)
c.post("/crm?name=Rick+at+Alamo+Property&deal=40+doors+maintenance&value=0&mrr=3960&note=Walked+two+units",headers=H)
wc=c.get("/crm",headers=W).json(); hc=c.get("/crm",headers=H).json()
check("Watson's contacts",   wc["contacts"], ["Maria at Blanco Cafe"])
check("HomeRepair's contacts", hc["contacts"], ["Rick at Alamo Property"])
check("Watson's deals",      wc["deals"], ["AI Vision build"])
check("HomeRepair's deals",  hc["deals"], ["40 doors maintenance"])
check("Watson's timeline",   wc["touches"], ["Called Tuesday"])
check("HomeRepair's timeline", hc["touches"], ["Walked two units"])

print("\n8. and neither one's money shows up in the other's numbers")
check("Watson open value $24,000",  wc["open_value"], 24000)
check("Watson MRR $750",            wc["mrr"], 750)
check("HomeRepair open value $0",   hc["open_value"], 0)
check("HomeRepair MRR $3,960",      hc["mrr"], 3960)

print("\n" + ("ALL PASS" if not fails else "FAILURES: "+", ".join(fails)))
raise SystemExit(1 if fails else 0)
