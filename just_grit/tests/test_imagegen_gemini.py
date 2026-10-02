# -*- coding: utf-8 -*-
"""Google Gemini as a picture provider - Daniel pays for Google AI Pro.

The subscription doesn't cover API calls; the key from AI Studio bills a
Google Cloud project (where AI Pro's $10/month credit lands). No network here.
"""
import base64, io, json, sys
sys.path.insert(0, '.')
import imagegen as G

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (64, 64), (200, 80, 20)).save(buf, "PNG")
PNG = buf.getvalue(); B64 = base64.b64encode(PNG).decode()
KEY = "AIzaSyFAKEKEY1234567890"

SEEN = []
def interactions_ok(method, url, headers, body, timeout=0):
    SEEN.append((url, headers, json.loads(body)))
    return 200, json.dumps({"interaction": {"id": "i1", "output_image": {"mime_type": "image/png", "data": B64}}}).encode()

cfg = G.config("gemini", KEY)
check("Gemini is a provider with the balanced model by default", (cfg["model"], cfg["refine"]), ("gemini-3.1-flash-image", False))
out = G.generate(cfg, "a bear holding a tablet in a clean garage", http=interactions_ok)
check("the picture comes back as bytes", (out["bytes"] == PNG, out["mime"]), (True, "image/png"))
url, hdr, body = SEEN[-1]
check("it calls the Interactions API", url, "https://generativelanguage.googleapis.com/v1beta/interactions")
check("  with the key in a header, never in the URL", (hdr.get("x-goog-api-key") == KEY, KEY in url), (True, False))
check("  and the prompt as text input", body["input"][0], {"type": "text", "text": "a bear holding a tablet in a clean garage"})

def old_api(method, url, headers, body, timeout=0):
    SEEN.append((url, headers, json.loads(body)))
    if url.endswith("/interactions"):
        return 404, b'{"error":{"code":404}}'
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": "here"}, {"inlineData": {"mimeType": "image/png", "data": B64}}]}}]}).encode()
out = G.generate(G.config("gemini", KEY, model="gemini-2.5-flash-image"), "x", http=old_api)
check("falls back to generateContent when Interactions isn't there", (out["bytes"] == PNG, SEEN[-1][0].endswith("models/gemini-2.5-flash-image:generateContent")), (True, True))

def refused(method, url, headers, body, timeout=0):
    return 200, json.dumps({"interaction": {"outputs": [{"type": "text", "text": "I can't make that picture."}]}}).encode()
try:
    G.generate(cfg, "x", http=refused); got = ""
except G.ImageGenError as e:
    got = str(e)
check("a refusal says what Gemini said", "I can't make that picture." in got)

def bad_key(method, url, headers, body, timeout=0):
    return 403, ('{"error":{"message":"API key %s not valid"}}' % KEY).encode()
try:
    G.generate(cfg, "x", http=bad_key); got = ""
except G.ImageGenError as e:
    got = str(e)
check("a bad key is explained, and the key never appears in the error", ("rejected the API key" in got, KEY in got), (True, False))
check("the setup steps say the subscription doesn't pay for this", any("does not pay" in s for s in G.PROVIDERS["gemini"]["setup"]))
check("refining is refused plainly (Gemini starts fresh)", True)
try:
    G.generate(cfg, "x", previous_id="abc", http=interactions_ok); got = ""
except G.ImageGenError as e:
    got = str(e)
check("  ...and says Meta is the one that can", "Meta is the provider that can" in got)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
