# -*- coding: utf-8 -*-
"""Reels and Shorts from the schedule, in place of Metricool.

Daniel, 2026-11-17: "I created just grit so i wouldnt have to use metricool."
A scheduled video goes to Instagram as a Reel and to YouTube as a Short.
No network: Instagram's calls and YouTube's HTTP door are both faked.
"""
import json, os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-reels-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws, youtube as yt, media as M

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
t = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
W.is_owner = lambda r: True
class Req:
    headers = {}; cookies = {}; base_url = "http://127.0.0.1:8000/"
    class client: host = "127.0.0.1"

# a tiny MP4 (only the ftyp box matters to the sniffer)
mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000
vid = M.save(mp4, W.DATA)
check("an MP4 is kept as video", vid["kind"], "video/mp4")
BASE = "https://grit.example.com"
W.public_base = lambda: BASE
vurl = M.public_url(BASE, vid["id"])
check("its public link resolves back to the file", W.own_media_path(vurl) is not None)
check("a link to somebody else's site does not", W.own_media_path("https://cdn.other.com/x.mp4"), None)

# ── size limits: video gets room, pictures keep the old ceiling ──
check("video ceiling is bigger than the picture one", M.VIDEO_MAX_BYTES > M.MAX_BYTES)
try:
    M.save(b"\xff\xd8\xff" + b"\x00" * (M.MAX_BYTES + 1), W.DATA); got = ""
except M.MediaError as e:
    got = str(e)
check("a picture over 60 MB is still refused", "pictures" in got)

# ── YouTube's rules for a Short ──
check("#Shorts is added to the title once", yt.shorts_text("Roof leak fixed", "")[0], "Roof leak fixed #Shorts")
check("  and not twice", yt.shorts_text("Roof #shorts", "")[0], "Roof #shorts")
long_t = "x" * 98
check("a title with no room puts #Shorts in the description",
      yt.shorts_text(long_t, "desc"), (long_t, "desc\n\n#Shorts"))

# ── what the scheduler refuses, in words ──
W.social_connected = lambda: {"facebook": True, "instagram": True, "youtube": True}
W.media_check = lambda req: {"ok": True}
when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
def refused(**kw):
    body = dict(networks=["instagram", "youtube"], text="Twisted Fate, part 1", when=when)
    body.update(kw)
    try:
        W.social_queue_add(Req(), W.QueueBody(**body)); return ""
    except W.HTTPException as e:
        return e.detail
check("a Short with no title is refused", "YouTube needs a title" in refused(video_url=vurl))
check("YouTube with no video is refused", "YouTube needs a video" in refused(title="T"))
check("a video from another site can't go to YouTube",
      "uploaded to Just Grit first" in refused(video_url="https://cdn.other.com/x.mp4", title="T"))
check("a picture and a video together is refused",
      "not both" in refused(video_url=vurl, image_url=BASE + "/welcome/m/a.jpg", title="T"))
check("Facebook video says it isn't wired yet",
      "Facebook video" in refused(networks=["facebook"], video_url=vurl, title="T"))
W.social_connected = lambda: {"facebook": True, "instagram": True, "youtube": False}
check("YouTube not connected is refused", "Youtube is not connected" in refused(video_url=vurl, title="T"))
W.social_connected = lambda: {"facebook": True, "instagram": True, "youtube": True}

q = W.social_queue_add(Req(), W.QueueBody(networks=["instagram", "youtube"], text="Twisted Fate, part 1",
                                          video_url=vurl, title="Twisted Fate part 1", when=when))
row = [r for r in W.social_queue_list(Req())["rows"] if r["id"] == q["id"]][0]
check("the scheduled row keeps the video and the title",
      (row["video_url"], row["title"], row["networks"]), (vurl, "Twisted Fate part 1", ["instagram", "youtube"]))

# ── publishing: fake Instagram, fake YouTube ──
store = {"ig_user_id": "1784", "ig_token": "tok", "yt_client_id": "cid.apps",
         "yt_client_secret": "sec", "yt_refresh_token": "rt", "yt_privacy": "unlisted"}
for k, v in store.items():
    W.set_setting(k, v)
staged, waited, released = [], [], []
W.insta.stage = lambda ig, tok, **kw: staged.append(kw) or {"container_id": "c1"}
W.insta.wait_ready = lambda cid, tok, **kw: waited.append(kw) or "FINISHED"
W.insta.release = lambda ig, tok, cid: released.append(cid) or {"id": "m1"}
slept = []
W._social_sleep = lambda s: slept.append(s)
calls = []
def fake_http(method, url, headers=None, body=None, timeout=60):
    calls.append((method, url, headers or {}, body))
    if url == yt.TOKEN_URL:
        return 200, {}, json.dumps({"access_token": "at"}).encode()
    if url.startswith(yt.UPLOAD):
        return 200, {"Location": "https://www.googleapis.com/upload/x?upload_id=9"}, b""
    if method == "PUT":
        return 200, {}, json.dumps({"id": "abc123", "status": {"privacyStatus": "unlisted"}}).encode()
    return 404, {}, b"{}"
