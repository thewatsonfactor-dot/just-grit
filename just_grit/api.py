"""Just Grit Website Analyzer — API + dashboard, one origin, one command:

    uvicorn api:app --port 8080
    open http://localhost:8080/

Changes from v1 (the notes' "before this is public" list, done):
- Rate limiting: per-IP token bucket on /analyze (10/min) and /batch+/compare
  (6/hour). Tune with GRIT_RATE_PER_MINUTE / GRIT_BATCH_PER_HOUR.
- Domain suppression: suppressed_domains.txt is honored everywhere.
- CORS: locked by default (same-origin needs none). To open it, set
  GRIT_CORS_ORIGINS=https://app.justgrit.com — never "*" in production.
- Scans persist to SQLite (grit_scans.db) with /history for score-over-time
  and regression detection.
- Cache is TTL'd and bounded (in-process; move to Redis at multi-instance).
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from grit_analyzer import __version__, db, guard
from grit_analyzer.analyzer import analyze, analyze_batch
from grit_analyzer.compare import compare as compare_markets
from grit_analyzer.fetch import normalize_url
from grit_analyzer.report import render_report

app = FastAPI(title="Just Grit Website Analyzer", version=__version__)

# ------------------------------------------------------------------- CORS --
_origins = [o.strip() for o in os.environ.get("GRIT_CORS_ORIGINS", "").split(",")
            if o.strip()]
if _origins:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=_origins,
                       allow_methods=["GET", "POST"], allow_headers=["*"])

# ------------------------------------------------------------------ cache --
CACHE_TTL = int(os.environ.get("GRIT_CACHE_TTL_SECONDS", "900"))
CACHE_MAX = 500
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        if hit:
            del _cache[key]
    return None


def cache_put(key: str, value: dict):
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            del _cache[oldest]
        _cache[key] = (time.time(), value)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd
            else (request.client.host if request.client else "unknown"))


def _gate_single(request: Request):
    ip = _client_ip(request)
    if not guard.analyze_bucket.allow(ip):
        raise HTTPException(
            429, detail="Rate limit reached — try again in "
            f"{guard.analyze_bucket.retry_after(ip)}s.")


def _gate_batch(request: Request):
    ip = _client_ip(request)
    if not guard.batch_bucket.allow(ip):
        raise HTTPException(
            429, detail="Batch limit reached — try again in "
            f"{guard.batch_bucket.retry_after(ip)}s.")


# ---------------------------------------------------------------- routes --

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"),
                        media_type="text/html")


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "cached": len(_cache),
            "db": db.stats()}


@app.get("/analyze")
def analyze_route(request: Request, url: str = Query(..., min_length=3),
                  fresh: bool = False):
    if guard.is_suppressed(url):
        raise HTTPException(451, detail=guard.SUPPRESSED_MESSAGE)
    try:
        key = normalize_url(url)
    except ValueError:
        raise HTTPException(400, detail="That doesn't look like a domain.")
    if not fresh:
        hit = cache_get(key)
        if hit:
            return hit
    _gate_single(request)
    d = analyze(url, deep=True)
    if d.get("ok"):
        cache_put(key, d)
    db.save_scan(d)
    if not d.get("ok"):
        return JSONResponse(d, status_code=200)  # dashboard renders ok:false itself
    return d


@app.get("/report", response_class=HTMLResponse)
def report_route(request: Request, url: str = Query(..., min_length=3)):
    if guard.is_suppressed(url):
        raise HTTPException(451, detail=guard.SUPPRESSED_MESSAGE)
    key = normalize_url(url)
    d = cache_get(key)
    if d is None:
        _gate_single(request)
        d = analyze(url, deep=True)
        if d.get("ok"):
            cache_put(key, d)
        db.save_scan(d)
    return HTMLResponse(render_report(d))


class BatchBody(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=250)
    fast: bool = True


@app.post("/batch")
def batch_route(request: Request, body: BatchBody):
    _gate_batch(request)
    urls = [u for u in body.urls if u.strip()
            and not guard.is_suppressed(u)][:250]
    skipped = len(body.urls) - len(urls)
    rows = analyze_batch(urls, deep=not body.fast)
    return {"results": rows, "suppressed_skipped": skipped}


class CompareBody(BaseModel):
    urls: list[str] = Field(..., min_length=2, max_length=10)
    deep: bool = False


@app.post("/compare")
def compare_route(request: Request, body: CompareBody):
    _gate_batch(request)
    urls = [u for u in body.urls if not guard.is_suppressed(u)]
    if len(urls) < 2:
        raise HTTPException(400, detail="Need at least two non-suppressed sites.")
    return compare_markets(urls, deep=body.deep)


@app.get("/history")
def history_route(url: str = Query(..., min_length=3)):
    from urllib.parse import urlparse
    host = urlparse(normalize_url(url)).hostname
    return {"host": host, "history": db.history(host),
            "regression": db.regression(host)}
