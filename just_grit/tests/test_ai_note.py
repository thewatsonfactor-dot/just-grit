# -*- coding: utf-8 -*-
""""Picture created with AI." - a disclosure line you can switch on and off.

Daniel, 2026-09-24, after a DM offered to "bypass the AI flag": no hiding,
but a plain line on AI pictures - "as long as it can be turned off and on".
No network.
"""
import io, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-ainote-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
from PIL import Image
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

def pic(color):
    b = io.BytesIO(); Image.new("RGB", (600, 600), color).save(b, "JPEG")
    return W.mstore.save(b.getvalue(), W.DATA)["id"]
made, photo = pic((10, 20, 30)), pic((200, 190, 180))
with W.closing(W.db()) as c:
    c.execute("INSERT INTO imagegen_log (provider, model, prompt, media_id, ok, at) VALUES ('meta','m','a tech',?,1,?)",
              (made, W.now()))
    c.commit()

check("on by default, with a plain line", W.ai_note_setting(), {"on": True, "text": "Picture created with AI."})
check("a picture made in the picture box is known to be AI",
      W.social_picture_info(Req(), "https://homerepair.thewatsonfactor.dev/welcome/m/" + made)["ai"], True)
check("a real photo from the library is not", W.social_picture_info(Req(), "/media/" + photo)["ai"], False)
check("a picture from elsewhere isn't guessed at", W.social_picture_info(Req(), "https://example.com/x.jpg")["ai"], False)
with W.closing(W.db()) as c:
    c.execute("INSERT INTO imagegen_log (provider, model, prompt, media_id, ok, error, at) VALUES ('meta','m','x',?,0,'boom',?)",
              (photo, W.now()))
    c.commit()
check("a failed attempt doesn't make a photo 'AI'", W.picture_is_ai("/media/" + photo), False)

r = W.social_ai_note(Req(), W.AiNoteBody(on=False))
check("it can be switched off", r["on"], False)
check("  and the list tells the page", W.social_campaigns(Req())["ai_note"]["on"], False)
r = W.social_ai_note(Req(), W.AiNoteBody(on=True, text="Image made with AI - real crew, real work."))
check("back on, with your own wording", r, {"on": True, "text": "Image made with AI - real crew, real work."})
r = W.social_ai_note(Req(), W.AiNoteBody(text="   "))
check("blank wording falls back to the default line", r["text"], "Picture created with AI.")

t = ws.CURRENT.set("watson")
check("each business has its own switch", W.ai_note_setting()["on"], True)
W.social_ai_note(Req(), W.AiNoteBody(on=False))
ws.CURRENT.reset(t)
check("  switching Watson off leaves HomeRepair on", W.ai_note_setting()["on"], True)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
