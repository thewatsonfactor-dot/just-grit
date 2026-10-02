# -*- coding: utf-8 -*-
"""The week on autopilot.

Daniel, 2026-09-24: "what to post this week needs a lot of work." He chose
full autopilot: build the week, make the pictures, schedule it, cancel what
he doesn't like. No network - pictures, captions and Meta are faked.
"""
import io, json, os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-week-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
from PIL import Image
import captions as C
import socialweek as SW
import imagegen as G
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── the content: every week of the year, both businesses ──
camps = [dict(C.clean_campaign(c), id=i + 1) for i, c in enumerate(C.STARTERS["homerepair"])]
allowed = C.RULES["homerepair"] + " " + " ".join(" ".join(str(v) for v in c.values()) for c in camps)
ban = C.banned_list(C.BANNED["homerepair"])
bad, blanks, dup_pics, kinds = 0, 0, 0, set()
for wk in range(1, 53):
    for v in range(4):
        posts = SW.plan("2026-W%02d" % wk, "homerepair", {"name": "HomeRepair Tech", "city": "San Antonio, TX"}, camps, variant=v)
        kinds.add(tuple(p["kind"] for p in posts))
        bad += sum(1 for p in posts if C.violations(p["caption"], ban, allowed))
        blanks += sum(1 for p in posts if "[" in p["caption"] or "{" in p["caption"] or not p["picture_prompt"])
        dup_pics += len(posts) - len({p["picture_prompt"] for p in posts})
check("every HomeRepair post all year passes the rules (no 'free', no made-up prices)", bad, 0)
check("  no [blanks] to fill in, and every post has a picture idea", blanks, 0)
check("  no two posts in a week share a picture", dup_pics, 0)
check("  the week is a tip, an offer, a question, a checklist and a local post",
      kinds, {("tip", "offer", "question", "checklist", "local")})
w = SW.plan("2026-W40", "watson", {"name": "The Watson Factor"}, [None])
check("Watson Factor gets its own business-owner week", ("tap" in w[0]["caption"].lower() or "google" in w[0]["caption"].lower()
      or "review" in w[0]["caption"].lower() or "website" in w[0]["caption"].lower() or "call" in w[0]["caption"].lower()), True)
check("  with no [blanks] either", any("[" in p["caption"] for p in w), False)
fall = SW.plan("2026-W40", "homerepair", {"name": "HomeRepair Tech"}, camps)
spring = SW.plan("2027-W14", "homerepair", {"name": "HomeRepair Tech"}, camps)
check("the tip follows the season", fall[0]["caption"] != spring[0]["caption"])
check("posts land Tue-Sat at set times", [(p["day"], p["time"]) for p in fall],
      [("Tue", "12:00"), ("Wed", "18:30"), ("Thu", "19:00"), ("Fri", "12:00"), ("Sat", "09:30")])
check("the offer post is built from a real campaign", fall[1]["campaign_id"] in {c["id"] for c in camps})
check("  and a different week can pick a different angle",
      len({SW.plan("2026-W%02d" % k, "homerepair", {"name": "H"}, camps)[1]["caption"] for k in range(30, 40)}) > 1)
check("no 'behind the scenes' or review posts on autopilot - an AI 'crew' would be a lie",
      any(p["kind"] in ("behind", "proof") for p in fall), False)

# ── the engine ──
W.DATA = pathlib.Path(TMP)
for s in ("homerepair", "watson"):
    t = ws.CURRENT.set(s); W.init_db(); ws.CURRENT.reset(t)
tok = ws.CURRENT.set("homerepair")
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

check("posts are signed with the business, not the person",
      (W.set_setting("sender_name", "Daniel Watson") or W.social_business()["name"]), "HomeRepair Tech")

def jpeg():
    b = io.BytesIO(); Image.new("RGB", (1024, 1024), (70, 110, 60)).save(b, "JPEG"); return b.getvalue()
made = []
G.generate = lambda cfg, prompt, previous_id=None, **k: made.append(prompt) or {
    "bytes": jpeg(), "mime": "image/jpeg", "response_id": "", "provider": "meta", "model": "m", "usage": {}}
W.set_setting("imagegen_provider", "meta"); W.set_setting("imagegen_api_key", "sk-FAKE-1234567890")
W.hosting_problem_now = lambda: ""
W.public_base = lambda: "https://homerepair.example"
W.social_connected = lambda: {"facebook": True, "instagram": True}
W.caption_cfg = lambda: None                        # no AI writer: the library captions go out

nxt = SW.week_of((datetime.now(W.workspace_tz()) + timedelta(days=7)).date())
r = W.build_week(nxt)
check("building next week schedules all five", (r["scheduled"], r["saved"], len(made)), (5, 0, 5))
rows = W._auto_rows([nxt])
check("  each with a picture, words, both networks and a time", all(x["thumb"] and x["text"] and
      x["networks"] == ["facebook", "instagram"] and x["state"] == "scheduled" for x in rows))
