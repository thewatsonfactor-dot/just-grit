# -*- coding: utf-8 -*-
"""Bring-your-own image generation for the Social tab.

A post needs a picture and the customer should not be buying pictures from
us. Every workspace holds its own provider and its own key, so the bill lands
on the business that made the image, and switching providers is a dropdown.

Four providers, one shape. `generate()` returns raw image bytes plus a
mime type and (where the provider supports it) a response id that a later
call can chain from to refine the same picture instead of starting over.
Nothing in here writes to disk or the database - webapp.py converts to JPEG
(Instagram takes nothing else), stores through media.py, and logs the call.

Verified against the providers' own docs on 2026-09-20:
  meta        POST {base}/responses, model muse-image-1.0. Output is a list
              whose `image_generation_call` item carries base64 WebP.
              `previous_response_id` chains a refinement. $0.01 / image.
  elevenlabs  POST /v1/flows/image {model_id, prompt} -> {id, status}; poll
              GET /v1/flows/image/{id} until completed -> content_url.
  openai      POST {base}/images/generations, the OpenAI Images shape that
              OpenAI itself and most compatible hosts speak. b64_json or url.

The network goes through one injectable `http` callable so every provider
path is testable without a key or a connection.
"""
from __future__ import annotations
import base64
import io
import json
import time
import urllib.error
import urllib.request

TIMEOUT = 120
POLL_EVERY = 2.0          # elevenlabs is async; this is how often we ask
POLL_MAX = 90             # ...and for how long before giving up (seconds)
MAX_PROMPT = 2000

PROVIDERS = {
    "meta": {
        "label": "Meta Muse Image",
        "recommended": True,
        "cost": "$0.01 per image, pay-as-you-go on your Meta developer account",
        "base_url": "https://api.meta.ai/v1",
        "model": "muse-image-1.0",
        "key_label": "Model API key (starts with sk-)",
        "refine": True,
        "needs_base_url": False,
        "setup": [
            "Go to dev.meta.ai and open the Model API dashboard.",
            "Create an API key. Copy it - it is shown once.",
            "Paste it below and press Save. It is stored for this workspace only "
            "and never shown again.",
            "Billing is pay-as-you-go on that developer account - separate from "
            "any Meta AI subscription. A cent a picture: 100 posts is a dollar.",
        ],
    },
    "elevenlabs": {
        "label": "ElevenLabs (Image & Video)",
        "recommended": False,
        "cost": "Billed in ElevenLabs credits; price depends on the model you pick",
        "base_url": "https://api.elevenlabs.io",
        "model": "gpt-image-2",
        "key_label": "ElevenLabs API key (xi-api-key)",
        "refine": False,
        "needs_base_url": False,
        "setup": [
            "In ElevenLabs, open your profile menu -> API Keys and create one.",
            "Paste it below. The model field is which image model ElevenLabs "
            "runs for you (gpt-image-2, gemini-3-pro-image, "
            "bytedance-seedream-5-pro...). Leave it alone unless you know why.",
            "Generation is asynchronous on their side; a picture takes 10-40 "
            "seconds to come back.",
        ],
    },
    "gemini": {
        "label": "Google Gemini",
        "recommended": False,
        "cost": "A few cents a picture, billed to your Google Cloud project - a Google AI Pro "
                "plan's $10/month Cloud credit can cover it",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3.1-flash-image",
        "key_label": "Gemini API key (from Google AI Studio)",
        "refine": False,
        "needs_base_url": False,
        "setup": [
            "Go to aistudio.google.com and sign in with the Google account that has your AI Pro plan.",
            "Click Get API key -> Create API key, and pick (or create) a Google Cloud project.",
            "Paste the key below and press Save. It is stored for this workspace only and never "
            "shown again.",
            "Your AI Pro subscription itself does not pay for pictures made here - Google bills them "
            "to that Cloud project. AI Pro includes $10 a month of Google Cloud credit (claim it by "
            "linking AI Pro to your Google Developer Program profile), which covers a couple hundred "
            "pictures. Model: gemini-3.1-flash-image is the balanced choice; "
            "gemini-3.1-flash-lite-image is cheapest; gemini-3-pro-image is the best.",
        ],
    },
    "openai": {
        "label": "OpenAI, or any OpenAI-compatible image API",
        "recommended": False,
        "cost": "Whatever that provider charges; OpenAI's gpt-image-1 is roughly "
                "$0.02-0.19 a picture depending on size and quality",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-image-1",
        "key_label": "API key",
        "refine": False,
        "needs_base_url": True,
        "setup": [
            "Create an API key with the provider and paste it below.",
            "For OpenAI itself leave the base URL and model as they are.",
            "For another host that speaks the OpenAI Images API (Together, "
            "Fireworks, a self-hosted gateway...), set its base URL - the part "
            "before /images/generations - and the model name it expects.",
        ],
    },
}


