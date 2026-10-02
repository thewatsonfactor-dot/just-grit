# -*- coding: utf-8 -*-
"""Competitor Watch, checked against what it actually showed on 2026-09-23.

Daniel: "check this is working correctly". What was wrong on screen:
  * "Going after" chips stitched words from opposite ends of a sentence -
    "pretty good what", "antonio austin", "four things most", and the
    Steal-this list repeated them as "phrases they target";
  * "They have 113 reviews to your 0" - we never looked up your listing;
  * the same problem twice on one card ("No analytics - ..." and "No
    analytics installed");
  * "A fast, clean site" next to "Slow server response";
  * a raw page title as the name, and Dallas / Austin shops in a San
    Antonio search with nothing saying so.
The fixture is the real titles and headings those four sites served.
"""
import json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-cw2-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import competitors as C
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

SEO = json.load(open("tests/fixtures/competitor_seo_2026-09-23.json"))
for v in SEO.values():
    v["ok"] = True
def kws(dom, name):
    return [k["phrase"] for k in C.keywords_from(SEO[dom], name, dom)]

JUNK = ["pretty good what", "antonio austin", "web development san", "four things most", "things most providers",
        "most providers cannot", "have expertise working", "expertise working range", "sized texas",
        "cybersecurity mid", "turn camera employee", "intelligent system time", "tools automation san"]
own = kws("thewatsonfactor.dev", "The Watson Factor")
seven = kws("7tech.com", "7tech - San Antonio Managed IT Services Company")
core = kws("corewebtechnologies.com", "Coreweb technologies llc | digital marketing agency | best website designing")
nxt = kws("nxtgenweb.com", "NXT GEN WEB")
every = own + seven + core + nxt
check("none of the stitched-together phrases from the screen come back", [j for j in JUNK if j in every], [])
check("yours: app development, AI tools, automation, custom software",
      all(p in own for p in ("app development", "ai tools", "automation", "custom software")))
check("7tech: managed IT services, cybersecurity", ("managed it services" in seven, "cybersecurity" in seven), (True, True))
check("Coreweb: video editing, app design, SEO company",
      all(p in core for p in ("video editing", "app design specialist", "seo company")))
check("NXT GEN WEB: digital marketing agency and web design (their brand has 'web' in it)",
      ("digital marketing agency" in nxt, "web design services" in nxt), (True, True))
check("  PPC management from their service headings", "ppc management" in nxt)
check("the city they target is its own chip, at the end",
      [k for k in C.keywords_from(SEO["nxtgenweb.com"], "NXT GEN WEB", "nxtgenweb.com") if k["where"] == ["place"]][0]["phrase"],
      "san antonio")
check("no brand names as keywords", any("7tech" in p or "coreweb" in p or "nxt" in p for p in every), False)
check("the Blanco Cafe case still works",
      C.keywords_from({"ok": True, "title": "Blanco Cafe | Breakfast Tacos & Catering in New Braunfels",
                       "h1s": ["Breakfast Tacos New Braunfels"], "h2s": []}, "Blanco Cafe", "blancocafe.com")[0]["phrase"],
      "breakfast tacos")

# reviews: never "your 0" when we don't know yours
comp = {"site_ok": True, "reviews": 113, "keywords": []}
steal = " ".join(C.steal_list({"reviews": None, "keywords": []}, comp))
check("listing unknown: says what they have, no made-up 'your 0'", ("113 Google reviews" in steal, "your 0" in steal), (True, False))
steal = " ".join(C.steal_list({"reviews": 12, "keywords": []}, comp))
check("listing known: the real comparison", "113 Google reviews to your 12" in steal)

# the same thing twice
p = C.profile({"rating": 5.0, "reviews": 30}, {"ok": True, "title": "x"},
              {"ok": True, "overall": 74, "grade": "C", "metrics": {"tel_links": 1},
               "stack": {"detected": {}},
               "priorities": [{"title": "No LocalBusiness structured data"}, {"title": "No analytics installed"},
                              {"title": "No street address on the homepage"}]}, name="Coreweb", domain="coreweb.com")
check("'No analytics' is said once, not twice", sum("analytics" in w.lower() for w in p["weak"]), 1)
check("  the other findings still come through", "No LocalBusiness structured data." in p["weak"])

