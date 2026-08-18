"""
Just Grit — the whole thing, one app.

Find businesses → score their sites → get the sentence to say → call them →
log what happened. No scripts, no CSVs, no terminal.

    ./.venv/bin/uvicorn webapp:app --host 127.0.0.1 --port 8080

Auth is handled by Cloudflare Access at the edge (Google sign-in) before any
request reaches this machine. This app additionally verifies the header Access
injects, so the port can't be used directly by anything else on the network.
"""
from __future__ import annotations

import csv, io, json, os, pathlib, re, sqlite3, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from contextlib import closing
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel, Field

from grit_analyzer.analyzer import analyze, PLAIN_MAP, PLAIN_ORDER
from grit_analyzer.report import render_report
from grit_analyzer import guard

ROOT = pathlib.Path(__file__).parent
DB_PATH = ROOT.parent / "justgrit.db"
UI = ROOT / "static" / "app.html"

app = FastAPI(title="Just Grit", docs_url=None, redoc_url=None)
POOL = ThreadPoolExecutor(max_workers=4)


# ─────────────────────────── storage ───────────────────────────

def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id TEXT UNIQUE, company TEXT, domain TEXT, phone TEXT,
            city TEXT, address TEXT, rating REAL, reviews INTEGER,
            vertical TEXT DEFAULT 'generic',
            gap TEXT DEFAULT '', say TEXT DEFAULT '', email TEXT DEFAULT '',
            score INTEGER, grade TEXT, opener TEXT DEFAULT '',
            findings TEXT DEFAULT '[]', scan_state TEXT DEFAULT 'pending',
            scan_error TEXT DEFAULT '',
            status TEXT DEFAULT 'new', note TEXT DEFAULT '',
            added_at TEXT, scanned_at TEXT, touched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_status ON prospects(status);
        CREATE INDEX IF NOT EXISTS ix_domain ON prospects(domain);
        """)
        c.commit()


def setting(k, default=""):
    with closing(db()) as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(k, v):
    with closing(db()) as c:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        c.commit()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


init_db()


# ─────────────────────────── auth ───────────────────────────
# Cloudflare Access authenticates with Google at the edge and injects the
# verified email as a header. We trust that header ONLY because nothing but
# the tunnel can reach this process — the app binds to 127.0.0.1 and the
# tunnel is the single ingress. Requests arriving without the header are
# allowed only from the loopback interface, which is how local use works.

ACCESS_HEADER = "cf-access-authenticated-user-email"


def caller_email(request: Request) -> Optional[str]:
    return (request.headers.get(ACCESS_HEADER) or "").strip().lower() or None


def allowed_emails() -> set[str]:
    raw = setting("allowed_emails", "")
    return {e.strip().lower() for e in re.split(r"[,\s]+", raw) if e.strip()}


def require_auth(request: Request) -> str:
    email = caller_email(request)
    if email:
        allow = allowed_emails()
        if allow and email not in allow:
            raise HTTPException(403, f"{email} is not on the allowed list.")
        return email
    host = (request.client.host if request.client else "") or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return "local"
    raise HTTPException(403, "Sign in required.")


# ─────────────────────── Google Places ───────────────────────

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.websiteUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "nextPageToken",
])

THIRD_PARTY = {
    "order.online": "DoorDash's white-label ordering page",
    "doordash.com": "DoorDash", "ubereats.com": "Uber Eats",
    "grubhub.com": "Grubhub", "toasttab.com": "Toast",
    "clover.com": "Clover", "square.site": "a Square hosted page",
    "squareup.com": "Square", "facebook.com": "a Facebook page",
    "instagram.com": "an Instagram profile", "linktr.ee": "a Linktree",
    "booksy.com": "Booksy", "styleseat.com": "StyleSeat",
    "vagaro.com": "Vagaro", "wixsite.com": "a free Wix subdomain",
    "business.site": "a Google auto-generated page",
}


def domain_of(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"^https?://", "", url.strip()).split("/")[0].lower().replace("www.", "")


def places_search(query: str, key: str, pages: int = 2, region: str = "us"):
    out, token = [], None
    for i in range(max(1, min(pages, 3))):
        body = {"textQuery": query, "regionCode": region, "languageCode": "en"}
        if token:
            body["pageToken"] = token
        req = urllib.request.Request(
            PLACES_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": FIELD_MASK}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code == 403:
                raise HTTPException(400, "Google rejected the key. Check that "
                                         "'Places API (New)' is enabled and the key "
                                         "isn't restricted to a different API.")
            raise HTTPException(400, f"Google returned {e.code}: {msg}")
        except Exception as e:
            raise HTTPException(502, f"Could not reach Google: {type(e).__name__}")
        out.extend(data.get("places") or [])
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(2)
    return out


def auto_angle(p: dict, name_counts: dict):
    site = p.get("websiteUri") or ""
    dom = domain_of(site)
    name = (p.get("displayName") or {}).get("text", "")
    reviews = p.get("userRatingCount") or 0
    rating = p.get("rating")

    for host, label in THIRD_PARTY.items():
        if host in dom:
            return (f"Doesn't own their site — points at {label}",
                    f"the website on your Google listing is actually {label}, not "
                    f"something you own")
    if not site:
        return ("No website at all on their Google listing",
                "you don't have a website on your Google listing at all — people "
                "searching for you find a phone number and nothing else")
    n = name_counts.get(name, 1)
    if n >= 3:
        return (f"{n} locations in this search",
                f"you've got at least {n} locations, and I couldn't find one place "
                f"where you can see all of them at once")
    if reviews >= 200 and rating and rating >= 4.3:
        return (f"{reviews} reviews at {rating}★",
                f"you've got {reviews} reviews at {rating} stars and almost none of "
                f"that shows up on your own website")
    return ("", "")


# ─────────────────────── scanning ───────────────────────


# The analyzer's plain-spoken lines were written for contractors. A few of them
# land wrong on a restaurant or a barbershop, so re-word those per vertical.
# Anything not listed here falls through to the analyzer's own wording.
VERTICAL_PHRASING = {
    "restaurant": {
        "no_form": "there's no way to order or reach you from the site after hours — "
                   "the 9 PM hungry person can't do anything",
        "form_only_contact": "ordering takes an extra click that most people never make",
        "no_reviews_shown": "your Google reviews don't show anywhere on your own site — "
                            "you've earned stars nobody sees",
        "no_trust": "nothing on the page says how long you've been around or what "
                    "you're known for",
    },
    "appointment": {
        "no_form": "there's no way to book online — if they can't call right then, "
                   "they book with whoever they can",
        "form_only_contact": "booking takes an extra click that most people never make",
        "no_trust": "nothing on the page says who your people are or how long "
                    "you've been cutting",
    },
    "multilocation": {
        "no_form": "there's no way for someone to reach a specific location after hours",
    },
}


def best_opener(d: dict, vertical: str = "generic") -> str:
    """Shortest human sentence for the phone. Reuses the analyzer's own map."""
    findings = [f for f in (d.get("findings") or []) if f.get("severity") != "good"]
    ranked = sorted(
        (f for f in findings if f.get("key") in PLAIN_ORDER),
        key=lambda f: PLAIN_ORDER[f["key"]])
    if ranked:
        key = ranked[0]["key"]
        phrase = VERTICAL_PHRASING.get(vertical, {}).get(key) or PLAIN_MAP[key]
        try:
            return phrase.format(load=(d.get("metrics") or {}).get("load_seconds", 0) or 0)
        except Exception:
            return phrase
    if findings:
        t = findings[0].get("title", "")
        return (t[0].lower() + t[1:]) if t else ""
    return ""


