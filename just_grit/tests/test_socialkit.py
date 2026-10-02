# -*- coding: utf-8 -*-
"""The Social tab's own tools: the week plan, the review card, tracked
links, and the queue that publishes at the chosen minute.

The rule the queue tests pin hardest: the loop publishes ONLY rows a person
scheduled, exactly once, and a failure is recorded on the row rather than
retried into a duplicate post. Publishing to an audience is the one thing in
this app that cannot be undone by editing a row.
"""
import json, os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-socialkit-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import socialkit as S
import webapp as W, workspaces as ws
from contextlib import closing
W.DATA = pathlib.Path(TMP)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

BIZ = {"name": "HomeRepair Tech", "city": "San Antonio, TX",
       "sells": "handyman and home repair", "buyer": "homeowners"}

# ── the plan ─────────────────────────────────────────────────────────────
p1 = S.plan_week(BIZ, "2026-W38")
p2 = S.plan_week(BIZ, "2026-W38")
p3 = S.plan_week(BIZ, "2026-W39")
check("five posts, one per pillar, in order", [p["kind"] for p in p1], S.ORDER)
check("the same week gives the same plan", [p["caption"] for p in p1], [p["caption"] for p in p2])
check("a different week gives a different plan",
      [p["caption"] for p in p1] != [p["caption"] for p in p3], True)
check("the business's facts are in the captions",
      all("HomeRepair Tech" in p["caption"] or "[" in p["caption"] for p in p1), True)
check("the city loses its state suffix", any("San Antonio" in p["caption"] and
      "San Antonio, TX" not in p["caption"] for p in p1), True)
check("every post has a slot", all(p["day"] and p["time"] for p in p1), True)
check("every post has an image prompt, most naming the place",
      (all(p["image_prompt"] for p in p1), sum("San Antonio" in p["image_prompt"] for p in p1) >= 3),
      (True, True))
check("the proof post says it needs a review when there is none",
      bool(p1[0]["needs"]), True)
p4 = S.plan_week(dict(BIZ, quote="Here by 9, done by lunch.", reviewer="Maria G."), "2026-W38")
check("  and fills itself when there is one",
      (p4[0]["needs"], "Here by 9" in p4[0]["caption"], "Maria G." in p4[0]["caption"]),
      ("", True, True))
check("fill_in flags captions with [brackets] for the owner", p1[2]["fill_in"], True)
check("unknown fields render as [field], never as a crash",
      "[name]" in S.plan_week({}, "2026-W38")[1]["caption"], True)

# ── tracked links ────────────────────────────────────────────────────────
check("utm adds source/medium", S.utm("https://x.com/book", "Instagram"),
      "https://x.com/book?utm_source=instagram&utm_medium=social")
check("existing params are kept, existing utm not clobbered",
      S.utm("https://x.com/book?ref=a&utm_source=mine", "facebook", "Fall Tune-Up!"),
      "https://x.com/book?ref=a&utm_source=mine&utm_medium=social&utm_campaign=fall-tune-up")
check("no scheme -> https", S.utm("x.com/a", "facebook").startswith("https://x.com/a?"), True)
check("empty -> empty", S.utm("", "facebook"), "")

# ── the review card ──────────────────────────────────────────────────────
check("clean_quote collapses whitespace and strips outer quotes",
      S.clean_quote('  "Great   work.\n\nCame back twice."  '), "Great work. Came back twice.")
long = ("Very good. " * 60).strip()
cq = S.clean_quote(long, max_chars=100)
check("a long review is cut at a sentence end, never mid-word", cq.endswith("."), True)
check("  and under the ceiling", len(cq) <= 100, True)
cap = S.review_caption("Here by 9, done by lunch.", "Maria G.", 5, "HomeRepair Tech", "San Antonio, TX")
check("caption: stars, reviewer, quote, thanks, city",
      ("★★★★★ from Maria G." in cap, "\"Here by 9, done by lunch.\"" in cap,
       "HomeRepair Tech" in cap, "San Antonio -" in cap), (True, True, True, True))
