# -*- coding: utf-8 -*-
"""The October 1 pricing switch.

Daniel, 2026-09-25, pasting the new homerepair.tech catalog: "I'm moving
to the new pricing structure as of October 1." Until then the September
ladder ($29 / $99 / $199) stands; from the 1st it's Basic $29, Essential
$59 (four seasonal visits), Pro $179, +$49 setup waived on annual, a $49
audit and a $79 HomePassport report. Campaigns he edited are his and stay.
No network.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-catalog-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import captions as C
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── the two catalogs ──
sep, octo = C.starters_for("homerepair", "2026-09-30"), C.starters_for("homerepair", "2026-10-01")
check("September: four campaigns at $29 / $99 / $199", [s["offer"] for s in sep],
      ["Protect Essential - $99 a month", "Get the fall list handled on your monthly visit",
       "Protect Basic - $29 a month", "Protect Pro - $199 a month per property"])
check("October: six, at $29 / $59 / $179 plus the $49 audit and $79 report", [s["offer"] for s in octo],
      ["Protect Essential - $59 a month", "Get the fall list handled on your fall care visit",
       "Protect Basic - $29 a month", "Protect Pro - $179 a month per property",
       "A $49 Home Health & Safety Audit", "A HomePassport Property Report - $79, one time"])
check("  the same four campaigns carry over (one renamed), so an untouched row is swapped in place",
      [C.RENAMED.get(s["name"], s["name"]) for s in sep], [s["name"] for s in octo][:4])
text = " ".join(str(v) for s in octo for v in s.values())
for fact in ("$588", "$276", "$1,788", "$49 setup fee, waived", "$75/hr", "$85/hr", "$65/hr",
             "$50 repair credit", "$25 repair credit", "$150 repair credit", "spring AC check", "two gutter cleans"):
    check("  October says '%s'" % fact, fact in text)
check("  and never 'free'", any(C.violations(str(s), ["free"], str(s)) for s in octo), False)
check("  every October price is on the rules list (so the AI writer may use it)",
      C.violations(text, [], C.rules_for("homerepair", "2026-10-01")), [])
check("the rules text follows the date too", ("$99/mo" in C.rules_for("homerepair", "2026-09-30"),
      "$59/mo" in C.rules_for("homerepair", "2026-10-01")), (True, True))
check("Watson Factor has one catalog and no switch",
      (C.starters_for("watson", "2026-09-01") == C.STARTERS["watson"], "watson" in C.STARTERS_2026_09), (True, False))

# ── the switch, in a workspace that was seeded in September ──
W.DATA = pathlib.Path(TMP)
for s in ("homerepair", "watson"):
    t = ws.CURRENT.set(s); W.init_db(); ws.CURRENT.reset(t)
tok = ws.CURRENT.set("homerepair")
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

W.local_today = lambda: "2026-09-25"
rows = W.social_campaigns(Req())["rows"]
check("seeded today: the September four", ([r["name"] for r in rows][:1], len(rows)), (["Under an hour? It's included"], 4))
check("  the rules quote September prices", "$99/mo" in W.caption_rules()[0])
check("  and the workspace knows which catalog it is on", W.setting("catalog_version"), "2026-09")
# Daniel edits the Pro campaign - his words now
pro = [r for r in rows if r["name"].startswith("Rental owners")][0]
W.social_campaign_save(Req(), W.CampaignBody(**dict({k: pro[k] for k in C.FIELDS}, id=pro["id"],
                                                     details="Ask us about make-ready turns. We quote first.")))
# a scheduled October post that quotes the old price, and one that doesn't
with W.closing(W.db()) as c:
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, created_at) VALUES ('[\"facebook\"]', ?, ?, 'scheduled', ?)",
              ("Protect Essential is $99 a month with up to 60 minutes.", "2026-10-07T18:30:00-05:00", W.now()))
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, created_at) VALUES ('[\"facebook\"]', ?, ?, 'scheduled', ?)",
              ("Unhook the hoses before the first freeze.", "2026-10-06T12:00:00-05:00", W.now()))
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, created_at) VALUES ('[\"facebook\"]', ?, ?, 'scheduled', ?)",
              ("Last call for September: $99 a month.", "2026-09-30T18:30:00-05:00", W.now()))
    c.commit()

W.local_today = lambda: "2026-09-30"
W.social_campaigns(Req())
check("Sep 30: nothing moves yet", (W.setting("catalog_version"), "$99/mo" in W.caption_rules()[0]), ("2026-09", True))

W.local_today = lambda: "2026-10-01"
rows = W.social_campaigns(Req())["rows"]
by = {r["name"]: r for r in rows}
check("Oct 1: untouched starters are swapped for the October versions",
      (by["Four seasons, one plan"]["offer"], by["Eyes on your home - $29/mo"]["details"][:20]),
      ("Protect Essential - $59 a month", "Your 0-100 Home Inte"))
check("  the one Daniel edited keeps his words", by["Rental owners & property managers"]["details"],
      "Ask us about make-ready turns. We quote first.")
check("  the two one-time products are added", ("$49 Home Health & Safety Audit" in by, "HomePassport report - $79" in by), (True, True))
check("  six campaigns, nothing duplicated", len(rows), 6)
check("  the rules now quote October prices", ("$59/mo" in W.caption_rules()[0], "$99/mo" in W.caption_rules()[0]), (True, False))
with W.closing(W.db()) as c:
    q = {r["text"][:12]: (r["state"], r["error"][:14]) for r in c.execute("SELECT text, state, error FROM social_queue")}
check("  the October post that quoted $99 is pulled back into Saved posts, and says why",
      q["Protect Esse"], ("draft", "Pulled back on"))
check("  the October post with no price stays scheduled", q["Unhook the h"][0], "scheduled")
check("  a September post is left alone", q["Last call fo"][0], "scheduled")
check("  the workspace remembers", W.setting("catalog_version"), "2026-10")

# it runs once: re-edit a swapped campaign, call again, still his
W.social_campaign_save(Req(), W.CampaignBody(**dict({k: by["Four seasons, one plan"][k] for k in C.FIELDS},
                                                     id=by["Four seasons, one plan"]["id"], offer="My own offer line")))
W.local_today = lambda: "2026-10-02"
rows2 = W.social_campaigns(Req())["rows"]
check("it doesn't run twice", ([r for r in rows2 if r["name"] == "Four seasons, one plan"][0]["offer"], len(rows2)),
      ("My own offer line", 6))

# rules Daniel rewrote are his
W.set_setting("caption_rules", "My rules.")
W.set_setting("catalog_version", "2026-09")
W.social_campaigns(Req())
check("rules Daniel rewrote survive the switch", W.caption_rules()[0], "My rules.")

# a list he emptied on purpose stays empty
for r in W.social_campaigns(Req())["rows"]:
    W.social_campaign_remove(Req(), r["id"])
W.set_setting("catalog_version", "2026-09")
check("an emptied list isn't refilled by the switch", len(W.social_campaigns(Req())["rows"]), 0)
ws.CURRENT.reset(tok)

# ── a workspace first seeded after Oct 1 goes straight to the new catalog ──
t = ws.CURRENT.set("watson")
W.local_today = lambda: "2026-10-05"
rows = W.social_campaigns(Req())["rows"]
check("Watson Factor: its own starters, untouched by all this", len(rows), len(C.STARTERS["watson"]))
check("  and it is marked as on the current catalog", W.setting("catalog_version"), "2026-10")
ws.CURRENT.reset(t)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
