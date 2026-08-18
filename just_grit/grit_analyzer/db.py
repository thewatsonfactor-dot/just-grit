"""Scan persistence — SQLite by default, zero setup.

v1 kept everything in an in-process dict and lost it on restart. This gives
score history and regression detection out of the box. The schema mirrors
what the Supabase/Postgres version will use, so migrating later is a
copy-paste, not a redesign (set GRIT_DB_PATH to move the file; the Supabase
schema lives in the README).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

_DB_PATH = os.environ.get("GRIT_DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "grit_scans.db"))
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        ts REAL NOT NULL,
        ok INTEGER NOT NULL,
        overall INTEGER,
        grade TEXT,
        scores_json TEXT,
        findings_json TEXT,
        stack_json TEXT,
        metrics_json TEXT,
        opening_line TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_scans_host_ts ON scans(host, ts)")
    return conn


def save_scan(d: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO scans (host, ts, ok, overall, grade, scores_json, "
            "findings_json, stack_json, metrics_json, opening_line) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d.get("host") or d.get("url", "?"), time.time(),
             1 if d.get("ok") else 0,
             d.get("overall"), d.get("grade"),
             json.dumps(d.get("scores")) if d.get("ok") else None,
             json.dumps(d.get("findings")) if d.get("ok") else None,
             json.dumps(d.get("stack")) if d.get("ok") else None,
             json.dumps(d.get("metrics")) if d.get("ok") else None,
             d.get("opening_line")))


def history(host: str, limit: int = 30) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT ts, ok, overall, grade, scores_json FROM scans "
            "WHERE host = ? ORDER BY ts DESC LIMIT ?", (host, limit)).fetchall()
    return [{"ts": r[0], "ok": bool(r[1]), "overall": r[2], "grade": r[3],
             "scores": json.loads(r[4]) if r[4] else None} for r in rows]


def regression(host: str) -> dict | None:
    """Compare the two most recent successful scans. Returns a delta dict if
    the score moved, else None. This powers 'their site got worse — call now'
    alerts and 'our fix raised the score 11 points' proof."""
    runs = [h for h in history(host, 10) if h["ok"] and h["overall"] is not None]
    if len(runs) < 2:
        return None
    latest, prev = runs[0], runs[1]
    delta = latest["overall"] - prev["overall"]
    if delta == 0:
        return None
    return {"host": host, "delta": delta,
            "from": prev["overall"], "to": latest["overall"],
            "direction": "improved" if delta > 0 else "regressed"}


def stats() -> dict:
    with _lock, _conn() as conn:
        total, hosts = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT host) FROM scans").fetchone()
    return {"scans": total, "hosts": hosts}
