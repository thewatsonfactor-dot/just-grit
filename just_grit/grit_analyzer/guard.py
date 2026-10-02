"""Rate limiting + domain suppression — the two things the v1 notes said
were required before the analyzer is exposed to anyone outside the company.

Rate limit: token-bucket per client IP, in-memory. Enough for one instance;
move the counters to Redis when there's more than one.

Suppression: a plain-text file of domains we will not scan — site owners who
asked to be left alone, competitors, anyone legal says to skip. Honoring
opt-outs is the same non-negotiable as honoring robots.txt.
"""

from __future__ import annotations

import os
import re
import threading
import time
from urllib.parse import urlparse

# ------------------------------------------------------------- rate limit --

RATE_PER_MINUTE = int(os.environ.get("GRIT_RATE_PER_MINUTE", "10"))
BATCH_PER_HOUR = int(os.environ.get("GRIT_BATCH_PER_HOUR", "6"))


class TokenBucket:
    def __init__(self, capacity: int, window_seconds: float):
        self.capacity = capacity
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.capacity:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            # opportunistic cleanup so the dict can't grow unbounded
            if len(self._hits) > 5000:
                cutoff = now - self.window
                self._hits = {k: v for k, v in self._hits.items()
                              if v and v[-1] > cutoff}
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            hits = self._hits.get(key, [])
        if not hits:
            return 0
        return max(1, int(self.window - (time.time() - hits[0])) + 1)


analyze_bucket = TokenBucket(RATE_PER_MINUTE, 60.0)
batch_bucket = TokenBucket(BATCH_PER_HOUR, 3600.0)

# ------------------------------------------------------------ suppression --

SUPPRESSION_FILE = os.environ.get(
    "GRIT_SUPPRESSION_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "suppressed_domains.txt"))

_supp_lock = threading.Lock()
# One cache entry per file. The list used to be a single process-wide set,
# which was fine while every caller was the same owner; it is not fine once a
# customer workspace can call suppress() - see webapp.suppression_paths().
_supp_cache: dict[str, tuple[float, set[str]]] = {}


def _norm_domain(raw: str) -> str:
    raw = raw.strip().lower()
    if "://" in raw:
        raw = urlparse(raw).netloc or raw
    raw = raw.split("/")[0].split(":")[0]
    return re.sub(r"^www\.", "", raw)


def _load(path: str | None = None) -> set[str]:
    path = path or SUPPRESSION_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return set()
    with _supp_lock:
        hit = _supp_cache.get(path)
        if hit is None or hit[0] != mtime:
            with open(path, encoding="utf-8") as fh:
                doms = {_norm_domain(line) for line in fh
                        if line.strip() and not line.startswith("#")}
            _supp_cache[path] = (mtime, doms)
            return doms
        return hit[1]


def is_suppressed(url_or_domain: str, paths=None) -> bool:
    """Is this domain on any of the given lists (default: the account list)?

    `paths` is a list of files; a domain on ANY of them is suppressed. A
    workspace passes its own file plus the account's, so the owner's global
    do-not-scan decisions still hold everywhere while a customer's own
    dead/corporate marks stay inside that customer's workspace.
    """
    d = _norm_domain(url_or_domain)
    for path in (paths or [SUPPRESSION_FILE]):
        if any(d == s or d.endswith("." + s) for s in _load(path)):
            return True
    return False


def suppress(domain: str, path: str | None = None) -> None:
    """Append a domain to a suppression list (default: the account list)."""
    path = path or SUPPRESSION_FILE
    d = _norm_domain(domain)
    with _supp_lock:
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if d not in existing:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(d + "\n")
        _supp_cache.pop(path, None)


SUPPRESSED_MESSAGE = (
    "This domain is on our do-not-scan list. If the owner asked us not to "
    "audit their site, we honor that the same way we honor robots.txt.")
