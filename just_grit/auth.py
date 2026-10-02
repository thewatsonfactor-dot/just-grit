# -*- coding: utf-8 -*-
"""Sign in by email code (magic code), the front door for the cloud.

Until now the only identities were Cloudflare Access (public hostname) and
"local" (the owner at his own Mac). A stranger could not get in at all,
and the wrong email got a raw 403 string. This module adds the third
identity: a session cookie earned by typing a six-digit code that was
emailed to an address on the list.

Rules
- Codes: 6 digits, 10 minutes, one use, 5 wrong tries burns it.
- At most 5 codes per address per 15 minutes; the reply never says
  whether a code was sent to an address that isn't on the list.
- Sessions: 32-byte random token, only its SHA-256 stored, 30 days,
  refreshed on use. Cookie is httponly + samesite=lax, secure off the Mac.
- Tables live in the PRIMARY workspace's database: identity is
  account-level, which workspace an email may open is decided by
  require_auth exactly as before.
"""
import hashlib, secrets, sqlite3
from datetime import datetime, timedelta, timezone

CODE_TTL_MIN = 10
CODE_TRIES = 5
CODES_PER_WINDOW = 5
CODE_WINDOW_MIN = 15
SESSION_DAYS = 30
COOKIE = "jg_session"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def init(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS auth_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL, code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        tries INTEGER DEFAULT 0, used_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_auth_codes_email ON auth_codes(email, created_at);
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen TEXT);
    """)
    c.commit()


def norm(email: str) -> str:
    return (email or "").strip().lower()


def looks_like_email(email: str) -> bool:
    e = norm(email)
    return "@" in e and "." in e.split("@")[-1] and " " not in e and len(e) < 200


def issue_code(c: sqlite3.Connection, email: str):
    """Make a fresh code for this address. Returns (code, None) or
    (None, reason) when rate-limited. The caller emails the code."""
    email = norm(email)
    window = _iso(_now() - timedelta(minutes=CODE_WINDOW_MIN))
    n = c.execute("SELECT COUNT(*) FROM auth_codes WHERE email=? AND created_at>?", (email, window)).fetchone()[0]
    if n >= CODES_PER_WINDOW:
        return None, "too_many"
    code = "%06d" % secrets.randbelow(1_000_000)
    c.execute("UPDATE auth_codes SET used_at=COALESCE(used_at, ?) WHERE email=? AND used_at IS NULL",
              (_iso(_now()), email))   # one live code at a time
    c.execute("INSERT INTO auth_codes (email, code_hash, created_at, expires_at) VALUES (?,?,?,?)",
              (email, _h(code), _iso(_now()), _iso(_now() + timedelta(minutes=CODE_TTL_MIN))))
    c.commit()
    return code, None


def verify_code(c: sqlite3.Connection, email: str, code: str):
    """Burn the code and return a session token, or (None, reason):
    'expired' (no live code), 'wrong' (tries left), 'burned' (too many)."""
    email = norm(email)
    code = (code or "").strip().replace(" ", "")
    row = c.execute("SELECT * FROM auth_codes WHERE email=? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
                    (email,)).fetchone()
    if not row or row["expires_at"] < _iso(_now()):
        return None, "expired"
    if row["tries"] >= CODE_TRIES:
        return None, "burned"
    if not secrets.compare_digest(row["code_hash"], _h(code)):
        c.execute("UPDATE auth_codes SET tries=tries+1 WHERE id=?", (row["id"],))
        c.commit()
        return None, ("burned" if row["tries"] + 1 >= CODE_TRIES else "wrong")
    c.execute("UPDATE auth_codes SET used_at=? WHERE id=?", (_iso(_now()), row["id"]))
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions (token_hash, email, created_at, expires_at, last_seen) VALUES (?,?,?,?,?)",
              (_h(token), email, _iso(_now()), _iso(_now() + timedelta(days=SESSION_DAYS)), _iso(_now())))
    c.commit()
    return token, None


def session_email(c: sqlite3.Connection, token: str):
    """Who holds this cookie, or None. Slides the expiry on use."""
    if not token:
        return None
    row = c.execute("SELECT email, expires_at FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
    if not row or row["expires_at"] < _iso(_now()):
        return None
    c.execute("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
              (_iso(_now()), _iso(_now() + timedelta(days=SESSION_DAYS)), _h(token)))
    c.commit()
    return row["email"]


def end_session(c: sqlite3.Connection, token: str):
    c.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))
    c.commit()


def sweep(c: sqlite3.Connection):
    """Old codes and dead sessions out, once a day is plenty."""
    c.execute("DELETE FROM auth_codes WHERE created_at < ?", (_iso(_now() - timedelta(days=2)),))
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(_now()),))
    c.commit()


def code_email(code: str, product: str = "Just Grit") -> tuple:
    """(subject, body) for the code email. Plain, short, no link to click -
    a code typed into the page the person is already on beats a link that
    opens in a different browser."""
    subject = "%s is your %s sign-in code" % (code, product)
    body = ("Your sign-in code is %s\n\nIt works for %d minutes on the page where you asked for it. "
            "If you didn't ask for one, ignore this - nobody gets in without the code.\n\n%s"
            % (code, CODE_TTL_MIN, product))
    return subject, body
