# -*- coding: utf-8 -*-
"""Your logo on every picture.

Daniel: "upload the homerepair.tech logo so that Meta will use it in any
image created." An image model redraws a logo (and misspells it), so the
real file is stamped on the finished picture instead. No network.
"""
import io, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-logo-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
from PIL import Image, ImageDraw
import imagegen as G
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

def jpeg(w=1024, h=1024, color=(40, 90, 60)):
    b = io.BytesIO(); Image.new("RGB", (w, h), color).save(b, "JPEG"); return b.getvalue()
def logo_png(opaque=False):
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0) if not opaque else (255, 255, 255, 255))
    d = ImageDraw.Draw(im); d.rounded_rectangle([40, 40, 360, 360], 50, fill=(243, 146, 0, 255))
    d.polygon([(120, 220), (200, 140), (280, 220), (280, 300), (120, 300)], fill=(30, 30, 30, 255))
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

out = G.stamp_logo(jpeg(), logo_png(), "br")
im = Image.open(io.BytesIO(out)).convert("RGB")
check("the stamped picture is still a JPEG the same size", (out[:3] == b"\xff\xd8\xff", im.size), (True, (1024, 1024)))
check("the logo lands in the bottom-right corner", im.getpixel((835, 835))[0] > 200)
check("  and the rest of the picture is untouched", im.getpixel((200, 200)), Image.open(io.BytesIO(jpeg())).convert("RGB").getpixel((200, 200)))
tl = Image.open(io.BytesIO(G.stamp_logo(jpeg(), logo_png(), "tl"))).convert("RGB")
check("top-left works too", tl.getpixel((65, 65))[0] > 200)
check("an opaque logo (JPEG-style) still stamps, as a badge",
      Image.open(io.BytesIO(G.stamp_logo(jpeg(), logo_png(opaque=True), "br"))).size, (1024, 1024))
wide = Image.new("RGBA", (900, 200), (243, 146, 0, 255)); b = io.BytesIO(); wide.save(b, "PNG")
w_im = Image.open(io.BytesIO(G.stamp_logo(jpeg(), b.getvalue(), "br"))).convert("RGB")
check("a long wordmark gets more width than a square icon", w_im.getpixel((1024 - 36 - 290, 1024 - 36 - 30))[0] > 200)
open(os.path.join(TMP, "sample.jpg"), "wb").write(out)

# the route
W.DATA = pathlib.Path(TMP); tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
    def __init__(self, body=b""): self._b = body
    async def body(self): return self._b
import asyncio
b = asyncio.get_event_loop().run_until_complete(W.brand_logo_upload(Req(logo_png())))
check("uploading a logo saves it and switches it on", (b["logo"].startswith("/media/"), b["on"], b["position"]), (True, True, "br"))
try:
    asyncio.get_event_loop().run_until_complete(W.brand_logo_upload(Req(b"not a picture")))
    got = ""
except W.HTTPException as e:
    got = str(e.detail)
check("anything that isn't a picture is refused", bool(got))

W.set_setting("imagegen_provider", "gemini"); W.set_setting("imagegen_api_key", "AIzaFAKEFAKEFAKEFAKE")
G.generate = lambda cfg, prompt, previous_id=None, **k: {"bytes": jpeg(), "mime": "image/jpeg", "response_id": "", "provider": "gemini", "model": "m", "usage": {}}
r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a technician at an AC unit"))
made = Image.open(io.BytesIO((pathlib.Path(TMP) / "media" / r["id"]).read_bytes())).convert("RGB")
check("a made picture comes back with the logo on it", (r["logo"], made.getpixel((835, 835))[0] > 200), (True, True))
W.brand_settings(Req(), W.BrandBody(on=False))
r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a technician at an AC unit, dusk"))
check("switched off: no logo", r["logo"], False)
W.brand_settings(Req(), W.BrandBody(on=True, position="tl"))
check("the corner is remembered", W.brand_get(Req())["position"], "tl")
r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a technician at an AC unit", use_logo=False))
check("one picture can skip it", r["logo"], False)
W.brand_logo_remove(Req())
check("remove clears it", (W.brand_get(Req())["logo"], W.brand_get(Req())["on"]), ("", False))
tok2 = ws.CURRENT.set("watson")
check("each business has its own logo (Watson Factor has none)", W.brand_get(Req())["logo"], "")
ws.CURRENT.reset(tok2)