class ImageGenError(RuntimeError):
    pass


# ── config ──────────────────────────────────────────────────────────────
def config(provider: str, api_key: str, base_url: str = "", model: str = "") -> dict:
    """The settings as they are stored -> what a call needs. Raises with a
    sentence a person can act on, never a stack trace."""
    p = PROVIDERS.get((provider or "").strip())
    if not p:
        raise ImageGenError("No image provider chosen yet. Pick one in the Social "
                            "tab - Meta at a cent a picture is the easy default.")
    key = (api_key or "").strip()
    if not key:
        raise ImageGenError("%s is selected but there is no API key saved for it." % p["label"])
    return {
        "provider": provider.strip(),
        "api_key": key,
        "base_url": ((base_url or "").strip() or p["base_url"]).rstrip("/"),
        "model": (model or "").strip() or p["model"],
        "refine": p["refine"],
    }


def redact(text: str, key: str) -> str:
    """The key must never come back in an error message. It is the one thing
    on this path a customer typed that they would not want in a log."""
    t = str(text or "")
    if key and len(key) >= 8:
        t = t.replace(key, "[key]")
    return t


# ── the one place the network happens ───────────────────────────────────
def http(method: str, url: str, headers: dict, body: bytes | None, timeout: int = TIMEOUT):
    """(status, body_bytes). Non-2xx is returned, not raised, so callers
    can read the provider's own error text."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json(status: int, body: bytes, what: str, key: str) -> dict:
    if status >= 400:
        snippet = redact(body.decode("utf-8", "replace")[:300], key)
        if status in (401, 403):
            raise ImageGenError("%s rejected the API key (HTTP %d). Check it was pasted "
                                "whole, and that billing is set up on that account." % (what, status))
        if status == 402:
            raise ImageGenError("%s says billing isn't set up on that account (HTTP 402) - add or "
                                "check the payment method on the provider's billing page, then try "
                                "again." % what)
        if status == 429:
            raise ImageGenError("%s says slow down (HTTP 429). Wait a minute and try again." % what)
        raise ImageGenError("%s answered HTTP %d: %s" % (what, status, snippet))
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise ImageGenError("%s sent back something that is not JSON." % what)


# ── providers ───────────────────────────────────────────────────────────
def _meta(cfg: dict, prompt: str, previous_id: str | None, http=http) -> dict:
    """Meta's Responses API. One conversation per picture: pass the previous
    response id and describe only the change to refine it."""
    # Shaped like Meta's own example on dev.meta.ai (2026-09-24): the prompt
    # as a user message, and the image_generation tool named explicitly -
    # a bare string input is not what their docs show. store=True is what
    # lets previous_response_id find this picture later to refine it.
    payload = {
        "model": cfg["model"],
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": prompt}]}],
        "tools": [{"type": "image_generation", "output_format": "png"}],
        "store": True,
        "stream": False,
        "max_output_tokens": 2048,
    }
    if previous_id:
        payload["previous_response_id"] = previous_id
    status, body = http("POST", cfg["base_url"] + "/responses",
                        {"Authorization": "Bearer " + cfg["api_key"],
                         "Content-Type": "application/json"},
                        json.dumps(payload).encode("utf-8"))
    data = _json(status, body, "Meta", cfg["api_key"])
    if data.get("status") not in (None, "completed"):
        raise ImageGenError("Meta returned status %r instead of an image." % data.get("status"))
    b64 = None
    for item in data.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "image_generation_call":
            b64 = item.get("result")
            break
    if not b64:
        # The model can answer in words instead of a picture (a refused
        # prompt, usually). Surface what it said rather than "no image".
        said = ""
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("text"):
                        said = part["text"][:200]
        raise ImageGenError("Meta did not return an image%s." % (": " + said if said else ""))
    raw = base64.b64decode(b64)
    return {"bytes": raw, "mime": _sniff(raw, "image/webp"),
            "response_id": data.get("id") or "", "usage": data.get("usage") or {}}


def _elevenlabs(cfg: dict, prompt: str, http=http, sleep=time.sleep) -> dict:
    """Submit, then poll. content_url is fetched with no auth header - it is
    a signed storage link, and sending the key to a third-party host would
    be exactly the wrong thing."""
    hdr = {"xi-api-key": cfg["api_key"], "Content-Type": "application/json"}
    status, body = http("POST", cfg["base_url"] + "/v1/flows/image", hdr,
                        json.dumps({"model_id": cfg["model"], "prompt": prompt}).encode("utf-8"))
    job = _json(status, body, "ElevenLabs", cfg["api_key"])
    gid = job.get("id")
    if not gid:
        raise ImageGenError("ElevenLabs accepted the request but gave no generation id.")
    waited = 0.0
    while True:
        st = job.get("status")
        if st == "completed":
            break
        if st == "failed":
            raise ImageGenError("ElevenLabs failed the generation: %s"
                                % redact(job.get("error_message") or job.get("failure_reason")
                                         or "no reason given", cfg["api_key"]))
        if waited >= POLL_MAX:
            raise ImageGenError("ElevenLabs is still working after %d seconds. Try again "
                                "in a minute - the picture may still finish on their side." % POLL_MAX)
        sleep(POLL_EVERY); waited += POLL_EVERY
        status, body = http("GET", cfg["base_url"] + "/v1/flows/image/" + str(gid),
                            {"xi-api-key": cfg["api_key"]}, None)
        job = _json(status, body, "ElevenLabs", cfg["api_key"])
    url = job.get("content_url")
    if not url:
        raise ImageGenError("ElevenLabs finished but gave no content_url.")
    status, raw = http("GET", url, {}, None)
    if status >= 400 or not raw:
        raise ImageGenError("Could not download the finished image (HTTP %d)." % status)
    return {"bytes": raw, "mime": _sniff(raw, job.get("content_mime_type") or "image/png"),
            "response_id": "", "usage": {}}


def _openai_images(cfg: dict, prompt: str, http=http) -> dict:
    """POST /images/generations. gpt-image-* always returns b64_json; older
    models and some compatible hosts return a URL, so both are handled."""
    payload = {"model": cfg["model"], "prompt": prompt, "n": 1}
    if not cfg["model"].startswith("gpt-image"):
        payload["response_format"] = "b64_json"
    status, body = http("POST", cfg["base_url"] + "/images/generations",
                        {"Authorization": "Bearer " + cfg["api_key"],
                         "Content-Type": "application/json"},
                        json.dumps(payload).encode("utf-8"))
    data = _json(status, body, "The image API", cfg["api_key"])
    items = data.get("data") or []
    if not items:
        raise ImageGenError("The image API returned no image.")
    first = items[0] or {}
    if first.get("b64_json"):
        raw = base64.b64decode(first["b64_json"])
    elif first.get("url"):
        status, raw = http("GET", first["url"], {}, None)
        if status >= 400 or not raw:
            raise ImageGenError("Could not download the finished image (HTTP %d)." % status)
    else:
        raise ImageGenError("The image API returned neither bytes nor a URL.")
    return {"bytes": raw, "mime": _sniff(raw, "image/png"),
            "response_id": "", "usage": data.get("usage") or {}}


def _find_image(obj):
    """(base64, mime) from wherever Google put it. The Interactions API
    returns interaction.output_image {mime_type, data}; the older
    generateContent returns candidates[].content.parts[].inlineData
    {mimeType, data}. Walk the JSON for the first image-shaped object
    rather than trusting one path."""
    if isinstance(obj, dict):
        for k in ("output_image", "inlineData", "inline_data", "image"):
            v = obj.get(k)
            if isinstance(v, dict) and isinstance(v.get("data"), str) and len(v["data"]) > 100:
                return v["data"], v.get("mime_type") or v.get("mimeType") or ""
        mime = obj.get("mime_type") or obj.get("mimeType") or ""
        if str(mime).startswith("image/") and isinstance(obj.get("data"), str) and len(obj["data"]) > 100:
            return obj["data"], mime
        for v in obj.values():
            got = _find_image(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_image(v)
            if got:
                return got
    return None


def _find_text(obj) -> str:
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str):
            return obj["text"]
        for v in obj.values():
            t = _find_text(v)
            if t:
                return t
    elif isinstance(obj, list):
        for v in obj:
            t = _find_text(v)
            if t:
                return t
    return ""


def _gemini(cfg: dict, prompt: str, http=http) -> dict:
    """Google's Interactions API (the current image path in their docs),
    falling back to models/<model>:generateContent for keys or models that
    haven't moved over. The key goes in a header, never the URL."""
    hdr = {"x-goog-api-key": cfg["api_key"], "Content-Type": "application/json"}
    status, body = http("POST", cfg["base_url"] + "/interactions", hdr,
                        json.dumps({"model": cfg["model"],
                                    "input": [{"type": "text", "text": prompt}]}).encode("utf-8"))
    if status in (404, 405):
        status, body = http("POST", "%s/models/%s:generateContent" % (cfg["base_url"], cfg["model"]), hdr,
                            json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                        "generationConfig": {"responseModalities": ["IMAGE"]}}).encode("utf-8"))
    data = _json(status, body, "Google Gemini", cfg["api_key"])
    found = _find_image(data)
    if not found:
        said = _find_text(data)[:200]
        raise ImageGenError("Gemini did not return an image%s." % (": " + said if said else ""))
    raw = base64.b64decode(found[0])
    return {"bytes": raw, "mime": _sniff(raw, found[1] or "image/png"),
            "response_id": "", "usage": data.get("usage") or data.get("usageMetadata") or {}}


