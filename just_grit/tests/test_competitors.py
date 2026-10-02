# -*- coding: utf-8 -*-
"""Competitor watch.

"Check Google for the same products the end user is selling, pull up the
competitors, check their website, their SEO and the keywords they're
using, all the info we can find - so it's easier to sell against, and we
can find what's working for them and apply it."

Google finds them, the site readers read them, competitors.py says what it
means - and says only what the page can back up: the keywords are the
phrases in their own title and headings, never a claim about rankings.
"""
import json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-cw-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import competitors as C
import webapp as W, workspaces as ws
from contextlib import closing

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── what to search for ──────────────────────────────────────────────────
q = C.search_queries("custom software: AI Vision, ordering apps, websites, AI receptionist",
                     ["Ordering App", "AI Vision", "Reviews & Rewards"], "New Braunfels, TX")
check("one Google query per thing you sell, in your city",
      q[:3], ["Ordering App New Braunfels, TX", "AI Vision New Braunfels, TX", "Reviews & Rewards New Braunfels, TX"])
check("  the workspace's own 'sells' line fills in the rest", any("websites New Braunfels" in x for x in q), True)
check("  capped at six", len(q) <= 6, True)
check("nothing configured still searches something", C.search_queries("", [], "")[0], "small business services San Antonio, TX")

# ── the keywords they're going after ────────────────────────────────────
SEO = {"ok": True, "title": "Blanco Cafe | Breakfast Tacos & Catering in New Braunfels",
       "meta_desc": "Family owned breakfast tacos, catering for events, and daily lunch specials in New Braunfels.",
       "h1s": ["Breakfast Tacos New Braunfels"], "h2s": ["Catering for events", "Daily lunch specials", "Our story"],
       "sitemap_urls": 44, "noindex": False}
kws = C.keywords_from(SEO, name="Blanco Cafe", domain="blancocafe.com")
phrases = [k["phrase"] for k in kws]
check("phrases come from the title and headings", "breakfast tacos" in phrases[0] or "breakfast tacos" in " ".join(phrases[:2]), True)
check("  the brand's own name is not a keyword", any("blanco" in p for p in phrases), False)
check("  stopwords aren't", any(p.split()[0] in ("and", "in", "for", "the") for p in phrases), False)
check("  each says where it was found", "title" in kws[0]["where"] or "h1" in kws[0]["where"], True)
check("a site that didn't load has no keywords", C.keywords_from({"ok": False}), [])

# ── the profile, and what it says ───────────────────────────────────────
ANALYSIS = {"ok": True, "overall": 84, "grade": "B", "metrics": {"load_seconds": 1.4, "page_weight_kb": 900, "tel_links": 2},
            "stack": {"cms": "Squarespace", "detected": {"analytics": ["GA4"], "ads": ["Meta Pixel"], "booking": ["Resy"]},
                      "has_analytics": True, "has_retargeting": True},
            "priorities": [{"title": "No alt text on 12 images"}]}
p = C.profile({"rating": 4.7, "reviews": 312}, SEO, ANALYSIS, name="Blanco Cafe", domain="blancocafe.com")
check("the profile reads the stack", (p["cms"], p["ads"], p["booking"]), ("Squarespace", ["Meta Pixel"], ["Resy"]))
check("'working for them' names the review engine", any("312 reviews" in x for x in p["working"]), True)
check("  and the ads", any("Running ads" in x for x in p["working"]), True)
check("  and the booking", any("Online booking" in x for x in p["working"]), True)
check("  and the phrases they go after", any("go after" in x for x in p["working"]), True)
check("'sell against' does not invent a weakness for a tight ship",
      any("No retargeting" in x or "Slow site" in x for x in p["weak"]), False)
check("  but does carry the analyzer's top issue", any("alt text" in x for x in p["weak"]), True)

WEAK = {"ok": True, "overall": 41, "grade": "D", "metrics": {"load_seconds": 6.2, "page_weight_kb": 5000, "tel_links": 0},
        "stack": {"cms": None, "detected": {}, "has_analytics": False, "has_retargeting": False}, "priorities": []}
w = C.profile({"rating": 3.6, "reviews": 14}, {"ok": True, "title": "Home", "h1s": [], "h2s": [], "meta_desc": ""}, WEAK,
              name="Slow Joe's", domain="slowjoes.com")