def scan_one(pid: int):
    with closing(db()) as c:
        row = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not row:
            return
        c.execute("UPDATE prospects SET scan_state='scanning' WHERE id=?", (pid,))
        c.commit()

    dom = row["domain"]
    if not dom:
        with closing(db()) as c:
            c.execute("UPDATE prospects SET scan_state='no_site', scanned_at=? WHERE id=?",
                      (now(), pid))
            c.commit()
        return

    try:
        d = analyze(dom, deep=False)
        ok = bool(d.get("ok"))
        opener = best_opener(d, row["vertical"] or "generic") if ok else ""
        keep = [{"title": f.get("title"), "severity": f.get("severity"),
                 "evidence": f.get("evidence"), "fix": f.get("fix")}
                for f in (d.get("findings") or []) if f.get("severity") != "good"][:6]
        with closing(db()) as c:
            c.execute("""UPDATE prospects SET score=?, grade=?, opener=?, findings=?,
                         scan_state=?, scan_error=?, scanned_at=? WHERE id=?""",
                      (d.get("overall") if ok else None, d.get("grade") if ok else None,
                       opener, json.dumps(keep),
                       "done" if ok else "failed",
                       "" if ok else str(d.get("error") or "site did not load")[:200],
                       now(), pid))
            c.commit()
    except Exception as e:
        with closing(db()) as c:
            c.execute("UPDATE prospects SET scan_state='failed', scan_error=?, scanned_at=? "
                      "WHERE id=?", (f"{type(e).__name__}: {e}"[:200], now(), pid))
            c.commit()


