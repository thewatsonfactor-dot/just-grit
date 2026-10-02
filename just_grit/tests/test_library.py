# -*- coding: utf-8 -*-
"""The picture library, and Meta getting let in.

Daniel: "some of the other platforms have you store the marketing material
on like a Google Drive so it can pull the material from there and make
posts" - and, on where it lives: "the customer media should be saved on
their computer, external drive or a cloud service". So: originals stay put,
the app copies what it is handed, from a file or a link, without a Google
sign-in. And the public picture URL moves to /welcome/m/, the prefix
Cloudflare Access already lets through, so Meta's fetcher stops getting a
sign-in page - "oh but let's let Meta in".
"""
import io, json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-lib-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import library as L
import media as M
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── links are recognised for what they are ──────────────────────────────
check("a Drive folder link", L.classify("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing"),
      ("drive_folder", "1AbCdEfGhIjKlMnOp"))
check("  the /u/0/ variant too", L.classify("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOp"),
      ("drive_folder", "1AbCdEfGhIjKlMnOp"))
check("a Drive file link", L.classify("https://drive.google.com/file/d/1ZyXwVuTsRqPoNm/view?usp=drive_link"),
      ("drive_file", "1ZyXwVuTsRqPoNm"))
check("a Dropbox link", L.classify("https://www.dropbox.com/scl/fi/abc/photo.jpg?rlkey=x&dl=0")[0], "dropbox")
check("a plain picture address", L.classify("https://blancocafe.com/img/patio.jpg"), ("url", "https://blancocafe.com/img/patio.jpg"))
try:
    L.classify("C:\\Users\\me\\Pictures\\logo.png"); got = "allowed"
except L.LibraryError as e:
    got = "https://" in str(e)
check("a file path is refused with a hint, not a traceback", got, True)

check("Dropbox's share page becomes the file", L.dropbox_direct("https://www.dropbox.com/s/abc/photo.jpg?dl=0"),
      "https://www.dropbox.com/s/abc/photo.jpg?dl=1")

# ── the embedded folder view is parsed ──────────────────────────────────
HTML = """<html><body><div class="flip-entries">
<div class="flip-entry" id="entry-1Photo111111"><div class="flip-entry-thumb"></div><div class="flip-entry-info"><div class="flip-entry-title">Patio at dusk.jpg</div></div></div>
<div class="flip-entry" id="entry-1Doc2222222"><div class="flip-entry-info"><div class="flip-entry-title">Menu Fall 2026.pdf</div></div></div>
<div class="flip-entry" id="entry-1Logo333333"><div class="flip-entry-info"><div class="flip-entry-title">Logo &amp; mark.PNG</div></div></div>
</div></body></html>"""
entries = L.parse_drive_folder_html(HTML)
check("three entries found", [e["id"] for e in entries], ["1Photo111111", "1Doc2222222", "1Logo333333"])
check("  names decoded", entries[2]["name"], "Logo & mark.PNG")

# ── listing a folder without a Google key: the HTML view, pictures only ─
calls = []
def http_folder(url):
    calls.append(url)
    if "embeddedfolderview" in url:
        return 200, {"Content-Type": "text/html"}, HTML.encode()
    return 404, {}, b""
r = L.list_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", http_folder, google_key="")
check("without a key the folder is read from Google's public HTML view", "embeddedfolderview" in calls[0], True)
check("  only the pictures are listed - the PDF is not", [i["name"] for i in r["items"]],
      ["Patio at dusk.jpg", "Logo & mark.PNG"])
check("  each has a download address", all(i["download"].startswith("https://drive.google.com/uc?export=download&id=") for i in r["items"]), True)

# with a key: the Drive API first, HTML as the fallback
def http_api(url):
    if "googleapis.com/drive/v3/files?" in url:
        return 200, {"Content-Type": "application/json"}, json.dumps({"files": [
            {"id": "A1", "name": "truck.jpg", "mimeType": "image/jpeg"},
            {"id": "A2", "name": "notes.txt", "mimeType": "text/plain"},
            {"id": "A3", "name": "crew.HEIC", "mimeType": "image/heic"}]}).encode()
    return 500, {}, b""
r = L.list_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", http_api, google_key="AIzaKEY")
check("with a Maps key the Drive API is used and JPEG/PNG are kept",
      [i["name"] for i in r["items"]], ["truck.jpg"])
check("  and the download goes through the API too", "alt=media" in r["items"][0]["download"], True)

def http_api_off(url):
    if "googleapis.com" in url:
        return 403, {"Content-Type": "application/json"}, b'{"error":{"status":"PERMISSION_DENIED"}}'
    return http_folder(url)
r = L.list_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", http_api_off, google_key="AIzaKEY")
check("a key without the Drive API enabled falls back to the HTML view", len(r["items"]), 2)

def http_private(url):
    return 200, {"Content-Type": "text/html"}, b"<html><body>Sign in</body></html>"
r = L.list_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", http_private, "")
check("a folder that isn't shared says so", "Anyone with the link" in r["note"], True)