p = C.profile({"rating": 5.0, "reviews": 30}, {"ok": True},
              {"ok": True, "overall": 93, "grade": "A", "metrics": {"load_seconds": 1.3}, "stack": {"detected": {}},
               "priorities": [{"title": "Slow server response — 1311 ms TTFB"}]}, name="N", domain="n.com")
check("an A site with a slow server is 'well-built', not 'fast'",
      [w for w in p["working"] if "site (" in w], ["A well-built site (A, 93)."])

p = C.profile({"rating": 5.0, "reviews": 30}, {"ok": True},
              {"ok": True, "overall": 93, "grade": "A", "metrics": {"load_seconds": 1.3, "tel_links": 1},
               "stack": {"detected": {"analytics": ["GA4"], "ads": ["Google Ads"]}},
               "priorities": [{"title": "Slow server response — 1311 ms TTFB"}, {"title": "39 of 42 images not lazy-loaded"}]},
              name="N", domain="n.com")
check("a slow server and lazy-loading are two different problems - both shown",
      p["weak"], ["Slow server response — 1311 ms TTFB.", "39 of 42 images not lazy-loaded."])

# names and places
check("page title → business name", W.display_company("Coreweb technologies llc | digital marketing agency | best website designing", "corewebtechnologies.com"),
      "Coreweb technologies llc")
check("town from a Google address", W.town_of("4802 Cambray Dr, San Antonio, TX 78229, USA"), "San Antonio, TX")
check("San Antonio is local", W.is_local_address("4802 Cambray Dr, San Antonio, TX 78229, USA"))
check("Cibolo is local (781xx)", W.is_local_address("123 Main St, Cibolo, TX 78108, USA"))
check("Dallas is not", W.is_local_address("1 Elm St, Dallas, TX 75201, USA"), False)
check("Austin is not", W.is_local_address("500 Congress Ave, Austin, TX 78701, USA"), False)
check("no address: don't hide it", W.is_local_address(""), True)

# the find list: local first, the rest marked
W.DATA = pathlib.Path(TMP); tok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.require_auth = lambda r: "local"; W.google_key = lambda: "AIzaFAKE"
W.set_setting("sender_site", "thewatsonfactor.dev")
W.places_search = lambda q, key, pages=1, region="us": [
    {"id": "a", "displayName": {"text": "iQlance Solutions - Mobile App Development Company Dallas"}, "websiteUri": "https://iqlance.com",
     "formattedAddress": "1 Elm St, Dallas, TX 75201, USA", "rating": 5, "userRatingCount": 23, "businessStatus": "OPERATIONAL"},
    {"id": "b", "displayName": {"text": "Coreweb technologies llc | digital marketing agency"}, "websiteUri": "https://corewebtechnologies.com",
     "formattedAddress": "4802 Cambray Dr, San Antonio, TX 78229, USA", "rating": 5, "userRatingCount": 30, "businessStatus": "OPERATIONAL"},
    {"id": "c", "displayName": {"text": "The Watson Factor"}, "websiteUri": "https://thewatsonfactor.dev",
     "formattedAddress": "3217 Wild Iris, New Braunfels, TX 78130, USA", "rating": 5, "userRatingCount": 9, "businessStatus": "OPERATIONAL"}]
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
r = W.competitors_find(Req(), W.CompetitorFindBody(query="app development San Antonio"))
names = [(x["name"], x["local"]) for x in r["candidates"]]
check("find: local first, clean names, Dallas marked outside", names,
      [("Coreweb technologies llc", True), ("iQlance Solutions", False)])
check("  and counted, so the page can hide them", r["outside"], 1)
check("  your own business is never listed as a competitor", any("Watson" in n for n, _ in names), False)

# your own listing, matched on your website
pl = W.own_place("thewatsonfactor.dev")
check("your own Google listing is found by your website", (pl.get("reviews"), pl.get("rating")), (9, 5))
W.places_search = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached"))
check("  and looked up once a week, not every read", W.own_place("thewatsonfactor.dev")["reviews"], 9)
W.set_setting("own_place", "")
W.places_search = lambda *a, **k: []
check("no listing links to your site: unknown, not zero", W.own_place("thewatsonfactor.dev").get("reviews"), None)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