check("  AI pictures get the 'Picture created with AI.' line", all(x["text"].endswith("Picture created with AI.") for x in rows))
t0 = datetime.fromisoformat(rows[0]["when_at"])
local = t0.astimezone(W.workspace_tz())
check("  times are Central time (Tue 12:00 local)", (local.strftime("%a %H:%M")), "Tue 12:00")
offer = [x for x in rows if x["kind"] == "offer"][0]
check("  the offer carries its campaign's tracked link", "utm_source=facebook" in offer["link"] and "homerepair.tech/plans" in offer["link"])
check("  the question and local posts don't push a link", [x["link"] for x in rows if x["kind"] in ("question", "local")], ["", ""])
check("they show up in the schedule list too", len(W.social_queue_list(Req())["rows"]), 5)
made.clear()
r = W.build_week(nxt)
check("building the same week again doesn't double up", (r["scheduled"], r["skipped"], len(made)), (0, 5, 0))
check("the week is remembered as built", nxt in W.auto_built_weeks())

# swap one: same slot, a different post
tip = [x for x in rows if x["kind"] == "tip"][0]
W._auto_run = lambda slug, week, only_slot="", variant=0: W.build_week(week, only_slot=only_slot, variant=variant) and True
W.social_autopilot_swap(Req(), tip["id"])
rows2 = W._auto_rows([nxt])
new_tip = [x for x in rows2 if x["kind"] == "tip"][0]
check("swap replaces the post in the same slot with a different one",
      (len(rows2), new_tip["id"] != tip["id"], new_tip["text"] != tip["text"]), (5, True, True))

# edit: back into the post box as a saved post
q = [x for x in rows2 if x["kind"] == "question"][0]
W.social_queue_to_draft(Req(), q["id"])
check("edit takes it off the schedule and into Saved posts",
      (q["id"] in [d["id"] for d in W.social_drafts(Req())["rows"]],
       [x["state"] for x in W._auto_rows([nxt]) if x["id"] == q["id"]]), (True, ["draft"]))

# not connected -> saved, not scheduled, with the reason
W.social_connected = lambda: {"facebook": False, "instagram": False}
after = SW.week_of((datetime.now(W.workspace_tz()) + timedelta(days=14)).date())
r = W.build_week(after)
check("not connected: the week is built into Saved posts, not scheduled", (r["scheduled"], r["saved"]), (0, 5))
check("  and it says why", "aren't connected" in r["problems"][0])

# pictures fail -> Facebook only, and it says so
W.social_connected = lambda: {"facebook": True, "instagram": True}
def boom(*a, **k): raise G.ImageGenError("Meta says billing isn't set up")
G.generate = boom
wk3 = SW.week_of((datetime.now(W.workspace_tz()) + timedelta(days=21)).date())
r = W.build_week(wk3)
rows3 = W._auto_rows([wk3])
check("no picture: still scheduled for Facebook, skipped for Instagram",
      (r["scheduled"], {tuple(x["networks"]) for x in rows3}), (5, {("facebook",)}))
check("  no AI line without an AI picture", any("Picture created with AI." in x["text"] for x in rows3), False)
check("  and the reason is reported", any("billing" in p for p in r["problems"]))

# the AI writer, when there is one, writes from the post's idea
seen = []
W.caption_cfg = lambda: {"provider": "gemini", "api_key": "k", "base_url": "https://x", "model": "m", "refine": False}
C.write = lambda biz, camp, **k: seen.append(k.get("brief")) or {"source": "ai", "text": "AI words #HomeRepairTech"}
G.generate = lambda cfg, prompt, previous_id=None, **k: {"bytes": jpeg(), "mime": "image/jpeg", "response_id": "",
                                                        "provider": "meta", "model": "m", "usage": {}}
wk4 = SW.week_of((datetime.now(W.workspace_tz()) + timedelta(days=28)).date())
W.build_week(wk4)
check("with an AI writer, each post is written from its planned idea",
      (len(seen), all(seen), W._auto_rows([wk4])[0]["text"].startswith("AI words")), (5, True, True))

# on/off and when weeks are due
W.auto_built_weeks = lambda: []
fri = datetime(2026, 9, 25, 10, 0, tzinfo=W.workspace_tz())
tue = datetime(2026, 9, 22, 10, 0, tzinfo=W.workspace_tz())
check("autopilot builds this week any day, and next week from Friday",
      (W.auto_due_weeks(tue), W.auto_due_weeks(fri)), (["2026-W39"], ["2026-W39", "2026-W40"]))
started = []
W._auto_run = lambda slug, week, **k: started.append(week) or True
r = W.social_autopilot_set(Req(), W.AutoBody(on=True))
check("switching it on starts building right away", (r["on"], r["building"], len(started)), (True, True, 1))
r = W.social_autopilot_set(Req(), W.AutoBody(on=False))
check("and it can be switched off", W.social_autopilot(Req())["on"], False)
st = W.social_autopilot(Req())
check("the panel gets the week's posts and what's ready", (len(st["slots"]), st["ready"]["pictures"], st["ready"]["facebook"]),
      (5, True, True))

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
