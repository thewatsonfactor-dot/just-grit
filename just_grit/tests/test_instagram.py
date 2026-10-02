# -*- coding: utf-8 -*-
"""Instagram's two-step publish.

The point of these tests is the boundary between staging and releasing.
Instagram has no unpublished state - media_publish IS the publish - so the
irreversible call has to be its own explicit step, and staging must never
reach an audience by itself.
"""
import sys
sys.path.insert(0, '.')
import instagram as IG

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %-62s %s" % ("ok" if ok else "FAIL", name, "" if ok else repr(got)))
def raises(fn, *a, **k):
    try: fn(*a, **k); return False
    except IG.InstagramError: return True

calls = []
IG._req = lambda m, p, params: (calls.append((m, p, dict(params)))
                                or {"id": "CONTAINER1", "status_code": "FINISHED"})

# ── refuse before spending a round trip ──────────────────────────────
check("no ig id is refused", raises(IG.stage, "", "t", "https://x/a.jpg"), True)
check("no token is refused", raises(IG.stage, "1", "", "https://x/a.jpg"), True)
check("text-only is refused (IG has no such post)", raises(IG.stage, "1", "t"), True)
check("a local file path is refused",
      raises(IG.stage, "1", "t", "/tmp/a.jpg"), True)

# ── staging hits /media and nothing else ─────────────────────────────
calls.clear()
out = IG.stage("IGID", "TOK", image_url="https://x/a.jpg", caption="hi")
check("stage posts to /media", calls[0][1], "IGID/media")
check("stage makes exactly one call", len(calls), 1)
check("NOTHING is published by staging",
      any("media_publish" in c[1] for c in calls), False)
check("stage returns a container id", out["container_id"], "CONTAINER1")
check("image goes in image_url", calls[0][2]["image_url"], "https://x/a.jpg")

calls.clear()
IG.stage("IGID", "TOK", video_url="https://x/v.mp4")
check("video defaults to REELS", calls[0][2]["media_type"], "REELS")
check("and uses video_url", calls[0][2]["video_url"], "https://x/v.mp4")

# ── releasing is the separate, irreversible call ─────────────────────
calls.clear()
IG.release("IGID", "TOK", "CONTAINER1")
check("release posts to /media_publish", calls[0][1], "IGID/media_publish")
check("with creation_id, not id", calls[0][2]["creation_id"], "CONTAINER1")
check("releasing nothing is refused", raises(IG.release, "IGID", "TOK", ""), True)

# ── polling: the step people skip, which only breaks on video ────────
seq = []
def status_seq(m, p, params):
    return {"status_code": seq.pop(0)} if seq else {"status_code": "FINISHED"}
IG._req = status_seq
slept = []
seq[:] = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
check("wait_ready polls until FINISHED",
      IG.wait_ready("C", "T", tries=5, delay=0, sleep=slept.append), "FINISHED")
check("and it actually waited between polls", len(slept), 2)

seq[:] = ["ERROR"]
check("ERROR aborts instead of publishing",
      raises(IG.wait_ready, "C", "T", tries=3, delay=0, sleep=lambda s: None), True)
seq[:] = ["EXPIRED"]
check("EXPIRED aborts too",
      raises(IG.wait_ready, "C", "T", tries=3, delay=0, sleep=lambda s: None), True)
seq[:] = ["IN_PROGRESS"] * 9
check("still processing after the last try raises, not publishes",
      raises(IG.wait_ready, "C", "T", tries=3, delay=0, sleep=lambda s: None), True)

# ── errors translated into the actual cause ──────────────────────────
check("expired token names the Page token",
      "Page" in IG._explain(400, {"code": 190, "message": "x"}, ""), True)
check("permission error names Business/Creator",
      "Business or Creator" in IG._explain(400, {"code": 200, "message": "x"}, ""), True)
check("aspect error names the 4:5-1.91 window",
      "4:5" in IG._explain(400, {"code": 36003, "message": "x"}, ""), True)
check("format error says JPEG",
      "JPEG" in IG._explain(400, {"code": 36001, "message": "x"}, ""), True)
check("fetch failure points at Access/localhost",
      "anonymous" in IG._explain(400, {"code": 9007, "message": "x"}, ""), True)

print("-" * 76)
print("FAILURES: %d" % fails)
sys.exit(1 if fails else 0)
