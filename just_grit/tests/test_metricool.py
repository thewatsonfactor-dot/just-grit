# -*- coding: utf-8 -*-
"""The Metricool client, without touching the network.

Everything asserted here is a thing that silently produces a wrong request if
you get it wrong: the auth header name, the userId/blogId on every call, and
the draft-by-default rule.
"""
import sys, json
sys.path.insert(0, '.')
import metricool as M

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %-56s %s" % ("ok" if ok else "FAIL", name, "" if ok else repr(got)))

# ── the request shape ────────────────────────────────────────────────
seen = {}
class FakeMC(M.Metricool):
    def _call(self, method, path, params=None, body=None):
        seen.clear(); seen.update(method=method, path=path,
                                  params=dict(params or {}), body=body)
        return {}

c = FakeMC("tok123", "5270354", "6848830")
c.schedule_post("hello", "2026-09-12T09:00:00", ["instagram", "facebook"])
check("schedule uses POST", seen["method"], "POST")
check("schedule path", seen["path"], "/v2/scheduler/posts")
check("providers are objects with a network key",
      seen["body"]["providers"], [{"network": "instagram"}, {"network": "facebook"}])
check("publicationDate carries its timezone",
      seen["body"]["publicationDate"], {"dateTime": "2026-09-12T09:00:00",
                                        "timezone": "America/Chicago"})

# ── the rule that matters ────────────────────────────────────────────
check("a post is a DRAFT unless told otherwise", seen["body"]["draft"], True)
check("and never auto-publishes by default", seen["body"]["autoPublish"], False)
c.schedule_post("go", "2026-09-12T09:00:00", ["instagram"], auto_publish=True)
check("auto_publish=True clears draft", seen["body"]["draft"], False)
c.schedule_post("hold", "2026-09-12T09:00:00", ["instagram"],
                auto_publish=True, draft=True)
check("an explicit draft=True still wins over auto_publish",
      seen["body"]["draft"], True)
check("and sets autoPublish", seen["body"]["autoPublish"], True)

# ── auth is the classic trip-up ──────────────────────────────────────
import urllib.request
captured = {}
def fake_urlopen(req, timeout=None):
    captured["url"] = req.full_url
    captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
    class R:
        def read(self): return b'{}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()
urllib.request.urlopen = fake_urlopen
real = M.Metricool("tok123", "5270354", "6848830")
real.scheduled_posts("2026-09-10T00:00:00", "2026-09-20T00:00:00")
check("auth header is X-Mc-Auth, not Bearer",
      captured["headers"].get("x-mc-auth"), "tok123")
check("no Authorization header is sent",
      "authorization" in captured["headers"], False)
check("userId is on the query string", "userId=5270354" in captured["url"], True)
check("blogId is on the query string", "blogId=6848830" in captured["url"], True)

# ── refuse impossible calls before spending a round-trip ─────────────
def raises(fn, *a, **k):
    try: fn(*a, **k); return False
    except M.MetricoolError: return True
check("tiktok competitors are refused", raises(c.competitors, "tiktok", "a", "b"), True)
check("linkedin competitors are refused", raises(c.competitors, "linkedin", "a", "b"), True)
check("instagram competitors are allowed",
      raises(c.competitors, "instagram", "a", "b"), False)
check("besttimes rejects an unsupported provider",
      raises(c.best_times, "pinterest", "a", "b"), True)
check("missing credentials fail loudly, not on the wire",
      raises(M.Metricool, "", "1", "2"), True)
check("a post with no network is refused",
      raises(c.schedule_post, "hi", "2026-09-12T09:00:00", []), True)

# ── an all-zero grid is "no history", not "post at midnight" ─────────
zero = {"data": [{"dayOfWeek": d, "bestTimesByHour":
                  [{"hourOfDay": h, "value": 0} for h in range(24)]} for d in range(1, 8)]}
r = M.Metricool.top_slots(zero)
check("all-zero best-times reports not-ready", r["ready"], False)
check("and explains why", "posting history" in r["why"], True)
check("and offers no fake ranking", r["slots"], [])

live = {"data": [{"dayOfWeek": 2, "bestTimesByHour":
                  [{"hourOfDay": 9, "value": 5}, {"hourOfDay": 17, "value": 9}]}]}
r = M.Metricool.top_slots(live)
check("real data ranks best-first", r["slots"][0], {"day": 2, "hour": 17, "score": 9})
check("and reports ready", r["ready"], True)

print("-" * 70)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