def queue_scans(ids):
    for i in ids:
        POOL.submit(scan_one, i)


# ─────────────────────── routes ───────────────────────

@app.get("/", include_in_schema=False)
def home(request: Request):
    require_auth(request)
    if not UI.exists():
        raise HTTPException(500, "app.html missing")
    return FileResponse(UI, media_type="text/html")


@app.get("/api/me")
def me(request: Request):
    email = require_auth(request)
    return {
        "email": email,
        "has_google_key": bool(setting("google_key")),
        "sender_name": setting("sender_name", ""),
        "sender_phone": setting("sender_phone", ""),
        "sender_site": setting("sender_site", ""),
        "sender_address": setting("sender_address", ""),
        "allowed_emails": setting("allowed_emails", ""),
        "default_city": setting("default_city", "San Antonio, TX"),
    }


class Settings(BaseModel):
    google_key: Optional[str] = None
    sender_name: Optional[str] = None
    sender_phone: Optional[str] = None
    sender_site: Optional[str] = None
    sender_address: Optional[str] = None
    allowed_emails: Optional[str] = None
    default_city: Optional[str] = None


@app.post("/api/settings")
def save_settings(request: Request, body: Settings):
    require_auth(request)
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "google_key" and not v.strip():
            continue
        set_setting(k, v.strip())
    return {"ok": True}


class FindBody(BaseModel):
    query: str = Field(..., min_length=2)
    city: str = ""
    pages: int = 2
    vertical: str = "generic"