# a folder with too many pictures is capped, and says so
BIG = "".join('<div class="flip-entry" id="entry-1F%04d"><div class="flip-entry-title">p%d.jpg</div></div>' % (i, i)
              for i in range(90))
r = L.list_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp",
                lambda u: (200, {"Content-Type": "text/html"}, BIG.encode()), "")
check("a 90-picture folder is capped at %d" % L.MAX_FILES, len(r["items"]), L.MAX_FILES)
check("  and the note says how many there were", "90" in r["note"], True)

# ── fetching one picture: HTML is never a picture ───────────────────────
from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (64, 48), (200, 90, 30)).save(buf, "JPEG"); JPG = buf.getvalue()
check("a real JPEG comes through", L.fetch_image("https://x/p.jpg", lambda u: (200, {"Content-Type": "image/jpeg"}, JPG)), JPG)
for status in (401, 403, 404):
    try:
        L.fetch_image("https://x/p.jpg", lambda u: (status, {}, b"")); got = "allowed"
    except L.LibraryError as e:
        got = "Anyone with the link" in str(e)
    check("  a %d says 'share it'" % status, got, True)
try:
    L.fetch_image("https://x/p.jpg", lambda u: (200, {"Content-Type": "text/html"}, b"<!DOCTYPE html><html>virus scan</html>")); got = "allowed"
except L.LibraryError as e:
    got = "web page" in str(e)
check("  Google's 'can't scan this' page is caught, not stored as a picture", got, True)

# ── Meta is let in: the public path is the bypassed one ─────────────────
check("public picture URLs live under /welcome/m/",
      M.public_url("https://justgrit.thewatsonfactor.dev", "ab.jpg"),
      "https://justgrit.thewatsonfactor.dev/welcome/m/ab.jpg")
seen = []
M.hosting_problem("https://x.dev", lambda u: (seen.append(u), (404, ""))[1])
check("  the hosting probe checks that same path", "/welcome/m/" in seen[0], True)

# ── the endpoints ───────────────────────────────────────────────────────
W.DATA = pathlib.Path(TMP)
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
W.public_base = lambda: "https://justgrit.thewatsonfactor.dev"

import asyncio
class Up:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
    def __init__(self, raw): self._raw = raw
    async def body(self): return self._raw

check("the library starts empty", W.library_list(Req())["rows"], [])
r = asyncio.get_event_loop().run_until_complete(W.library_upload(Up(JPG), name="Patio at dusk.jpg"))
check("a picture from the computer lands in the library", r["name"], "Patio at dusk.jpg")
check("  with a URL Meta can fetch", r["url"].startswith("https://justgrit.thewatsonfactor.dev/welcome/m/"), True)
check("  and a local thumbnail path for the app", r["local"].startswith("/media/"), True)
check("  and its size", (r["width"], r["height"]), (64, 48))
r2 = asyncio.get_event_loop().run_until_complete(W.library_upload(Up(JPG), name="same photo again.jpg"))
check("the same bytes again is the same row, not a duplicate", r2["id"], r["id"])
check("  the library has one picture", len(W.library_list(Req())["rows"]), 1)

try:
    asyncio.get_event_loop().run_until_complete(W.library_upload(Up(b"%PDF-1.4 not a picture"), name="menu.pdf")); got = "allowed"
except Exception as e:
    got = getattr(e, "status_code", None)
check("a PDF is refused with a 400", got, 400)

# the served file, on both paths
resp = W.serve_media(r["media_id"])
check("/media/<id> serves it", getattr(resp, "media_type", ""), "image/jpeg")
import inspect
routes = {rt.path for rt in W.app.routes}
check("/welcome/m/<id> is a real route too", "/welcome/m/{name}" in routes, True)

# from a link
W._http3 = lambda url, timeout=30: ((200, {"Content-Type": "text/html"}, HTML.encode()) if "embeddedfolderview" in url
                                    else (200, {"Content-Type": "image/jpeg"}, JPG) if "1Photo111111" in url
                                    else (200, {"Content-Type": "text/html"}, b"<html>Sign in</html>"))
W.google_key = lambda: ""
r = W.library_from_link(Req(), W.LibraryLinkBody(link="https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp"))
check("a Drive folder link pulls the pictures in", [a["name"] for a in r["added"]], ["Patio at dusk.jpg"])
check("  the one that wouldn't download is named, not silently dropped",
      [f["name"] for f in r["failed"]], ["Logo & mark.PNG"])
check("  a picture already in the library from a laptop is still one row", len(W.library_list(Req())["rows"]), 1)

# Setup counts your own photos as 'pictures done'
by = {s["key"]: s for s in W.setup_status(Req())["sections"]}
check("Setup's Pictures section is done with your own photos and no image provider", by["pictures"]["state"], "done")

# remove
W.library_remove(Req(), r["added"][0]["id"])
check("removing takes it out of the library", W.library_list(Req())["rows"], [])
check("  but the file is still served - a scheduled post may point at it",
      getattr(W.serve_media(r["added"][0]["media_id"]), "media_type", ""), "image/jpeg")

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