def _sniff(raw: bytes, fallback: str) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return fallback


# ── the front door ──────────────────────────────────────────────────────
def generate(cfg: dict, prompt: str, previous_id: str | None = None,
             http=None, sleep=time.sleep) -> dict:
    """Bytes + mime + (maybe) a response id to refine from. Every provider's
    failure comes back as ImageGenError with the key redacted.

    `http` defaults to this module's `http` at CALL time, not definition
    time, so a test can swap the network out with `imagegen.http = fake`."""
    if http is None:
        http = globals()["http"]
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageGenError("Describe the picture first.")
    if len(prompt) > MAX_PROMPT:
        raise ImageGenError("That description is over %d characters; shorter prompts "
                            "make better pictures anyway." % MAX_PROMPT)
    if previous_id and not cfg["refine"]:
        raise ImageGenError("%s cannot refine a previous picture - it starts fresh each time. "
                            "Meta is the provider that can." % PROVIDERS[cfg["provider"]]["label"])
    try:
        if cfg["provider"] == "meta":
            out = _meta(cfg, prompt, previous_id, http=http)
        elif cfg["provider"] == "elevenlabs":
            out = _elevenlabs(cfg, prompt, http=http, sleep=sleep)
        elif cfg["provider"] == "gemini":
            out = _gemini(cfg, prompt, http=http)
        else:
            out = _openai_images(cfg, prompt, http=http)
    except ImageGenError:
        raise
    except Exception as e:                    # a socket error, a bad decode...
        raise ImageGenError("%s: %s" % (type(e).__name__, redact(str(e)[:200], cfg["api_key"])))
    out["provider"] = cfg["provider"]
    out["model"] = cfg["model"]
    return out


