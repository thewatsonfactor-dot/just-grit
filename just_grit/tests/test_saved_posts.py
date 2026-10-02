# -*- coding: utf-8 -*-
"""Save this post, start the next one.

Daniel, 2026-09-24: "after the image is created need to be able to save it
and go the next post." Also: a separate key for writing captions, and the
media check that called a working host blocked. No network.
"""
import io, json, os, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone
TMP = tempfile.mkdtemp(prefix="jg-drafts-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
from PIL import Image
import media as M
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
for s in ("homerepair", "watson"):
    t = ws.CURRENT.set(s); W.init_db(); ws.CURRENT.reset(t)
tok = ws.CURRENT.set("homerepair")
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

camp = W.social_campaigns(Req())["rows"][0]
b = io.BytesIO(); Image.new("RGB", (1024, 1024), (90, 120, 80)).save(b, "JPEG")
m = W.mstore.save(b.getvalue(), W.DATA)
with W.closing(W.db()) as c:
    c.execute("INSERT INTO imagegen_log (provider, model, prompt, media_id, ok, at) VALUES ('meta','m',?,?,1,?)",
              ("A friendly technician kneeling beside an outdoor AC unit", m["id"], W.now()))
    c.commit()
url = "https://homerepair.thewatsonfactor.dev/welcome/m/" + m["id"]

# ── save, then the next one ──
try:
    W.social_draft_save(Req(), W.PostDraftBody()); got = ""
except W.HTTPException as e:
    got = e.detail
check("an empty post box can't be saved", "Nothing to save" in got)
r1 = W.social_draft_save(Req(), W.PostDraftBody(text="Post one", image_url=url, link="https://homerepair.tech/plans",
                                             campaign_id=camp["id"]))
check("a post saves", (r1["ok"], r1["saved"], r1["updated"]), (True, 1, False))
r2 = W.social_draft_save(Req(), W.PostDraftBody(text="Post two"))
check("  and the next one is a second saved post", r2["saved"], 2)
d = W.social_drafts(Req())["rows"]
check("saved posts list newest first, with the campaign and a thumbnail",
      (d[0]["text"], d[1]["campaign"], d[1]["thumb"]), ("Post two", camp["name"], "/media/" + m["id"]))
lib = W.library_list(Req())["rows"]
check("the made picture is kept in Your pictures", (len(lib), lib[0]["name"].startswith("Made: A friendly")), (1, True))
W.social_draft_save(Req(), W.PostDraftBody(text="Post one", image_url=url))
check("saving the same picture twice doesn't duplicate it in the library", len(W.library_list(Req())["rows"]), 1)
W.social_draft_save(Req(), W.PostDraftBody(id=r1["id"], text="Post one, edited", image_url=url, campaign_id=camp["id"]))
check("re-saving an opened post updates it instead of adding one",
      ([x["text"] for x in W.social_drafts(Req())["rows"] if x["id"] == r1["id"]], len(W.social_drafts(Req())["rows"])),
      (["Post one, edited"], 3))
cl = [x for x in W.social_campaigns(Req())["rows"] if x["id"] == camp["id"]][0]
check("the campaign counts its saved posts", (cl["saved"], cl["posts"]), (1, 0))
check("saved posts never appear in the schedule list", W.social_queue_list(Req())["rows"], [])

# scheduling a saved post turns that row into the scheduled one
W.social_connected = lambda: {"facebook": True, "instagram": True}
W.media_check = lambda req: {"ok": True}
when = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
q = W.social_queue_add(Req(), W.QueueBody(networks=["facebook"], text="Post one, edited", image_url=url,
                                          link="https://homerepair.tech/plans", when=when,
                                          campaign_id=camp["id"], draft_id=r1["id"]))
check("scheduling a saved post uses the same row", q["id"], r1["id"])
check("  it leaves Saved posts", r1["id"] in [x["id"] for x in W.social_drafts(Req())["rows"]], False)
check("  and shows in the schedule", [x["id"] for x in W.social_queue_list(Req())["rows"]], [r1["id"]])
check("  with the campaign on the link", "utm_campaign=under-an-hour" in q["link"])
check("  and counts as a post for the campaign",
      [x for x in W.social_campaigns(Req())["rows"] if x["id"] == camp["id"]][0]["posts"], 1)
check("the publisher never touches saved posts", W.social_publish_due("homerepair"), 0)

W.social_draft_used(Req(), r2["id"])
check("posting by hand takes it off the saved list", r2["id"] in [x["id"] for x in W.social_drafts(Req())["rows"]], False)
last = W.social_drafts(Req())["rows"][0]["id"]
W.social_draft_remove(Req(), last)
check("remove deletes a saved post", W.social_drafts(Req())["rows"], [])
W.social_draft_remove(Req(), r1["id"])
check("  but never a scheduled one", [x["id"] for x in W.social_queue_list(Req())["rows"]], [r1["id"]])

W.library_keep_route(Req(), W.LibraryKeepBody(image_url="/media/" + m["id"], name="Tech at AC unit"))
check("'Save to my pictures' works on its own", len(W.library_list(Req())["rows"]), 1)
try:
    W.library_keep_route(Req(), W.LibraryKeepBody(image_url="https://example.com/picture.jpg")); got = ""
except W.HTTPException as e:
    got = e.detail
check("  and refuses pictures from elsewhere", bool(got))

t = ws.CURRENT.set("watson")
check("each business has its own saved posts", W.social_drafts(Req())["rows"], [])
ws.CURRENT.reset(t)

# ── a separate key for writing ──
W.set_setting("imagegen_provider", "meta"); W.set_setting("imagegen_api_key", "sk-META-KEY-1234")
check("with no writer key, the picture key writes", (W.caption_writer_status()["provider"], W.caption_writer_status()["own_key"]), ("meta", False))
st = W.social_caption_writer(Req(), W.CaptionWriterBody(provider="gemini", api_key="AIzaWRITERKEY12345"))["writer"]
check("a Gemini writer key takes over the writing", (st["provider"], st["own_key"], st["ai"]), ("gemini", True, True))
check("  the picture key is untouched", W.setting("imagegen_provider"), "meta")
check("  and the key is never sent back", "WRITERKEY" in json.dumps(W.social_campaigns(Req())), False)
check("  the writer uses it", W.caption_cfg()["api_key"], "AIzaWRITERKEY12345")
try:
    W.social_caption_writer(Req(), W.CaptionWriterBody(provider="elevenlabs", api_key="x" * 20)); got = ""
except W.HTTPException as e:
    got = e.detail
check("a provider that can't write is refused", "Gemini, Meta or OpenAI" in got)
W.social_caption_writer(Req(), W.CaptionWriterBody(provider=""))
check("clearing it goes back to the picture key", W.caption_writer_status()["provider"], "meta")

# ── the media check ──
check("a sign-in redirect is called out as Cloudflare Access",
      "Bypass policy for the path /welcome/*" in M.hosting_problem("https://homerepair.example",
                                                                  lambda u: (302, "Location: https://x.cloudflareaccess.com/")))
check("a 404 from us still means reachable", M.hosting_problem("https://x.example", lambda u: (404, "")), "")
check("the check asks the way Meta's fetcher does", M.FETCHER_UA.startswith("facebookexternalhit"))

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
