# -*- coding: utf-8 -*-
"""Per-network publishing rules.

Every one of these is a rejection that otherwise arrives hours later, from the
platform, in a webhook nobody is watching. Catching them at queue time - while
a person is still looking at the screen - is the entire value of this file.
"""
import sys
sys.path.insert(0, '.')
import social as S

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %-58s %s" % ("ok" if ok else "FAIL", name, "" if ok else repr(got)))

IMG = ["https://cdn.homerepair.tech/a.jpg"]

# ── Instagram: the strictest, and the one that surprises people ──────
check("IG rejects a text-only post",
      any("text alone" in p for p in S.validate("instagram", "hello")), True)
check("IG rejects PNG (JPEG only)",
      any(".png" in p for p in S.validate("instagram", "hi", ["https://x/a.png"])), True)
check("IG accepts JPEG", S.validate("instagram", "hi", IMG), [])
check("IG rejects a local file path - media must be at a public URL",
      any("public URL" in p for p in S.validate("instagram", "hi", ["/tmp/a.jpg"])), True)
check("IG caption over 2200 is caught",
      any("caption" in p for p in S.validate("instagram", "x"*2201, IMG)), True)
check("IG 2200 exactly is fine", S.validate("instagram", "x"*2200, IMG), [])
check("IG over 30 hashtags is caught",
      any("hashtags" in p for p in S.validate(
          "instagram", " ".join("#t%d" % i for i in range(31)), IMG)), True)
check("IG 30 hashtags is fine",
      S.validate("instagram", " ".join("#t%d" % i for i in range(30)), IMG), [])
check("IG aspect 4:5 boundary passes",
      S.validate("instagram", "hi", IMG, aspect=0.8), [])
check("IG portrait taller than 4:5 is caught",
      any("aspect" in p for p in S.validate("instagram", "hi", IMG, aspect=0.5)), True)

# ── YouTube: quota is the real ceiling, not a post count ─────────────
V = ["https://cdn.homerepair.tech/clip.mp4"]
check("YouTube demands a title",
      any("needs a title" in p for p in S.validate("youtube", "desc", V)), True)
check("YouTube title over 100 chars is caught",
      any("title is" in p for p in S.validate("youtube", "d", V, title="t"*101)), True)
check("YouTube with a title is fine", S.validate("youtube", "d", V, title="Roof check"), [])
room = S.daily_room("youtube", 0)
check("YouTube caps at 6 uploads/day on default quota", room["cap"], 6)
check("and explains it is quota units, not posts", "1,600" in room["note"], True)
check("after 6 uploads there is no room left", S.daily_room("youtube", 6)["left"], 0)

# ── Facebook is the permissive one ───────────────────────────────────
check("FB allows text with no media", S.validate("facebook", "just words"), [])
check("FB accepts PNG", S.validate("facebook", "hi", ["https://x/a.png"]), [])
check("FB has no daily cap here", S.daily_room("facebook", 999)["limited"], False)

# ── a post to three networks is three rule sets ──────────────────────
r = S.check_post(["instagram", "facebook", "youtube"], "hello", IMG)
check("one bad network does not pass the post", r["ok"], False)
check("IG is fine with the jpeg", r["by_network"]["instagram"]["problems"], [])
check("FB is fine", r["by_network"]["facebook"]["problems"], [])
check("YouTube blocks: a jpeg is not a video",
      any("not accept" in p for p in r["by_network"]["youtube"]["problems"]), True)
check("and it names which network blocked", r["blocked"], ["youtube"])

r = S.check_post(["instagram", "facebook"], "hello", IMG)
check("a post valid everywhere passes", r["ok"], True)

# ── the daily ceiling stops a queue, not just a post ─────────────────
r = S.check_post(["youtube"], "d", ["https://x/v.mp4"], title="T",
                 sent_today={"youtube": 6})
check("at the ceiling the post is blocked", r["ok"], False)
check("and says to wait for tomorrow",
      any("tomorrow" in p for p in r["by_network"]["youtube"]["problems"]), True)

# ── an unknown network fails loudly rather than silently passing ─────
check("unknown network is refused",
      any("not a network" in p for p in S.validate("myspace", "hi")), True)

print("-" * 72)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
