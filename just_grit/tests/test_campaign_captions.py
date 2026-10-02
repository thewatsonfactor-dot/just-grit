# -*- coding: utf-8 -*-
"""Campaigns, and post text that writes itself around one.

Daniel, 2026-09-24: "auto gen the description for the post, and make an
area where we can select a campaign and then it makes the post in regards
to the campaign." No network: every provider call goes through a fake.
"""
import io, json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-cap-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
from PIL import Image
import captions as C
import imagegen as G
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

BIZ = {"name": "HomeRepair Tech", "city": "San Antonio, TX",
       "sells": "home maintenance subscriptions and the free Home Health Score",
       "buyer": "homeowners"}
CAMP = C.clean_campaign(C.STARTERS["homerepair"][0])
RULES, BANNED = C.RULES["homerepair"], C.BANNED["homerepair"]

# ── the rules, enforced ──
allowed = RULES + " " + " ".join(CAMP.values())
check("a clean caption passes", C.violations("Four seasons, one plan: Protect Essential for $59 a month.", C.banned_list(BANNED), allowed), [])
check('"free" is caught', C.violations("Get a FREE checkup!", C.banned_list(BANNED), allowed), ['uses "free"'])
check("an invented price is caught",
      C.violations("Fix that leak for $149.", C.banned_list(BANNED), allowed),
      ["mentions a price that isn't one of yours ($149)"])
check("real prices are fine, with or without cents", C.violations("$29, $59.00 and $179", [], allowed), [])
check('"free" inside another word is not "free"', C.violations("freedom to relax", ["free"], ""), [])

# ── template ──
t0 = C.template(BIZ, CAMP, seed=0)
check("the template uses the campaign's offer", "Protect Essential" in t0)
check("  and its call to action", "homerepair.tech/plans" in t0)
check("  and its hashtags, on the last line", t0.splitlines()[-1].startswith("#HomeRepairTech"))
check("write another gives a different shape", len({C.template(BIZ, CAMP, seed=i) for i in range(3)}), 3)
check("no campaign still writes something about the business", "home maintenance" in C.template(BIZ, None))
check("hashtags are cleaned and de-duplicated", C.hashtags("#a, b  #A #c-d"), ["#a", "#b", "#cd"])

# ── write(): no key -> template, with a note that says why ──
r = C.write(BIZ, CAMP, rules=RULES, banned=BANNED)
check("no key: the template, and it says so", (r["source"], "No AI key" in r["note"]), ("template", True))

# ── write(): Gemini, looking at the picture ──
calls = []
def gemini_ok(method, url, headers, body, timeout=0):
    calls.append((url, json.loads(body)))
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text":
        "Caption: Saturday morning, coffee in hand, and that squeaky door is still squeaking.\n\n"
        "With Protect Essential, $59 a month gets you four seasonal visits and member labor at $75/hr.\n\n"
        "See the plans at homerepair.tech/plans\n\n#HomeRepairTech #SanAntonio"}]}}]}).encode()
cfg = G.config("gemini", "AIzaSECRETKEY123456", "", "gemini-3.1-flash-image")
img = b"\xff\xd8\xff" + b"0" * 50
r = C.write(BIZ, CAMP, cfg=cfg, image=img, rules=RULES, banned=BANNED, http=gemini_ok)
check("Gemini writes it", (r["source"], r["provider"], r["saw_picture"]), ("ai", "gemini", True))
check("  with a text model, not the picture model", "gemini-3.8-flash:generateContent" in calls[0][0])
parts = calls[0][1]["contents"][0]["parts"]
check("  and the picture is sent along", parts[0]["inline_data"]["mime_type"], "image/jpeg")
prompt = parts[-1]["text"]
check("  the prompt carries the campaign facts", "Four seasonal care visits" in prompt)
check("  and the business rules", 'Never use the word "free"' in prompt)
check("  and the blurb has its banned word taken out", "the free Home" not in prompt and "Home Health Score" in prompt)
check("the 'Caption:' label is tidied off", r["text"].startswith("Saturday morning"))
check("the key never shows up in anything returned", "SECRETKEY" in json.dumps(r), False)

# a retired model -> the next one
calls.clear()
def gemini_404_first(method, url, headers, body, timeout=0):
    calls.append(url)
    if "3.8" in url:
        return 404, b'{"error":{"message":"not found"}}'
    return gemini_ok(method, url, headers, body)
r = C.write(BIZ, CAMP, cfg=cfg, rules=RULES, banned=BANNED, http=gemini_404_first)
check("a retired model falls through to the next", (r["source"], r["model"]), ("ai", "gemini-flash-latest"))