@app.post("/api/find")
def find(request: Request, body: FindBody):
    require_auth(request)
    key = setting("google_key")
    if not key:
        raise HTTPException(400, "No Google Maps key saved yet — add one in Settings.")
    city = body.city or setting("default_city", "San Antonio, TX")
    places = places_search(f"{body.query} in {city}", key, body.pages)

    counts = {}
    for p in places:
        n = (p.get("displayName") or {}).get("text", "")
        counts[n] = counts.get(n, 0) + 1

    added, skipped, new_ids = 0, 0, []
    with closing(db()) as c:
        for p in places:
            if p.get("businessStatus") not in (None, "OPERATIONAL"):
                skipped += 1
                continue
            pid = p.get("id") or ""
            name = (p.get("displayName") or {}).get("text", "").strip()
            dom = domain_of(p.get("websiteUri"))
            if dom and guard.is_suppressed(dom):
                skipped += 1
                continue
            gap, say = auto_angle(p, counts)
            try:
                cur = c.execute("""INSERT INTO prospects
                    (place_id, company, domain, phone, city, address, rating, reviews,
                     vertical, gap, say, added_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, name, dom, p.get("nationalPhoneNumber") or "", city,
                     p.get("formattedAddress") or "", p.get("rating"),
                     p.get("userRatingCount") or 0, body.vertical, gap, say, now()))
                added += 1
                new_ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                skipped += 1
        c.commit()

    queue_scans(new_ids)
    return {"found": len(places), "added": added, "skipped": skipped}


class AddBody(BaseModel):
    text: str
    vertical: str = "generic"
    city: str = ""


@app.post("/api/add")
def add_manual(request: Request, body: AddBody):
    """Paste anything: bare domains, or 'Name, domain, phone' per line."""
    require_auth(request)
    added, new_ids = 0, []
    with closing(db()) as c:
        for line in body.text.splitlines():
            line = line.strip().strip(",")
            if not line:
                continue
            parts = [x.strip() for x in re.split(r"[,\t]", line)]
            company = dom = phone = ""
            for part in parts:
                if not part:
                    continue
                if re.search(r"\d{3}[^\d]*\d{3}[^\d]*\d{4}", part) and not phone:
                    phone = part
                elif re.search(r"\.[a-z]{2,}", part, re.I) and not dom:
                    dom = domain_of(part)
                elif not company:
                    company = part
            if not dom and len(parts) == 1:
                dom = domain_of(parts[0])
            if not dom:
                continue
            if guard.is_suppressed(dom):
                continue
            try:
                cur = c.execute("""INSERT INTO prospects
                    (place_id, company, domain, phone, city, vertical, added_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (f"manual:{dom}", company or dom, dom, phone,
                     body.city or setting("default_city", ""), body.vertical, now()))
                added += 1
                new_ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                pass
        c.commit()
    queue_scans(new_ids)
    return {"added": added}


def row_to_dict(r):
    d = dict(r)
    try:
        d["findings"] = json.loads(d.get("findings") or "[]")
    except Exception:
        d["findings"] = []
    # what to actually say: human research first, scanner second
    d["line"] = (d.get("say") or "").strip() or (d.get("opener") or "").strip()
    return d


@app.get("/api/prospects")
def list_prospects(request: Request, status: str = "", q: str = "", limit: int = 300):
    require_auth(request)
    sql = "SELECT * FROM prospects"
    args, where = [], []
    if status and status != "all":
        where.append("status = ?"); args.append(status)
    if q:
        where.append("(company LIKE ? OR domain LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    # worst sites first, unscanned last, but anything with a human-written
    # angle floats up regardless of score
    sql += """ ORDER BY
        CASE WHEN say <> '' THEN 0 ELSE 1 END,
        CASE WHEN scan_state='no_site' THEN 0 WHEN scan_state='failed' THEN 1 ELSE 2 END,
        COALESCE(score, 999) ASC, id DESC LIMIT ?"""
    args.append(limit)
    with closing(db()) as c:
        rows = [row_to_dict(r) for r in c.execute(sql, args).fetchall()]
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM prospects GROUP BY status").fetchall()}
        pending = c.execute(
            "SELECT COUNT(*) n FROM prospects WHERE scan_state IN ('pending','scanning')"
        ).fetchone()["n"]
    return {"prospects": rows, "counts": counts, "scanning": pending}


class StatusBody(BaseModel):
    status: str
    note: str = ""


@app.post("/api/prospect/{pid}/status")
def set_status(request: Request, pid: int, body: StatusBody):
    require_auth(request)
    if body.status not in ("new", "called", "booked", "dead", "emailed"):
        raise HTTPException(400, "unknown status")
    with closing(db()) as c:
        cur = c.execute("UPDATE prospects SET status=?, note=?, touched_at=? WHERE id=?",
                        (body.status, body.note, now(), pid))
        c.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "not found")
    # "dead" means never contact again — honor it in the shared suppression list
    if body.status == "dead":
        with closing(db()) as c:
            r = c.execute("SELECT domain FROM prospects WHERE id=?", (pid,)).fetchone()
        if r and r["domain"]:
            try:
                guard.suppress(r["domain"])
            except Exception:
                pass
    return {"ok": True}


@app.post("/api/prospect/{pid}/rescan")
def rescan(request: Request, pid: int):
    require_auth(request)
    with closing(db()) as c:
        c.execute("UPDATE prospects SET scan_state='pending' WHERE id=?", (pid,))
        c.commit()
    POOL.submit(scan_one, pid)
    return {"ok": True}


@app.get("/api/prospect/{pid}/email")
def draft_email(request: Request, pid: int):
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    d = row_to_dict(r)

    addr = setting("sender_address", "").strip()
    name = setting("sender_name", "")
    phone = setting("sender_phone", "")
    site = setting("sender_site", "")

    bullets = "\n".join(f"  • {f['title']}" for f in d["findings"][:3])
    extra = f"\nThe scan also flagged:\n{bullets}\n" if bullets else "\n"
    hook = d["line"] or "a couple of things worth a look"

    body = f"""Hi,

I run a small software shop here in San Antonio. I was looking at {d['domain']} \
this morning and noticed {hook}.

Not a sales email — I built a tool that checks local business websites and yours \
came through it, so I figured I'd pass along what it found.
{extra}
If it's ever worth a conversation I'm easy to reach. If not, just say so and I \
won't email you again.

{name}
The Watson Factor Development
{phone} · {site}

—
{addr or '[ADD YOUR MAILING ADDRESS IN SETTINGS — required by law]'}
Reply "stop" and I'll remove you permanently.
"""
    return {"to": d.get("email") or "", "subject": f"{d['company']} — one thing I noticed",
            "body": body, "address_missing": not addr}


@app.get("/report", response_class=HTMLResponse)
def report(request: Request, url: str = Query(..., min_length=3)):
    require_auth(request)
    d = analyze(url, deep=True)
    return HTMLResponse(render_report(d))


@app.get("/api/export.csv")
def export_csv(request: Request):
    require_auth(request)
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM prospects ORDER BY id").fetchall()
    buf = io.StringIO()
    cols = ["company", "domain", "phone", "city", "address", "rating", "reviews",
            "score", "grade", "gap", "say", "opener", "status", "note", "email"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(dict(r))
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="just-grit-prospects.csv"'})


@app.get("/healthz", include_in_schema=False)
def healthz():
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) n FROM prospects").fetchone()["n"]
    return {"ok": True, "prospects": n}
