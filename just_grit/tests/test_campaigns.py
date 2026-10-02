# -*- coding: utf-8 -*-
"""The readiness gate, and the numbers it refuses to invent.

The gate exists because the old campaign tab would happily plan a $4,000/month
campaign for a business with no contact form and no pixel, and print a ROAS to
one decimal place while doing it. Refusing is the feature.
"""
import sys, json
sys.path.insert(0, '.')
import sqlite3
import campaigns as camp

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += (not ok)
    print("%-4s %-52s %r" % ("ok" if ok else "FAIL", name, got if ok else (got, "want", want)))

def P(gate=None, **kw):
    d = {"scan_state": "done", "company": "X", "city": "San Antonio",
         "vertical": "contractor", "findings": "[]"}
    d.update(kw)
    d["stack"] = json.dumps(gate) if gate is not None else ""
    return d

OK_GATE = {"keys": [], "analytics": True, "retargeting": True, "scanned_at": "now"}

# ── the gate must not pass on silence ────────────────────────────────
check("unscanned site is not assessable",
      camp.readiness(P(scan_state="", gate=None))["assessable"], False)
check("failed scan is not assessable",
      camp.readiness(P(scan_state="failed", gate=None))["assessable"], False)
check("old scan with no stored gate is not 'ready'",
      camp.readiness(P(gate=None))["ready"], False)
check("old scan is explicitly not assessable, not just unready",
      camp.readiness(P(gate=None))["assessable"], False)

# ── blockers ─────────────────────────────────────────────────────────
def blocks(gate, **kw):
    return {b["key"] for b in camp.readiness(P(gate, **kw))["blockers"]}

check("no form + no tap-to-call blocks",
      "no_conversion_path" in blocks({**OK_GATE, "keys": ["no_form", "no_tap_to_call"]}), True)
check("no form + no phone at all blocks",
      "no_conversion_path" in blocks({**OK_GATE, "keys": ["no_form", "no_phone"]}), True)
check("no form alone does NOT block (they can still call)",
      "no_conversion_path" in blocks({**OK_GATE, "keys": ["no_form"]}), False)
check("no analytics AND no pixel blocks",
      "no_measurement" in blocks({**OK_GATE, "analytics": False, "retargeting": False}), True)
check("analytics present, no pixel: not a blocker",
      "no_measurement" in blocks({**OK_GATE, "analytics": True, "retargeting": False}), False)
check("http-only blocks", "no_https" in blocks({**OK_GATE, "keys": ["no_https"]}), True)
check("clean site is ready", camp.readiness(P(OK_GATE))["ready"], True)

# ── a low rating warns but does not block ────────────────────────────
r = camp.readiness(P(OK_GATE, rating=3.4))
check("rating 3.4 warns", any(w["key"] == "low_rating" for w in r["warnings"]), True)
check("rating 3.4 does not block", r["ready"], True)

# ── economics refuses what it cannot support ─────────────────────────
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE deals (state TEXT, amount REAL, mrr REAL)")
check("no deals -> no ticket", camp.economics(c)["avg_ticket"], None)

c.executemany("INSERT INTO deals VALUES (?,?,?)", [("won", 9000, 0), ("won", 8000, 500)])
e = camp.economics(c)
check("two wins -> ticket is reported", e["avg_ticket"], 8500)
check("two wins, zero losses -> close_rate stays None", e["close_rate"], None)
check("and it says why", any("lost deals" in n for n in e["notes"]), True)

c.executemany("INSERT INTO deals VALUES (?,?,?)", [("lost", 0, 0)] * 3)
check("5 closed is still too few for a rate", camp.economics(c)["close_rate"], None)
c.executemany("INSERT INTO deals VALUES (?,?,?)", [("lost", 0, 0)] * 5)
check("10 closed -> a rate appears", camp.economics(c)["close_rate"], 0.2)

# ── the brief must not put OUR products in THEIR ad copy ─────────────
b = camp.brief(P(OK_GATE, offers="Ordering App, AI Vision"), camp.economics(c))
joined = " ".join(b["proof_points"])
check("our product names stay out of their proof points",
      ("Ordering App" not in joined and "AI Vision" not in joined), True)
check("and it says the service list is missing",
      "isn't on file" in joined, True)

print("-" * 70)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
