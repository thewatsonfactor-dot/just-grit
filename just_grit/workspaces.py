"""Which business am I working right now.

One codebase runs two businesses. The Watson Factor sells custom software to
other businesses; HomeRepair Tech sells home maintenance subscriptions. They
share every mechanism — the queue, the scoring, the outbox, the timeline —
and they share no data whatsoever.

**Isolation is by file, not by a `workspace` column.** A column would mean 109
SQL statements in webapp.py each needing a WHERE clause, and the first one
anybody forgets puts HomeRepair's leads into The Watson Factor's call queue
with no error and no clue. Separate database files make that failure
impossible to write rather than merely unlikely to happen. The cost is that
there can be no cross-workspace query — which is the right constraint anyway.

The Watson Factor keeps the original `justgrit.db` filename. Renaming it would
strand every backup taken before today for no gain.

A few things are genuinely account-level and stay in the primary file: who is
allowed to sign in, the Google key, and the monthly Google request counter.
One Google project, one bill, one cap — splitting those per workspace would
just mean double-counting the same money.
"""
from __future__ import annotations

import contextvars
import json
import os
import pathlib
import re
import sqlite3

PRIMARY = "watson"

WORKSPACES = {
    "watson": {
        "slug":    "watson",
        "name":    "The Watson Factor",
        "short":   "Watson Factor",
        "product": "Just Grit",
        "db":      "justgrit.db",
        "hosts":   ["justgrit.thewatsonfactor.dev"],
        "accent":  "#e8a33d",          # brass — the existing brand
        "sells":   "custom software: AI Vision, ordering apps, websites, AI receptionist",
        "buyer":   "local business owners",
    },
    "homerepair": {
        "slug":    "homerepair",
        "name":    "HomeRepair Tech",
        "short":   "HomeRepair",
        "product": "ProActive Home Care",
        "db":      "justgrit-homerepair.db",
        # Two hostnames on purpose. justgrit.homerepair.tech is the one we
        # want, but it needs the zone on Cloudflare - a tunnel hostname is a
        # CNAME to <uuid>.cfargotunnel.com, which resolves for nobody except
        # Cloudflare's own resolvers. homerepair.tech's nameservers are
        # Namecheap's (pdns1/pdns2.registrar-servers.com), so until that moves,
        # the workspace rides on a thewatsonfactor.dev subdomain, which already
        # has the tunnel and Access in front of it.
        # The FIRST host is where the workspace switcher sends you, so the one
        # that actually resolves goes first (2026-09-27: the switcher on
        # justgrit.thewatsonfactor.dev was sending Daniel to the dead
        # justgrit.homerepair.tech). Swap back once homerepair.tech's DNS is
        # on Cloudflare.
        "hosts":   ["homerepair.thewatsonfactor.dev", "justgrit.homerepair.tech"],
        "accent":  "#ff7a2f",          # hunter orange on a camo field — see
                                       # [data-ws="homerepair"] in dashboard.html
        "sells":   "home maintenance subscriptions and the free Home Health Score",
        "buyer":   "property managers, landlords and custom home builders",
    },
}

# ── customers, not code ──────────────────────────────────────────────────
# The two dicts above are the businesses this was built for, and they stay in
# code because they predate the registry and their filenames are load-bearing.
# Everything added AFTER them is data.
#
# That distinction is the difference between a tool and a product. Onboarding
# a paying customer used to mean editing this file, creating a database,
# adding a hostname, and redeploying - which is to say every sale carried a
# development task, and nobody can scale that. A workspace is now a row: the
# app reads the registry at import, merges it over the built-ins, and a new
# customer is live without anyone touching Python.
#
# Isolation does NOT change. Every workspace still gets its own database file,
# for exactly the reason in the docstring above: a forgotten WHERE clause
# cannot leak one customer's leads into another's queue if the rows were never
# in the same file to begin with.
REGISTRY_TABLE = "workspace_registry"

_SLUG_RX = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def _data_dir() -> pathlib.Path:
    """Where the databases live. Mirrors webapp's DATA so both agree."""
    env = os.environ.get("JUST_GRIT_DATA", "").strip()
    if env:
        return pathlib.Path(env)
    return pathlib.Path(__file__).resolve().parent.parent


