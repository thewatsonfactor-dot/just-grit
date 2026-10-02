# -*- coding: utf-8 -*-
"""Instagram gets the plain address; Facebook keeps the tracked link.
Daniel, 2026-09-24: "it's posted to IG but didn't include the link"."""
import os, sys, tempfile
os.environ["JUST_GRIT_DATA"] = tempfile.mkdtemp(prefix="jg-iglink-")
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

check("utm tags and https come off for Instagram",
      W.ig_link_text("https://homerepair.tech/checkyourhomescore/?utm_source=instagram&utm_medium=social"),
      "homerepair.tech/checkyourhomescore")
check("other query bits stay", W.ig_link_text("https://www.homerepair.tech/plans?ref=ig"), "homerepair.tech/plans?ref=ig")
check("a bare address is left alone", W.ig_link_text("homerepair.tech"), "homerepair.tech")

tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
W.set_setting("ig_user_id", "1784"); W.set_setting("fb_page_token", "tok")
W.public_base = lambda: ""
seen = {}
W.insta.stage = lambda ig, tok, **k: seen.update(k) or {"container_id": "c1"}
W.social_instagram = None
out = W.instagram_stage(Req(), W.IgStageBody(caption="Check your home score.", image_url="https://x.example/a.jpg",
                                             link="https://homerepair.tech/checkyourhomescore/"))
check("staging for Instagram puts the link at the end of the caption",
      seen["caption"], "Check your home score.\n\nhomerepair.tech/checkyourhomescore")
fb = {}
W.fb.publish = lambda page, tok, **k: fb.update(k) or {"id": "p1", "post_id": "1_2"}
W.set_setting("fb_page_id", "131")
r = W.facebook_publish(Req(), W.FbPublishBody(message="Check your home score.", image_url="https://x.example/a.jpg",
                                              link="https://homerepair.tech/checkyourhomescore/", publish_now=True))
check("Facebook gets the full tracked link in the words (a photo post has no link card)",
      ("utm_source=facebook" in fb["message"], fb["published"], fb["link"]), (True, True, ""))
ws.CURRENT.reset(tok)
print(); print("FAILS: %d" % fails); sys.exit(1 if fails else 0)