check("a weak competitor: slow, no pixel, no analytics, no tap-to-call, few reviews",
      (any("Slow site" in x for x in w["weak"]), any("No retargeting" in x for x in w["weak"]),
       any("No analytics" in x for x in w["weak"]), any("tap-to-call" in x for x in w["weak"]),
       any("Only 14" in x for x in w["weak"])), (True, True, True, True, True))
check("  and nothing is 'working for them'", w["working"], [])
down = C.profile({"rating": 4.0, "reviews": 50}, {"ok": False}, {"ok": False, "error": "timeout"}, name="Gone", domain="gone.com")
check("a site that won't load is its own sell-against line", "didn't load" in down["weak"][0], True)

# ── steal this: what they do that you don't ─────────────────────────────
own = C.profile({"rating": None, "reviews": 20}, {"ok": True, "title": "The Watson Factor - software", "h1s": ["Custom software"], "h2s": [], "meta_desc": "", "sitemap_urls": 6},
                {"ok": True, "overall": 60, "grade": "C", "metrics": {"load_seconds": 2.0, "tel_links": 1},
                 "stack": {"cms": None, "detected": {"analytics": ["GA4"]}, "has_analytics": True, "has_retargeting": False}, "priorities": []},
                name="The Watson Factor", domain="thewatsonfactor.dev")
steal = C.steal_list(own, p)
check("steal: they run ads with a pixel and you don't", any("retargeting" in x for x in steal), True)
check("  they take bookings and you don't", any("bookings" in x for x in steal), True)
check("  they out-review you", any("312 Google reviews to your 20" in x for x in steal), True)
check("  they publish more pages", any("44 pages to your 6" in x for x in steal), True)
check("  they target phrases you don't", any("Phrases they target" in x for x in steal), True)
check("  their site scores higher", any("84 to your 60" in x for x in steal), True)
check("nothing to steal from a site that didn't load", C.steal_list(own, down), [])
check("no own site, no comparison", C.steal_list({}, p), [])

# ── what changed ────────────────────────────────────────────────────────
prev = dict(p); cur = dict(p, reviews=330, rating=4.5, title="Blanco Cafe | Now open for dinner", h1s=["Dinner in New Braunfels"], chat=["Intercom"], sitemap_urls=60, score=70)
ch = C.changes(prev, cur)
check("changes: reviews, rating, score, title, headline, chat, pages",
      ("+18 Google reviews" in ch, "rating 4.7 → 4.5" in ch, "site score 84 → 70" in ch,
       any(x.startswith("new title") for x in ch), any(x.startswith("new headline") for x in ch),
       "added live chat" in ch, "+16 pages on the site" in ch), (True,) * 7)
check("no change, no noise", C.changes(p, dict(p, reviews=p["reviews"] + 2)), [])
check("a site going down is called out", "site is DOWN" in C.changes(p, dict(p, site_ok=False)), True)

# ── the endpoints, with the network stubbed ─────────────────────────────
W.DATA = pathlib.Path(TMP)
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("sender_site", "https://www.thewatsonfactor.dev")
W.set_setting("default_city", "New Braunfels, TX")
W.google_key = lambda: "AIzaFAKE"
SITES = {"thewatsonfactor.dev": ({"ok": True, "title": "The Watson Factor - software", "h1s": ["Custom software"], "h2s": [], "meta_desc": "", "sitemap_urls": 6},
                                 {"ok": True, "overall": 60, "grade": "C", "metrics": {"load_seconds": 2.0, "tel_links": 1},
                                  "stack": {"cms": None, "detected": {"analytics": ["GA4"]}, "has_analytics": True, "has_retargeting": False}, "priorities": []}),
         "blancocafe.com": (SEO, ANALYSIS)}