# the AI says "free" twice -> template, with the reason
def gemini_free(method, url, headers, body, timeout=0):
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": "Get a free home checkup today! #HomeRepairTech"}]}}]}).encode()
r = C.write(BIZ, CAMP, cfg=cfg, rules=RULES, banned=BANNED, http=gemini_free)
check("an AI draft that breaks a rule twice becomes the template", (r["source"], 'uses "free"' in r["note"]), ("template", True))
# ...but fixes it on the retry when it can
seen = []
def gemini_learns(method, url, headers, body, timeout=0):
    p = json.loads(body)["contents"][0]["parts"][-1]["text"]
    seen.append(p)
    txt = "A tech every month, up to 60 minutes included. #HomeRepairTech" if "broke a rule" in p else "Totally free visit! #HomeRepairTech"
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": txt}]}}]}).encode()
r = C.write(BIZ, CAMP, cfg=cfg, rules=RULES, banned=BANNED, http=gemini_learns)
check("the retry names the problem and the second draft is used", (r["source"], 'uses "free"' in seen[-1]), ("ai", True))

# a bad key -> template, error is readable, key redacted
def gemini_401(method, url, headers, body, timeout=0):
    return 401, b'{"error":"bad key AIzaSECRETKEY123456"}'
r = C.write(BIZ, CAMP, cfg=cfg, rules=RULES, banned=BANNED, http=gemini_401)
check("a rejected key falls back to the template with the reason", (r["source"], "rejected the API key" in r["note"]), ("template", True))