yt.http = fake_http

with W.closing(W.db()) as c:                              # make it due now
    c.execute("UPDATE social_queue SET when_at=? WHERE id=?",
              ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"), q["id"]))
    c.commit()
n = W.social_publish_due("homerepair")
with W.closing(W.db()) as c:
    r = dict(c.execute("SELECT * FROM social_queue WHERE id=?", (q["id"],)).fetchone())
res = json.loads(r["result"] or "{}")
check("one due row was published", (n, r["state"]), (1, "published"))
check("Instagram got it as a Reel", (staged[0].get("video_url"), staged[0].get("media_type")), (vurl, "REELS"))
check("  and was given time to process the video", waited[0].get("tries", 0) * waited[0].get("delay", 0) >= 300)
check("  then published", (released, res["instagram"]["reel"]), (["c1"], True))
up = [x for x in calls if x[1].startswith(yt.UPLOAD)]
meta = json.loads(up[0][3].decode())
check("YouTube got the file's bytes", [x[3] for x in calls if x[0] == "PUT"][0], mp4)
check("  with #Shorts in the title", meta["snippet"]["title"], "Twisted Fate part 1 #Shorts")
check("  the post text as description", meta["snippet"]["description"], "Twisted Fate, part 1")
check("  at the privacy chosen in Setup", meta["status"]["privacyStatus"], "unlisted")
check("  and the Short's link is on the row", res["youtube"]["video"]["url"], "https://youtube.com/shorts/abc123")

# a second tick must not post it again
calls.clear(); staged.clear()
check("a published row is never sent twice", (W.social_publish_due("homerepair"), calls, staged), (0, [], []))

# Google locking an unaudited project's upload to private is reported, not hidden
yt.http = lambda m, u, h=None, b=None, timeout=60: (
    (200, {}, b'{"access_token":"at"}') if u == yt.TOKEN_URL else
    (200, {"location": "https://www.googleapis.com/u?id=1"}, b"") if u.startswith(yt.UPLOAD) else
    (200, {}, b'{"id":"p1","status":{"privacyStatus":"private"}}'))
out = yt.upload("at", W.own_media_path(vurl), "Clip", privacy="public")
check("a private lock from Google comes back as a warning", "API audit" in out.get("warning", ""))

# a revoked key reads as something a person can act on
check("an expired Google key says to reconnect",
      "Connect YouTube again" in yt._explain(400, '{"error":"invalid_grant","error_description":"Token has been expired or revoked."}'))

# ── connecting ──
W.set_setting("yt_refresh_token", "")
st = W.youtube_status(Req())
check("status shows not connected but has the client", (st["connected"], st["has_client"]), (False, True))
check("  and the address to register with Google", st["redirect_uri"], BASE + "/api/youtube/callback")
resp = W.youtube_connect(Req())
loc = resp.headers["location"]
state = W.setting("yt_oauth_state")
check("Connect sends the browser to Google with a one-time state",
      loc.startswith(yt.AUTH_URL) and ("state=" + state) in loc and len(state) > 20)
check("  asking only to upload and read the channel name", "youtube.upload" in loc and "force-ssl" not in loc)
bad = W.youtube_callback(Req(), code="x", state="forged")
check("a callback with the wrong state connects nothing",
      (bad.status_code, W.setting("yt_refresh_token")), (400, ""))
check("  and the state is used up", W.setting("yt_oauth_state"), "")
W.youtube_connect(Req()); state = W.setting("yt_oauth_state")
def http_ok(method, url, headers=None, body=None, timeout=60):
    if url == yt.TOKEN_URL:
        return 200, {}, b'{"access_token":"at","refresh_token":"new-rt"}'
    if "/channels?" in url:
        return 200, {}, json.dumps({"items": [{"id": "UC1", "snippet": {"title": "Southern Grit <TV>"}}]}).encode()
    return 404, {}, b"{}"
yt.http = http_ok
good = W.youtube_callback(Req(), code="real", state=state)
check("the right state stores the key and the channel",
      (good.status_code, W.setting("yt_refresh_token"), W.setting("yt_channel_title")),
      (200, "new-rt", "Southern Grit <TV>"))
check("  and the page escapes the channel name", b"Southern Grit &lt;TV&gt;" in good.body)
check("YouTube now reads as ready", W.youtube_ready(), True)
check("the refresh token never shows in a settings export", bool(W.SECRET_KEY_RX.search("yt_refresh_token")))
W.youtube_disconnect(Req())
check("Disconnect forgets the key", (W.youtube_ready(), W.setting("yt_channel_title")), (False, ""))
try:
    W.youtube_privacy(Req(), W.YtPrivacyBody(privacy="everyone")); got = ""
except W.HTTPException as e:
    got = e.detail
check("privacy only takes public, unlisted or private", "unlisted" in got)

ws.CURRENT.reset(t)
print("FAILS:", fails)
sys.exit(1 if fails else 0)