# ── the line on every picture ──
# Daniel, 2026-09-25: "we need to add 830-205-0202 to every post pic."
plain = jpeg()
tagged = G.stamp_text(plain, "(830) 205-0202", "bl")
t_im = Image.open(io.BytesIO(tagged)).convert("RGB")
check("the tag line is drawn in the bottom-left: a dark pill with white lettering",
      (t_im.size, t_im.getpixel((60, 990))[0] < 60 or t_im.getpixel((60, 990))[0] > 200, abs(t_im.getpixel((512, 300))[0] - 40) < 4), ((1024, 1024), True, True))
check("  a blank line changes nothing", G.stamp_text(plain, "   "), plain)
check("  it sits opposite the logo", (G.opposite_corner("br"), G.opposite_corner("tl"), G.opposite_corner("bl")), ("bl", "tr", "br"))
W.brand_settings(Req(), W.BrandBody(tag="(830) 205-0202"))
check("the line saves and is on by default", (W.brand_get(Req())["tag"], W.brand_get(Req())["tag_on"]), ("(830) 205-0202", True))
r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a technician at an AC unit"))
made = Image.open(io.BytesIO((pathlib.Path(TMP) / "media" / r["id"]).read_bytes())).convert("RGB")
corner = made.crop((0, 940, 400, 1024)).getextrema()
check("a made picture carries it (no logo now, so bottom-left)", corner[0][1] > 200 and corner[0][0] < 60)
W.brand_settings(Req(), W.BrandBody(tag_on=False))
r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a technician at an AC unit, dusk"))
made = Image.open(io.BytesIO((pathlib.Path(TMP) / "media" / r["id"]).read_bytes())).convert("RGB")
check("  switched off: plain picture", made.crop((0, 940, 400, 1024)).getextrema()[0][1] < 100)
W.brand_settings(Req(), W.BrandBody(tag_on=True, position="br"))
asyncio.get_event_loop().run_until_complete(W.brand_logo_upload(Req(logo_png())))
r = W.imagegen_make(Req(), W.ImageGenBody(prompt="a technician at an AC unit"))
made = Image.open(io.BytesIO((pathlib.Path(TMP) / "media" / r["id"]).read_bytes())).convert("RGB")
check("with the logo bottom-right, the line goes bottom-left - both on the picture",
      (made.getpixel((835, 835))[0] > 200, made.crop((0, 940, 400, 1024)).getextrema()[0][1] > 200), (True, True))
tok3 = ws.CURRENT.set("watson")
check("the line is per business (Watson Factor has none)", W.brand_get(Req())["tag"], "")
ws.CURRENT.reset(tok3)

# ── the real logo on the site-check report and PDF ──
# Daniel, 2026-09-26: "the real logo needs to be put on the PDF going out
# through the system." The HomeRepair logo is uploaded above; a report from
# this workspace carries it, with the business name and phone under it.
from grit_analyzer.report import render_report
W.set_setting("sender_company", "HomeRepair Tech"); W.set_setting("sender_phone", "(830) 205-0202"); W.set_setting("sender_site", "homerepair.tech")
rb = W.report_brand()
check("the report header gets the uploaded logo as an embedded image", rb["logo"].startswith("data:image/png;base64,"))
check("  with the business name and contact line", (rb["brand"], rb["contact"]), ("HomeRepair Tech", "(830) 205-0202 · homerepair.tech"))
D = {"ok": True, "host": "x.com", "overall": 74, "grade": "C", "verdict": "v", "scores": {}, "metrics": {"load_seconds": 1.0, "ttfb_ms": 100, "page_weight_kb": 100, "requests_declared": 1, "render_blocking": 0, "tel_links": 0},
     "findings": [], "stack": {"cms": "", "detected": {}}, "measurement_note": ""}
html = render_report(D, **rb)
check("  the page shows it instead of the JG mark", ('<img class="logo" src="data:image/png' in html, '<div class="mark">JG</div>' in html, "HomeRepair Tech" in html), (True, False, True))
W.brand_logo_remove(Req())
html = render_report(D, **W.report_brand())
check("  no logo uploaded: the JG mark, as before", '<div class="mark">JG</div>' in html)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