# ── Meta: Responses API, muse-spark, picture as a data URL ──
mcalls = []
def meta_ok(method, url, headers, body, timeout=0):
    mcalls.append((url, json.loads(body)))
    return 200, json.dumps({"id": "resp_1", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Your list of little fixes, handled. #HomeRepairTech"}]}]}).encode()
mcfg = G.config("meta", "sk-SECRET-META-KEY", "", "muse-image-1.0")
r = C.write(BIZ, CAMP, cfg=mcfg, image=img, rules=RULES, banned=BANNED, http=meta_ok)
check("Meta writes it with muse-spark", (r["source"], r["model"]), ("ai", "muse-spark-1.3"))
content = mcalls[0][1]["input"][0]["content"]
check("  the picture goes as an input_image data URL", content[1]["image_url"].startswith("data:image/jpeg;base64,"))
# Meta refuses pictures -> same model, text only
mcalls.clear()
def meta_no_images(method, url, headers, body, timeout=0):
    b = json.loads(body)
    if len(b["input"][0]["content"]) > 1:
        return 400, b'{"error":"image input not supported"}'
    return meta_ok(method, url, headers, body)
r = C.write(BIZ, CAMP, cfg=mcfg, image=img, rules=RULES, banned=BANNED, http=meta_no_images)
check("a provider that refuses the picture gets asked again without it", (r["source"], len(mcalls)), ("ai", 1))

def meta_402(method, url, headers, body, timeout=0):
    return 402, b'{"error":{"code":"billing_not_configured","message":"Billing verification failed."}}'
r = C.write(BIZ, CAMP, cfg=mcfg, rules=RULES, banned=BANNED, http=meta_402)
check("no billing on the Meta account: template, and it says what to fix",
      (r["source"], "billing isn't set up" in r["note"]), ("template", True))

# ElevenLabs makes pictures but not text
ecfg = G.config("elevenlabs", "xi-KEY-123456789", "", "")
r = C.write(BIZ, CAMP, cfg=ecfg, rules=RULES, banned=BANNED)
check("ElevenLabs: template, and it says why", (r["source"], "doesn't write text" in r["note"]), ("template", True))

check("utm campaign names are url-safe", C.slug("Under an hour? It's included"), "under-an-hour-it-s-included")

# ── the routes ──
W.DATA = pathlib.Path(TMP)
for s in ("homerepair", "watson"):
    t = ws.CURRENT.set(s); W.init_db(); ws.CURRENT.reset(t)
tok = ws.CURRENT.set("homerepair")
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

W.local_today = lambda: "2026-10-15"          # the October catalog (test_catalog_switch covers the flip)
lst = W.social_campaigns(Req())
check("HomeRepair starts with its six starter campaigns", [r["name"] for r in lst["rows"]][:1] + [len(lst["rows"])],
      ["Four seasons, one plan", 6])
check("  none of them say 'free'", any(C.violations(json.dumps(r), ["free"], json.dumps(r)) for r in lst["rows"]), False)
check("  the rules come pre-filled", lst["rules"].startswith("Never use the word"))
check("  and the writer says there's no AI yet", lst["writer"]["ai"], False)
fall = [r for r in lst["rows"] if r["name"] == "Fall home checklist"][0]
check("a dated campaign knows whether it's running", isinstance(fall["live"], bool))

saved = W.social_campaign_save(Req(), W.CampaignBody(name="Spring AC tune-up", offer="Essential visit",
                               details="Up to 60 minutes a month.", hashtags="#SpringReady"))
cid = saved["campaign"]["id"]
check("a new campaign saves", saved["campaign"]["name"], "Spring AC tune-up")
W.social_campaign_save(Req(), W.CampaignBody(id=cid, name="Spring AC tune-up", offer="Essential visit - $99/mo"))
check("  and edits", W.social_campaigns(Req())["rows"][-1]["offer"], "Essential visit - $99/mo")
try:
    W.social_campaign_save(Req(), W.CampaignBody(name="Backwards", starts="2026-10-01", ends="2026-09-01")); got = ""
except W.HTTPException as e:
    got = e.detail
check("an end date before the start is refused", "before the start" in got)
W.social_campaign_remove(Req(), cid)
check("  and removes", len(W.social_campaigns(Req())["rows"]), 6)
for r in W.social_campaigns(Req())["rows"]:
    W.social_campaign_remove(Req(), r["id"])
check("deleting every starter doesn't bring them back", len(W.social_campaigns(Req())["rows"]), 0)
t = ws.CURRENT.set("watson")
check("Watson Factor has its own campaigns", [r["name"] for r in W.social_campaigns(Req())["rows"]],
      ["Never miss a call", "Websites that make the phone ring"])
ws.CURRENT.reset(t)

camp = W.social_campaign_save(Req(), W.CampaignBody(**C.STARTERS["homerepair"][0]))["campaign"]
# a generated picture in the media store, with its prompt in the log
b = io.BytesIO(); Image.new("RGB", (1200, 1200), (90, 120, 80)).save(b, "JPEG")
m = W.mstore.save(b.getvalue(), W.DATA)
with W.closing(W.db()) as c:
    c.execute("INSERT INTO imagegen_log (provider, model, prompt, media_id, ok, at) VALUES ('gemini','m',?,?,1,?)",
              ("a technician fixing a cabinet hinge", m["id"], W.now()))
    c.commit()
raw, hint = W.caption_picture("https://homerepair.thewatsonfactor.dev/welcome/m/" + m["id"])
check("our own picture is read, shrunk, and its prompt found",
      (raw is not None, max(Image.open(io.BytesIO(raw)).size) <= 768, hint), (True, True, "a technician fixing a cabinet hinge"))
check("a picture from anywhere else is never fetched", W.caption_picture("https://example.com/x.jpg"), (None, ""))

r = W.social_caption(Req(), W.CaptionBody(campaign_id=camp["id"], image_url="/media/" + m["id"]))
check("no key: the post box still gets a caption", (r["source"], "Protect Essential" in r["text"]), ("template", True))
check("  and the campaign's link comes back for the link box", r["campaign"]["link"], "https://homerepair.tech/plans")

W.set_setting("imagegen_provider", "gemini"); W.set_setting("imagegen_api_key", "AIzaSECRETKEY123456")
G.http = gemini_ok; calls.clear()
r = W.social_caption(Req(), W.CaptionBody(campaign_id=camp["id"], image_url="/media/" + m["id"], angle=2))
check("with a Gemini key the route writes with AI and sends the picture",
      (r["source"], r["saw_picture"], "inline_data" in json.dumps(calls[0][1])), ("ai", True, True))
check("  the angle steps (write another)", r["angle"], "story")
check("  the list now says the writer is on", W.social_campaigns(Req())["writer"]["ai"], True)

W.social_caption_rules(Req(), W.CaptionRulesBody(rules="Be kind.", banned="cheap"))
check("rules are editable per workspace", W.caption_rules(), ("Be kind.", "cheap"))

check("a post's link is tracked with the campaign's name",
      "utm_campaign=four-seasons-one-plan" in W.socialkit.utm("https://homerepair.tech/plans", "facebook",
                                                                    W.campaign_for(camp["id"])[1]))
check("with_link adds the link once", W.with_link("Hi", "https://x.co") == "Hi\n\nhttps://x.co" and
      W.with_link("Hi https://x.co", "https://x.co") == "Hi https://x.co")
check("a deleted campaign doesn't break a post", W.campaign_for(99999), (None, ""))

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