W.read_site = lambda dom: SITES.get(dom, ({"ok": False}, {"ok": False, "error": "unreachable"}))
W.places_search = lambda q, key, pages=1, region="us": [
    {"id": "g1", "displayName": {"text": "Blanco Cafe"}, "websiteUri": "https://blancocafe.com", "rating": 4.7, "userRatingCount": 312, "businessStatus": "OPERATIONAL", "formattedAddress": "1 Main"},
    {"id": "g2", "displayName": {"text": "The Watson Factor"}, "websiteUri": "https://www.thewatsonfactor.dev", "rating": 5, "userRatingCount": 3, "businessStatus": "OPERATIONAL"},
    {"id": "g3", "displayName": {"text": "Closed Co"}, "websiteUri": "", "businessStatus": "CLOSED_PERMANENTLY"},
    {"id": "g4", "displayName": {"text": "No Site Grill"}, "rating": 4.1, "userRatingCount": 40, "businessStatus": "OPERATIONAL"}]

lst = W.competitors_list(Req())
check("the list knows your own site", lst["own_domain"], "thewatsonfactor.dev")
check("  and read it", lst["own"]["score"], 60)
check("  and offers the searches it would run", "New Braunfels, TX" in lst["queries"][0], True)
check("  nobody watched yet", lst["competitors"], [])

f = W.competitors_find(Req(), W.CompetitorFindBody(query=""))
names = [c["name"] for c in f["candidates"]]
check("find returns Google's businesses", "Blanco Cafe" in names and "No Site Grill" in names, True)
check("  never yourself", "The Watson Factor" in names, False)
check("  never a closed business", "Closed Co" in names, False)
check("  most-reviewed first", names[0], "Blanco Cafe")
check("  nothing saved by finding", W.competitors_list(Req())["competitors"], [])

cand = next(c for c in f["candidates"] if c["name"] == "Blanco Cafe")
card = W.competitors_track(Req(), W.CompetitorTrackBody(**{k: cand[k] for k in ("place_id", "name", "domain", "phone", "address", "rating", "reviews")}))
check("Watch reads their site at once", card["profile"]["score"], 84)
check("  with the phrases they go after", any("breakfast tacos" in k["phrase"] for k in card["profile"]["keywords"]), True)
check("  what's working for them", len(card["profile"]["working"]) >= 3, True)
# your own listing (g2 links to your site, 3 reviews) is found, so the
# comparison uses your real count - it used to say "your 0" for everyone
check("  and what to steal, compared to YOUR site", any("312 Google reviews to your 3" in x for x in card["steal"]), True)
check("  nothing changed on a first read", card["changes"], [])
try:
    W.competitors_track(Req(), W.CompetitorTrackBody(**{k: cand[k] for k in ("place_id", "name", "domain", "phone", "address", "rating", "reviews")})); got = "allowed"
except Exception as e:
    got = getattr(e, "status_code", None)
check("watching the same one twice is refused", got, 400)
f2 = W.competitors_find(Req(), W.CompetitorFindBody(query="tacos"))
check("find now marks them as watched", next(c for c in f2["candidates"] if c["name"] == "Blanco Cafe")["tracked"], True)

# their site changes; the loop's rescan notices
SITES["blancocafe.com"] = (dict(SEO, title="Blanco Cafe | Now open for dinner"), dict(ANALYSIS, overall=70))
with closing(W.db()) as c:
    c.execute("UPDATE competitors SET last_scan_at='2026-09-01T00:00:00+00:00'"); c.commit()
check("the scheduled pass re-reads anyone older than the window", W.competitors_due(ws.PRIMARY), 1)
card = W.competitors_list(Req())["competitors"][0]
check("  and the card says what changed", ("site score 84 → 70" in card["changes"], any(x.startswith("new title") for x in card["changes"])), (True, True))
check("  a fresh read is not re-read", W.competitors_due(ws.PRIMARY), 0)
check("  unless forced", W.competitors_due(ws.PRIMARY, force=True), 1)

# a competitor with no website still gets a card
nos = next(c for c in f["candidates"] if c["name"] == "No Site Grill")
card = W.competitors_track(Req(), W.CompetitorTrackBody(**{k: nos.get(k) or ("" if k != "reviews" else 0) for k in ("place_id", "name", "domain", "phone", "address", "rating", "reviews")}))
check("no website: the card still has Google's numbers and a sell-against line",
      (card["reviews"], "didn't load" in (card["profile"]["weak"] or [""])[0]), (40, True))

W.competitors_remove(Req(), card["id"])
check("stop watching takes them off the list", [c["name"] for c in W.competitors_list(Req())["competitors"]], ["Blanco Cafe"])

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
