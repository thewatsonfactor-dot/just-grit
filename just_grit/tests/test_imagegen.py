# -*- coding: utf-8 -*-
"""Bring-your-own image generation. Every provider path runs here against a
fake network, so the shape of each request and the parsing of each answer
is pinned without a key. The one rule that matters most: the customer's
key never comes back out - not in /api/me, not in an error, not to a
third-party download host.
"""
import base64, io, json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-imagegen-")
os.environ["JUST_GRIT_DATA"] = TMP
sys.path.insert(0, '.')
import imagegen as G
import webapp as W, workspaces as ws
from contextlib import closing
W.DATA = pathlib.Path(TMP)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

KEY = "sk-live-abcdefghijklmnop"
PNG = base64.b64decode(  # 1x1 red PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")
try:
    from PIL import Image
    _b = io.BytesIO(); Image.new("RGB", (4, 4), (200, 30, 30)).save(_b, "WEBP"); WEBP = _b.getvalue()
    HAVE_PIL = True
except Exception:
    WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
    HAVE_PIL = False

# ── config ───────────────────────────────────────────────────────────────
try:
    G.config("", KEY); got = "no error"
except G.ImageGenError as e:
    got = "provider" in str(e)
check("no provider chosen -> a sentence that says so", got, True)
try:
    G.config("meta", ""); got = "no error"
except G.ImageGenError as e:
    got = "no API key" in str(e)
check("provider without a key -> says which provider", got, True)
try:
    G.config("midjourney", KEY); got = "no error"
except G.ImageGenError as e:
    got = True
check("an unknown provider is refused", got, True)
cfg = G.config("meta", KEY)
check("meta defaults: base url and model filled in", (cfg["base_url"], cfg["model"]),
      ("https://api.meta.ai/v1", "muse-image-1.0"))
cfg2 = G.config("openai", KEY, base_url="https://api.together.xyz/v1/", model="flux-1")
check("openai-compatible: overrides win, trailing slash trimmed",
      (cfg2["base_url"], cfg2["model"]), ("https://api.together.xyz/v1", "flux-1"))
check("the key is redacted out of any text", G.redact("bad key " + KEY + " here", KEY),
      "bad key [key] here")

# ── a fake network that records what it was asked ───────────────────────
class Net:
    def __init__(self, script):
        self.script = list(script); self.calls = []
    def __call__(self, method, url, headers, body, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": json.loads(body) if body else None})
        status, payload = self.script.pop(0)
        return status, (payload if isinstance(payload, bytes) else json.dumps(payload).encode())

# ── meta ─────────────────────────────────────────────────────────────────
meta_ok = {"id": "resp_123", "status": "completed", "usage": {"total_tokens": 900},
           "output": [{"type": "reasoning"}, {"type": "message", "content": [{"text": "here"}]},
                      {"type": "image_generation_call", "result": base64.b64encode(WEBP).decode()}]}
net = Net([(200, meta_ok)])
out = G.generate(cfg, "a taco truck at golden hour", http=net)
c = net.calls[0]
check("meta: POST {base}/responses", (c["method"], c["url"]), ("POST", "https://api.meta.ai/v1/responses"))
check("meta: bearer auth", c["headers"].get("Authorization"), "Bearer " + KEY)
check("meta: model + input, no previous id on a fresh picture",
      (c["body"]["model"], c["body"]["input"][0]["content"][0]["text"], "previous_response_id" in c["body"]),
      ("muse-image-1.0", "a taco truck at golden hour", False))
check("meta: shaped like Meta's own example - user message, image_generation tool, stored",
      (c["body"]["input"][0]["role"], c["body"]["input"][0]["content"][0]["type"],
       c["body"]["tools"][0]["type"], c["body"]["store"], c["body"]["stream"]),
      ("user", "input_text", "image_generation", True, False))
check("meta: bytes come out of image_generation_call.result", out["bytes"], WEBP)
check("meta: mime sniffed as webp", out["mime"], "image/webp")
check("meta: response id kept for refining", out["response_id"], "resp_123")
check("meta: usage passed through for the ledger", out["usage"], {"total_tokens": 900})

net = Net([(200, meta_ok)])
G.generate(cfg, "make it dusk", previous_id="resp_123", http=net)
check("meta: a refinement chains with previous_response_id",
      net.calls[0]["body"].get("previous_response_id"), "resp_123")

refused = {"id": "r2", "status": "completed",
           "output": [{"type": "message", "content": [{"text": "I can't make that image."}]}]}
net = Net([(200, refused)])
try:
    G.generate(cfg, "something", http=net); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("meta: a words-only answer surfaces what the model said", "can't make that" in got, True)

net = Net([(401, {"error": {"message": "invalid key " + KEY}})])
try:
    G.generate(cfg, "x", http=net); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("meta: 401 -> 'rejected the API key', not a stack trace", "rejected the API key" in got, True)
check("  and the key is not in the message", KEY not in got, True)

net = Net([(500, {"error": {"message": "boom with " + KEY}})])
try:
    G.generate(cfg, "x", http=net); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("meta: any other HTTP error quotes the provider, key redacted",
      "HTTP 500" in got and KEY not in got and "[key]" in got, True)

# ── elevenlabs ───────────────────────────────────────────────────────────
ecfg = G.config("elevenlabs", "xi-secret-key-0123456789")
slept = []
net = Net([(200, {"id": "gen1", "status": "pending"}),
           (200, {"id": "gen1", "status": "generating"}),
           (200, {"id": "gen1", "status": "completed",
                  "content_url": "https://storage.googleapis.com/generations/gen1",
                  "content_mime_type": "image/png"}),
           (200, PNG)])
out = G.generate(ecfg, "a corgi lifeguard", http=net, sleep=slept.append)
c0 = net.calls[0]
check("elevenlabs: POST /v1/flows/image with xi-api-key",
      (c0["url"], c0["headers"].get("xi-api-key"), c0["body"]),
      ("https://api.elevenlabs.io/v1/flows/image", "xi-secret-key-0123456789",
       {"model_id": "gpt-image-2", "prompt": "a corgi lifeguard"}))
check("elevenlabs: polls GET /v1/flows/image/{id} until completed",
      [x["url"].rsplit("/", 1)[-1] for x in net.calls[1:3]], ["gen1", "gen1"])
check("elevenlabs: slept between polls", len(slept), 2)
check("elevenlabs: downloads content_url WITHOUT the api key",
      (net.calls[3]["url"], "xi-api-key" in net.calls[3]["headers"]),
      ("https://storage.googleapis.com/generations/gen1", False))
check("elevenlabs: bytes are the download", out["bytes"], PNG)
check("elevenlabs: no response id (cannot refine)", out["response_id"], "")

net = Net([(200, {"id": "gen2", "status": "failed", "failure_reason": "content_policy",
                  "error_message": "nope"})])
try:
    G.generate(ecfg, "x", http=net, sleep=lambda s: None); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("elevenlabs: a failed job says why", "nope" in got, True)

net = Net([(200, {"id": "gen3", "status": "pending"})] + [(200, {"id": "gen3", "status": "generating"})] * 200)
try:
    G.generate(ecfg, "x", http=net, sleep=lambda s: None); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("elevenlabs: gives up after POLL_MAX seconds instead of hanging", "still working" in got, True)

try:
    G.generate(ecfg, "x", previous_id="gen1", http=Net([])); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("refine on a provider that cannot: refused before any network call",
      "cannot refine" in got, True)

# ── openai / compatible ──────────────────────────────────────────────────
ocfg = G.config("openai", KEY)
net = Net([(200, {"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"total_tokens": 5}})])
out = G.generate(ocfg, "a kitchen", http=net)
c0 = net.calls[0]
check("openai: POST {base}/images/generations", c0["url"], "https://api.openai.com/v1/images/generations")
check("openai: gpt-image-* gets no response_format (it always returns b64)",
      "response_format" in c0["body"], False)
check("openai: bytes from b64_json", out["bytes"], PNG)

ocfg2 = G.config("openai", KEY, base_url="https://api.together.xyz/v1", model="flux-1")
net = Net([(200, {"data": [{"url": "https://cdn.example.com/x.png"}]}), (200, PNG)])
out = G.generate(ocfg2, "a kitchen", http=net)
check("compatible host: non gpt-image model asks for b64_json explicitly",
      net.calls[0]["body"].get("response_format"), "b64_json")
check("compatible host: a URL answer is downloaded, no auth header sent to it",
      (net.calls[1]["url"], net.calls[1]["headers"]), ("https://cdn.example.com/x.png", {}))
check("  bytes are the download", out["bytes"], PNG)

# ── prompt guards ────────────────────────────────────────────────────────
try:
    G.generate(cfg, "   ", http=Net([])); got = "no error"
except G.ImageGenError as e:
    got = str(e)
check("an empty prompt is refused before the network", "Describe" in got, True)
try:
    G.generate(cfg, "x" * (G.MAX_PROMPT + 1), http=Net([])); got = "no error"
except G.ImageGenError as e:
    got = "characters" in str(e)
check("an absurdly long prompt is refused before the network", got, True)

# ── JPEG, because Instagram ──────────────────────────────────────────────
if HAVE_PIL:
    j = G.to_jpeg(WEBP, "image/webp")
    check("webp -> jpeg", j[:3], b"\xff\xd8\xff")
    j = G.to_jpeg(PNG, "image/png")
    check("png -> jpeg (alpha flattened onto white)", j[:3], b"\xff\xd8\xff")
    jpeg_in = j
    check("jpeg in -> the same bytes out, untouched", G.to_jpeg(jpeg_in, "image/jpeg") is jpeg_in, True)
else:
    print("skip  Pillow not installed here; JPEG conversion not exercised")

# ── the app side ─────────────────────────────────────────────────────────
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

tok = ws.CURRENT.set(ws.PRIMARY)
try:
    W.init_db()
    W.require_auth = lambda r: "local"

    try:
        W.save_settings(Req(), W.Settings(imagegen_provider="dalle9")); got = "no error"
    except Exception as e:
        got = getattr(e, "status_code", None)
    check("settings: an unknown provider is a 400, not stored", got, 400)

    W.save_settings(Req(), W.Settings(imagegen_provider="meta", imagegen_api_key=KEY,
                                      imagegen_style="Warm light, one orange accent."))
    me = W.me(Req())
    ig = me["imagegen"]
    check("/api/me says a key is saved", ig["has_key"], True)
    check("/api/me never carries the key itself", KEY in json.dumps(me), False)
    check("/api/me: ready, with the provider's defaults filled in",
          (ig["ready"], ig["label"], ig["model"], ig["refine"]),
          (True, "Meta Muse Image", "muse-image-1.0", True))

    W.save_settings(Req(), W.Settings(imagegen_api_key=""))
    check("saving a blank key leaves the saved one alone (the field renders blank)",
          W.setting("imagegen_api_key", ""), KEY)

    # generate through the route, with the network faked at the module level
    real_http = G.http
    G.http = Net([(200, meta_ok)])
    try:
        r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a taco truck"))
    finally:
        sent_body = G.http.calls[0]["body"]
        G.http = real_http
    check("route: the brand notes are put in front of the prompt",
          sent_body["input"][0]["content"][0]["text"].startswith("Warm light, one orange accent."), True)
    check("route: stored as a JPEG in the media store", r["id"].endswith(".jpg"), True)
    check("route: response id handed back for refining", r["response_id"], "resp_123")
    check("route: the picture gets a URL on this workspace's own address",
          r["url"].startswith("https://justgrit.thewatsonfactor.dev/welcome/m/"), True)
    # ...and when there is no address at all - a workspace nobody gave a
    # hostname - it is stored anyway and says why there is no URL, because
    # a picture Meta cannot fetch is a post that fails with a useless error.
    _real_base = W.public_base
    W.public_base = lambda: ""
    G.http = Net([(200, meta_ok)])
    try:
        r2 = W.imagegen_make(Req(), W.ImageGenBody(prompt="no address here"))
    finally:
        W.public_base = _real_base; G.http = real_http
    check("route: no address -> stored, URL empty, and it says so",
          (r2["url"], "public_base_url" in r2.get("warning", "")), ("", True))
    with closing(W.db()) as c:
        row = c.execute("SELECT provider, model, ok, media_id, response_id FROM imagegen_log "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    check("route: the ledger has the row", (row[0], row[1], row[2], row[3] == r["id"], row[4]),
          ("meta", "muse-image-1.0", 1, True, "resp_123"))

    G.http = Net([(429, {"error": "slow down"})])
    try:
        W.imagegen_make(Req(), W.ImageGenBody(prompt="another")); got = "no error"
    except Exception as e:
        got = (getattr(e, "status_code", None), "429" in str(getattr(e, "detail", "")))
    finally:
        G.http = real_http
    check("route: a provider failure is a 502 with the provider's words", got, (502, True))
    with closing(W.db()) as c:
        row = c.execute("SELECT ok, error FROM imagegen_log ORDER BY id DESC LIMIT 1").fetchone()
    check("  and the failure is in the ledger too", (row[0], "429" in row[1]), (0, True))
    st = W.imagegen_status()
    check("status counts this month's makes and failures",
          (st["made_this_month"], st["failed_this_month"]), (2, 1))

    rec = W.imagegen_recent(Req())
    check("recent: newest first, failures included, no key anywhere",
          (rec["rows"][0]["ok"], rec["rows"][1]["ok"], KEY in json.dumps(rec)), (0, 1, False))

    W.clear_imagegen_key(Req())
    check("clear_imagegen_key removes it", W.setting("imagegen_api_key", ""), "")
    try:
        W.imagegen_make(Req(), W.ImageGenBody(prompt="a picture")); got = "no error"
    except Exception as e:
        got = getattr(e, "status_code", None)
    check("with no key the route is a 400 before any network", got, 400)

    cat = W.imagegen_providers(Req())
    check("providers catalogue: four (Gemini added 2026-09-24), meta marked as suggested, each with steps",
          ([p["key"] for p in cat["providers"]],
           [p["recommended"] for p in cat["providers"]],
           all(len(p["setup"]) >= 3 for p in cat["providers"])),
          (["meta", "elevenlabs", "gemini", "openai"], [True, False, False, False], True))
finally:
    ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