check("caption: no reviewer -> 'a customer'", "from a customer" in S.review_caption("x y z", "", 5, "N"), True)
try:
    from PIL import Image
    import io
    jpg = S.review_card("Called at 7am about a leak under the sink, they were here by 9.",
                        "Maria G.", 4, "HomeRepair Tech", accent="#ff7a2f")
    im = Image.open(io.BytesIO(jpg))
    check("card is a 1080x1080 JPEG", (im.format, im.size), ("JPEG", (1080, 1080)))
    px = im.convert("RGB").getpixel((100, 100))
    check("  with the accent bar in the brand color at the top-left (JPEG-close)",
          all(abs(a - b) <= 6 for a, b in zip(px, (255, 122, 47))), True)
    jpg2 = S.review_card("x" * 1200, "", 5, "N", accent="not-a-color")
    check("  a 1200-char quote and a bad color still render", len(jpg2) > 10000, True)
    jpg3 = S.review_card("Short.", "A", 5, "N", fonts_dir=pathlib.Path("/nonexistent"))
    check("  missing font files fall back rather than crash", len(jpg3) > 1000, True)
except ImportError:
    print("skip  Pillow not installed here; card rendering not exercised")

# ── preview fold ─────────────────────────────────────────────────────────
shown, hidden = S.fold("word " * 40)
check("fold cuts at a word boundary under 125", (len(shown) <= 125, shown.endswith("word")), (True, True))
check("short text does not fold", S.fold("hi there"), ("hi there", ""))

# ── the queue ────────────────────────────────────────────────────────────
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