def to_jpeg(raw: bytes, mime: str, quality: int = 92) -> bytes:
    """Instagram takes JPEG and nothing else, and Meta's own model answers in
    WebP. Converted here, once, before it ever reaches the media store."""
    if mime == "image/jpeg":
        return raw
    try:
        from PIL import Image
    except ImportError:
        raise ImageGenError("The picture came back as %s and Pillow is not installed to "
                            "convert it to JPEG (pip install pillow)." % mime)
    try:
        im = Image.open(io.BytesIO(raw))
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except ImageGenError:
        raise
    except Exception as e:
        raise ImageGenError("Could not convert the %s to JPEG: %s" % (mime, type(e).__name__))


# ── the business's own logo, stamped on ─────────────────────────────────
# Daniel: "upload the homerepair.tech logo so that Meta will use it in any
# image created." An image model asked to draw a logo redraws it - letters
# come out wrong, the house icon changes shape - and a customer notices a
# misspelled brand faster than anything else in the picture. So the real
# file is placed on the finished picture, pixel for pixel, every time.
LOGO_POSITIONS = ("br", "bl", "tr", "tl")


def stamp_text(jpeg: bytes, text: str, position: str = "bl", fonts_dir=None, quality: int = 92) -> bytes:
    """A short line - a phone number, a web address - in one corner of the
    picture, drawn by us in a real font on a soft dark pill, so it is never
    misspelled or half-melted the way a model's own lettering is. Daniel,
    2026-09-25: "we need to add 830-205-0202 to every post pic"."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    from pathlib import Path
    text = " ".join((text or "").split())
    if not text:
        return jpeg
    base = Image.open(io.BytesIO(jpeg)).convert("RGBA")
    W, H = base.size
    px = max(18, int(W * 0.042))
    font = None
    for d in ([fonts_dir] if fonts_dir else []) + [Path(__file__).parent / "static" / "fonts"]:
        try:
            font = ImageFont.truetype(str(Path(d) / "IBMPlexSans-SemiBold.ttf"), px)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(base)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    padx, pady = int(px * 0.7), int(px * 0.42)
    bw, bh = tw + 2 * padx, th + 2 * pady
    pad = max(12, int(W * 0.035))
    pos = position if position in LOGO_POSITIONS else "bl"
    x = pad if pos[1] == "l" else W - bw - pad
    y = pad if pos[0] == "t" else H - bh - pad
    pill = Image.new("RGBA", base.size, (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pd.rounded_rectangle([x, y, x + bw, y + bh], radius=bh // 2, fill=(10, 10, 10, 165))
    base = Image.alpha_composite(base, pill.filter(ImageFilter.GaussianBlur(0.6)))
    ImageDraw.Draw(base).text((x + padx - l, y + pady - t), text, font=font, fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    base.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def opposite_corner(position: str) -> str:
    """Where the tag line goes so it never sits on the logo: the other side
    of the same edge."""
    pos = position if position in LOGO_POSITIONS else "br"
    return pos[0] + ("l" if pos[1] == "r" else "r")


def stamp_logo(jpeg: bytes, logo: bytes, position: str = "br", quality: int = 92) -> bytes:
    """The picture with the logo in one corner. A logo with a transparent
    background sits straight on the picture; an opaque one (a JPEG, or a
    square app-icon PNG) gets rounded corners and a soft shadow so it reads
    as a badge rather than a pasted rectangle."""
    from PIL import Image, ImageDraw, ImageFilter
    base = Image.open(io.BytesIO(jpeg)).convert("RGB")
    mark = Image.open(io.BytesIO(logo)).convert("RGBA")
    W, H = base.size
    ratio = mark.width / max(1, mark.height)
    # a square icon reads at ~18% of the width; a long wordmark needs more
    target_w = int(W * (0.30 if ratio >= 2.2 else 0.22 if ratio >= 1.3 else 0.18))
    target_w = max(80, min(target_w, W // 2))
    target_h = max(1, int(target_w / ratio))
    mark = mark.resize((target_w, target_h), Image.LANCZOS)
    opaque = mark.getextrema()[3][0] >= 250          # no transparent pixels at all
    if opaque:
        r = max(6, int(min(target_w, target_h) * 0.14))
        rounded = Image.new("L", mark.size, 0)
        ImageDraw.Draw(rounded).rounded_rectangle([0, 0, target_w - 1, target_h - 1], r, fill=255)
        mark.putalpha(rounded)
    pad = max(12, int(W * 0.035))
    pos = position if position in LOGO_POSITIONS else "br"
    x = pad if pos[1] == "l" else W - target_w - pad
    y = pad if pos[0] == "t" else H - target_h - pad
    out = base.convert("RGBA")
    if opaque:
        shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
        sh = Image.new("RGBA", mark.size, (0, 0, 0, 110))
        sh.putalpha(mark.split()[-1].point(lambda a: int(a * 0.45)))
        shadow.paste(sh, (x + 3, y + 5), sh)
        out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(6)))
    out.paste(mark, (x, y), mark)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()