def valid_slug(slug: str) -> bool:
    """Lowercase, dash-separated, no path tricks - this becomes a filename."""
    return bool(_SLUG_RX.match((slug or "").strip()))


def _registry_rows() -> list:
    """Customer workspaces from the primary database.

    Deliberately forgiving: if the table doesn't exist yet, or the file is
    missing, or a row is malformed, the app still boots on the built-ins. A
    registry problem must never be able to take the whole system down - the
    two businesses already running are more important than the newest row.
    """
    path = _data_dir() / WORKSPACES[PRIMARY]["db"]
    if not path.exists():
        return []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT slug, name, short, product, db, hosts, accent, sells, buyer "
            "FROM %s WHERE COALESCE(active,1)=1" % REGISTRY_TABLE).fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for r in rows:
        if not valid_slug(r["slug"]) or r["slug"] in (PRIMARY,):
            continue
        try:
            hosts = json.loads(r["hosts"] or "[]")
        except Exception:
            hosts = []
        out.append({
            "slug": r["slug"], "name": r["name"] or r["slug"],
            "short": r["short"] or r["name"] or r["slug"],
            "product": r["product"] or "Just Grit",
            "db": r["db"] or ("justgrit-%s.db" % r["slug"]),
            "hosts": [h for h in hosts if isinstance(h, str)],
            "accent": r["accent"] or "#e8a33d",
            "sells": r["sells"] or "", "buyer": r["buyer"] or "",
        })
    return out


def reload_registry() -> int:
    """Re-read customer workspaces. Returns how many are loaded."""
    added = 0
    for w in _registry_rows():
        WORKSPACES[w["slug"]] = w
        added += 1
    return added


# Settings that belong to the account rather than to one business.
GLOBAL_SETTINGS = {"allowed_emails", "google_key", "free_request_cap"}

try:
    reload_registry()
except Exception:
    pass          # never let a registry problem stop the app from booting

CURRENT = contextvars.ContextVar("workspace", default=PRIMARY)


def exists(slug: str) -> bool:
    return slug in WORKSPACES


def current() -> str:
    return CURRENT.get()


def use(slug: str) -> str:
    """Point the rest of this request (or thread) at one workspace."""
    if not exists(slug):
        slug = PRIMARY
    CURRENT.set(slug)
    return slug


def info(slug: str | None = None) -> dict:
    return WORKSPACES.get(slug or current()) or WORKSPACES[PRIMARY]


def db_filename(slug: str | None = None) -> str:
    return info(slug)["db"]


def from_host(host: str) -> str | None:
    """Map a Host header to a workspace. Returns None for anything unknown
    (localhost, an IP, a new tunnel hostname) so the caller can fall back."""
    h = (host or "").split(":")[0].strip().lower()
    if not h:
        return None
    for slug, w in WORKSPACES.items():
        if h in w["hosts"]:
            return slug
    return None


def carry(fn, *a, **kw):
    """Wrap a callable so it runs in the caller's workspace.

    A ContextVar is task-local and a thread started from a request does NOT
    inherit it — so a scan queued while working HomeRepair would otherwise
    wake up on a fresh thread, read the default, and write its findings into
    The Watson Factor's database. Capture the slug at submit time and set it
    again inside the worker.
    """
    slug = current()

    def run():
        CURRENT.set(slug)
        return fn(*a, **kw)

    run.__name__ = getattr(fn, "__name__", "worker")
    return run


def me() -> dict:
    """The current workspace, minus anything the browser has no business
    knowing (the database filename)."""
    w = info()
    return {"slug": w["slug"], "name": w["name"], "short": w["short"],
            "product": w["product"], "accent": w["accent"],
            "sells": w["sells"], "buyer": w["buyer"]}


def public() -> list[dict]:
    """What the browser is allowed to know about the workspaces."""
    return [{"slug": w["slug"], "name": w["name"], "short": w["short"],
             "product": w["product"], "accent": w["accent"],
             "sells": w["sells"], "buyer": w["buyer"],
             "host": (w["hosts"] or [""])[0]}
            for w in WORKSPACES.values()]
