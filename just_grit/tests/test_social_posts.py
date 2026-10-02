# -*- coding: utf-8 -*-
"""The planner list: every post with what it did, stats pulled from Meta
at most hourly and kept on the row. No network: fb/insta stats are faked."""
import os, pathlib, sys, tempfile, json
TMP = tempfile.mkdtemp(prefix="jg-sp-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws, facebook as fb, instagram as insta

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("fb_page_token", "t"); W.set_setting("ig_token", "t")
class Req:
    headers = {}; cookies = {}; query_params = {}
    class client: host = "127.0.0.1"
CALLS = []
fb.post_stats = lambda pid, t: (CALLS.append(("fb", pid)) or {"likes": 4, "comments": 1, "shares": 2, "reach": 120})
insta.media_stats = lambda mid, t: (CALLS.append(("ig", mid)) or {"likes": 9, "comments": 0, "shares": None, "reach": 300})
with W.closing(W.db()) as c:
    c.execute("INSERT INTO social_queue (networks, text, image_url, when_at, state, result, created_at, published_at) VALUES (?,?,?,?,?,?,?,?)",
              ('["facebook","instagram"]', "Fall checklist: gutters first.", "https://x/1.jpg", W.now(), "published",
               json.dumps({"facebook": {"ok": True, "post": {"id": "111_222"}}, "instagram": {"ok": True, "media": {"id": "999"}}}), W.now(), W.now()))
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, created_at) VALUES (?,?,?,?,?)",
              ('["facebook"]', "Coming Saturday.", W.now(), "scheduled", W.now()))
    c.execute("INSERT INTO social_queue (networks, text, when_at, state, error, created_at) VALUES (?,?,?,?,?,?)",
              ('["instagram"]', "Broken one.", W.now(), "failed", "instagram: InstagramError: Instagram needs a picture.", W.now()))
    c.commit()

d = W.social_posts(Req())
check("three posts listed, newest first", d["total"], 3)
pub = next(r for r in d["rows"] if r["state"] == "published")
check("published post carries stats from both networks", (pub["totals"]["reach"], pub["totals"]["likes"], pub["totals"]["comments"], pub["totals"]["shares"]), (420, 13, 1, 2))
check("  Meta asked once per network", sorted(CALLS), [("fb", "111_222"), ("ig", "999")])
check("  the summary adds up", d["summary"]["likes"], 13)
check("  counts by state", (d["counts"]["published"], d["counts"]["scheduled"], d["counts"]["failed"]), (1, 1, 1))
CALLS.clear(); W.social_posts(Req())
check("a second listing inside the hour asks Meta nothing", CALLS, [])
W.social_posts(Req(), refresh=1)
check("  refresh=1 asks again", len(CALLS), 2)
check("state filter", W.social_posts(Req(), state="failed")["rows"][0]["error"].startswith("instagram"))
check("search in captions", W.social_posts(Req(), q="saturday")["total"], 1)

# a dead token shows up in the Facebook status, and a failed post can be retried
W.set_setting("fb_page_id", "p"); W.set_setting("fb_page_token", "t")
with W.closing(W.db()) as c:
    c.execute("UPDATE social_queue SET error='facebook: FacebookError: The access token is invalid or expired.' WHERE state='failed'"); c.commit()
fs = W.facebook_status(Req())
check("a publish that failed on the token marks Facebook expired", (fs["connected"], fs["expired"], "Reconnect" in fs["broken"]), (True, True, True))
fid = W.social_posts(Req(), state="failed")["rows"][0]["id"]
W.social_post_retry(Req(), fid)
check("retry puts it back in the queue with the error cleared", W.social_posts(Req(), state="scheduled")["total"], 2)
try:
    W.social_post_retry(Req(), fid); check("retry twice refused", False)
except W.HTTPException as e:
    check("only a failed post can be retried", "Only a failed" in e.detail)
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