tok = ws.CURRENT.set(ws.PRIMARY)
try:
    W.init_db()
    W.require_auth = lambda r: "local"
    W.set_setting("public_base_url", "")         # no hosting check in the way
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    # not connected -> refused, nothing stored
    try:
        W.social_queue_add(Req(), W.QueueBody(networks=["facebook"], text="hi", when=future)); got = "no error"
    except Exception as e:
        got = (getattr(e, "status_code", None), "not connected" in str(getattr(e, "detail", "")))
    check("scheduling to a network that is not connected is refused", got, (400, True))

    W.set_setting("fb_page_id", "123"); W.set_setting("fb_page_token", "tok")
    W.set_setting("ig_user_id", "999")
    try:
        W.social_queue_add(Req(), W.QueueBody(networks=["facebook"], text="hi",
                                              when=datetime.now(timezone.utc).isoformat())); got = "no error"
    except Exception as e:
        got = getattr(e, "status_code", None)
    check("a time that is basically now is refused (use the Post buttons)", got, 400)
    try:
        W.social_queue_add(Req(), W.QueueBody(networks=["facebook"], text="hi", when="2026-10-01T10:00")); got = "no error"
    except Exception as e:
        got = "timezone" in str(getattr(e, "detail", ""))
    check("a naive time is refused - the browser must send an offset", got, True)
    try:
        W.social_queue_add(Req(), W.QueueBody(networks=["instagram"], text="hi", when=future)); got = "no error"
    except Exception as e:
        got = getattr(e, "status_code", None)
    check("Instagram with no picture is refused by the network rules", got, 400)

    r = W.social_queue_add(Req(), W.QueueBody(networks=["facebook", "instagram"], text="Hello town",
                                              image_url="https://cdn.example.com/a.jpg",
                                              link="https://homerepair.tech/book", when=future))
    check("a good post is stored as scheduled with a tracked link",
          (r["ok"], r["networks"], "utm_source=facebook" in r["link"]), (True, ["facebook", "instagram"], True))
    qid = r["id"]
    lst = W.social_queue_list(Req())
    check("it shows in the queue", (lst["rows"][0]["id"], lst["rows"][0]["state"]), (qid, "scheduled"))

    # the loop does NOT touch a future row
    n = W.social_publish_due(ws.PRIMARY)
    check("the loop leaves a future row alone", n, 0)

    # make it due, fake both networks, run the loop
    with closing(W.db()) as c:
        c.execute("UPDATE social_queue SET when_at=? WHERE id=?",
                  ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"), qid))
        c.commit()
    calls = []
    W.fb.publish = lambda page, tok, **kw: (calls.append(("fb", page, kw)) or {"id": "fbpost1"})
    W.insta.stage = lambda ig, tok, **kw: (calls.append(("ig-stage", ig, kw)) or {"container_id": "c1"})
    W.insta.wait_ready = lambda cid, tok, **kw: "FINISHED"
    W.insta.release = lambda ig, tok, cid: (calls.append(("ig-release", ig, cid)) or {"id": "igmedia1"})
    n = W.social_publish_due(ws.PRIMARY)
    check("a due row is published once", n, 1)
    check("  Facebook went out PUBLISHED (the schedule was the approval)",
          [c for c in calls if c[0] == "fb"][0][2]["published"], True)
    check("  Facebook photo post carries the caption and the tracked link",
          "utm_source" in [c for c in calls if c[0] == "fb"][0][2]["message"], True)
    check("  Instagram was staged then released", [c[0] for c in calls if c[0].startswith("ig")],
          ["ig-stage", "ig-release"])
    with closing(W.db()) as c:
        row = dict(c.execute("SELECT * FROM social_queue WHERE id=?", (qid,)).fetchone())
    check("  the row is 'published' with both results", (row["state"], sorted(json.loads(row["result"]))),
          ("published", ["facebook", "instagram"]))
    n = W.social_publish_due(ws.PRIMARY)
    check("a second tick does not publish it again", (n, len(calls)), (0, 3))

    # a failure is recorded, not retried
    r2 = W.social_queue_add(Req(), W.QueueBody(networks=["facebook"], text="Second", when=future))
    with closing(W.db()) as c:
        c.execute("UPDATE social_queue SET when_at=? WHERE id=?",
                  ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"), r2["id"]))
        c.commit()
    def boom(*a, **kw):
        raise W.fb.FacebookError("token expired")
    W.fb.publish = boom
    W.social_publish_due(ws.PRIMARY)
    with closing(W.db()) as c:
        row = dict(c.execute("SELECT * FROM social_queue WHERE id=?", (r2["id"],)).fetchone())
    check("a network error marks the row failed with the reason",
          (row["state"], "token expired" in row["error"]), ("failed", True))
    n = W.social_publish_due(ws.PRIMARY)
    check("  and it is never retried on its own", n, 0)

    # cancel
    r3 = W.social_queue_add(Req(), W.QueueBody(networks=["facebook"], text="Third", when=future))
    W.social_queue_cancel(Req(), r3["id"])
    with closing(W.db()) as c:
        st = c.execute("SELECT state FROM social_queue WHERE id=?", (r3["id"],)).fetchone()[0]
    check("cancel works on a scheduled row", st, "cancelled")
    try:
        W.social_queue_cancel(Req(), qid); got = "no error"
    except Exception as e:
        got = getattr(e, "status_code", None)
    check("  and refuses on a published one", got, 400)

    # the routes for plan and card
    plan = W.social_plan(Req())
    check("/api/social/plan uses the workspace's own name and city",
          (plan["business"]["name"], len(plan["posts"])), ("The Watson Factor", 5))
    try:
        from PIL import Image
        card = W.social_review_card(Req(), W.ReviewCardBody(quote="Here by 9, done by lunch, fair price.",
                                                            reviewer="Maria G.", stars=5))
        check("/api/social/review_card stores a JPEG, a caption and a fetchable URL",
              (card["id"].endswith(".jpg"), "Maria G." in card["caption"],
               card["url"].startswith("https://justgrit.thewatsonfactor.dev/welcome/m/")),
              (True, True, True))
        _real_base = W.public_base
        W.public_base = lambda: ""
        try:
            card2 = W.social_review_card(Req(), W.ReviewCardBody(quote="Here by 9, done by lunch.",
                                                                 reviewer="A", stars=5))
        finally:
            W.public_base = _real_base
        check("  with no address set, the card is still made and says why there is no URL",
              (card2["url"], "public_base_url" in card2.get("warning", "")), ("", True))
    except ImportError:
        pass
finally:
    ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
