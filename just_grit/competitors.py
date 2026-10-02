# -*- coding: utf-8 -*-
"""Competitor watch: who else sells what you sell, and what they're doing.

Daniel: "check Google for the same products the end user is selling, pull
up the competitors, check their website, their SEO and the keywords they're
using, all the info we can find - so it's easier to sell against, and we
can find what's working for them and apply it to what we're doing."

What this does, honestly:
  * finds competitors with the same Google Places search the lead finder
    uses, from what the workspace sells and where it is;
  * reads each one's site with the analyzer (score, speed, stack: ads
    pixels, analytics, chat, booking) and the SEO reader (title, meta,
    headings, sitemap);
  * pulls the KEYWORDS THEY ARE GOING AFTER from their own titles and
    headings - that is what a business puts in a title tag on purpose.
    This is not Google rankings; that needs a paid rank-tracking API and
    saying "ranking" for a title-tag phrase would be a lie;
  * compares each one to YOUR site and says what they do that you don't
    ("steal this"), what you do that they don't ("sell against"), and
    what changed since the last look.

Pure functions over dicts; the network lives in webapp.
"""
from __future__ import annotations

import re
from collections import Counter

STOP = set("""a an the and or of to for in on at by with from your you our we us is are be
this that it its as into near best top local free new get call today now online more all
one every any about services service company companies llc inc co home page contact
located since welcome official site website serving proudly family owned operated""".split())

MAX_TRACKED = 12


def search_queries(sells: str, offers: list, city: str) -> list:
    """What to type into Google to find the people you compete with.
    The workspace's own words, one query per thing it sells, in its city."""
    city = (city or "").strip() or "San Antonio, TX"
    terms = []
    for o in offers or []:
        o = re.sub(r"\s*\(.*?\)\s*", " ", str(o)).strip()
        if o and o.lower() not in [t.lower() for t in terms]:
            terms.append(o)
    for part in re.split(r"[,:;/]|\band\b", sells or ""):
        part = part.strip()
        if 3 <= len(part) <= 40 and part.lower() not in [t.lower() for t in terms]:
            terms.append(part)
    if not terms:
        terms = [sells.strip() or "small business services"]
    return ["%s %s" % (t, city) for t in terms[:6]]


def brand_phrases(name: str, domain: str) -> list:
    """The business's own name as it's written on its site: the full name
    minus company suffixes, and the domain's stem ("7tech", "nxtgenweb").
    Removed as whole phrases - dropping the single word "web" because the
    brand is NXT GEN WEB threw away "web design"."""
    out = []
    nm = re.sub(r"\b(llc|inc|co|ltd|pllc|corp)\b\.?", "", (name or "").split("|")[0], flags=re.I)
    nm = re.split(r"\s[-–—]\s", nm)[0].strip(" ,.")
    if nm and len(nm) >= 3:
        out.append(nm)
    stem = (domain or "").split(".")[0].lower()
    if stem and len(stem) >= 3 and stem.lower() != nm.lower().replace(" ", ""):
        out.append(stem)
    return sorted(out, key=len, reverse=True)


def brand_words(name: str, domain: str) -> set:
    w = set(re.findall(r"[a-z0-9]+", (name or "").lower()))
    w |= set(re.findall(r"[a-z0-9]+", (domain or "").split(".")[0].lower()))
    return w


# ── keywords ─────────────────────────────────────────────────────────────
# The first version dropped stopwords and then took every 2- and 3-word run
# of what was left, so words from opposite ends of a sentence ended up side
# by side: "We're pretty good in what we do" became "pretty good what",
# "San Antonio, Austin" became "antonio austin", "Four Things Most Providers
# Cannot Say" became three chips. A business targets a NOUN PHRASE - "managed
# IT", "digital marketing agency", "breakfast tacos". So text is cut into
# chunks at punctuation, at every filler word, and at place names; each chunk
# that's left is a candidate as it stands. Place names are kept whole and
# reported separately - "san antonio" is who they're targeting, not what.

FILLER = set("""a an the and or of to for in on at by with from your you our we us is are be was were
this that it its as into near all one every any about than then there their they them these those
each other some such only own same so too not no yes do does did done doing make makes made making
help helps helping let lets know need needs want wants get gets got can cannot could will would should
may might must just more most less very really also here what who when where why how which whether
until till before after while because if but even ever never always again still yet up down out over
under off new now today call calls ask asks say says said go going see look find found use using
start starts started thing things way ways time times day days year years people person everyone
everything anything something nothing one two three four five six seven eight nine ten first second
reviewed review reviews here's what's it's isn't we're you're they're don't won't can't let's
q1 q2 q3 q4 i me my mine he she his her him vs versus via per tired""".split())

# Words that sell rather than name: fine inside a phrase, never the phrase.
PUFF = set("""best top leading trusted premier affordable reliable professional expert experts quality
award winning award-winning pretty good great amazing awesome excellent responsive complete full
local family owned operated proudly since welcome official home page contact located serving serve
serves proud dedicated passionate innovative intelligent smart simple complex easy fast quick
zero compromise fine growing grow grows results delivers deliver delivered really true real
future forward building build builds built shipped move moves why what featured need inspiration
studio stack ready slow average mid sized small large big""".split())

# Nouns too generic to stand alone as a keyword.
GENERIC = set("""services service solutions solution business businesses company companies llc inc co
agency firm team teams industries industry clients client customers customer work works working range
things organizations organization projects project systems system products product options site
website websites needs checklist questions answers support online offline""".split())

PLACES = ["san antonio", "new braunfels", "round rock", "san marcos", "fort worth", "el paso",
          "corpus christi", "central texas", "south texas", "hill country", "cedar park",
          "universal city", "live oak", "leon valley", "canyon lake", "garden ridge",
          "austin", "dallas", "houston", "texas", "tx", "boerne", "seguin", "schertz", "cibolo",
          "converse", "helotes", "bulverde", "kyle", "buda", "georgetown", "pflugerville",
          "floresville", "castroville", "selma", "kerrville", "fredericksburg", "nationwide"]
_PLACE_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in sorted(PLACES, key=len, reverse=True)) + r")\b", re.I)

MAX_CHUNK = 4


def _chunks(text: str, brand: list) -> tuple:
    """(phrases, places) found in one piece of text."""
    text = text or ""
    for b in brand:                        # the business's own name, as a phrase
        text = re.sub(r"\b%s\b" % re.escape(b), " | ", text, flags=re.I)
    places = [m.group(1).lower() for m in _PLACE_RE.finditer(text)]
    text = _PLACE_RE.sub(" | ", text)
    out = []
    for piece in re.split(r"[.|:;–—•·,/!?()\[\]&\"“”]+|\s-\s|\band\b|\s[+]\s", text):
        run = []
        for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", piece):
            w = raw.lower().replace("’", "'").strip("-'")
            if w.endswith("'s"):
                w = w[:-2]
            acronym = raw.isalpha() and raw.isupper() and 2 <= len(raw) <= 5
            keep = acronym or (w not in FILLER and w not in PUFF and len(w) > 1
                               and "-" not in w and not any(ch.isdigit() for ch in w))
            if keep:
                run.append(w)
            else:
                if run:
                    out.append(run)
                run = []
        if run:
            out.append(run)
    phrases = []
    for run in out:
        if run[-1] == "service":
            run[-1] = "services"            # "web design service" and "...services" are one phrase
        if len(run) > MAX_CHUNK:
            run = run[-MAX_CHUNK:]          # the head noun sits at the end in English
        if all(w in GENERIC for w in run):
            continue
        phrases.append(" ".join(run))
    return phrases, places


def keywords_from(seo: dict, name: str = "", domain: str = "", top: int = 10) -> list:
    """The phrases a business chose for its title, description and
    headings, weighted by where they sit (title counts most), with the
    brand's own name removed. Returns [{"phrase", "weight", "where"}];
    place names they target come last, marked where=["place"]."""
    if not seo or not seo.get("ok"):
        return []
    brand = brand_phrases(name, domain)
    fields = [("title", [seo.get("title") or ""], 3.0),
              ("h1", seo.get("h1s") or [], 2.5),
              ("meta", [seo.get("meta_desc") or ""], 1.5),
              ("h2", seo.get("h2s") or [], 1.0)]
    score, where, places = Counter(), {}, Counter()
    core_words = set()                     # words chosen for the title, h1 or description
    found = {}
    for label, texts, w in fields:
        for t in texts:
            ph, pl = _chunks(t, brand)
            found.setdefault(label, []).extend(ph)
            if label in ("title", "h1"):
                for p in pl:
                    places[p] += w
            if label != "h2":
                for p in ph:
                    core_words |= set(p.split())
    h2_seen = Counter(found.get("h2", []))
    for label, _, w in fields:
        for ph in found.get(label, []):
            single = " " not in ph
            if single and label not in ("title", "h1"):
                continue                   # a lone word in a sentence is not a keyword
            if label == "h2" and h2_seen[ph] < 2 and not (set(ph.split()) & core_words):
                continue                   # a section heading that's off their main theme
            score[ph] += w * (1.0 if single else 1.2)
            where.setdefault(ph, set()).add(label)
    # "app" on its own and "app development" are the same keyword: the
    # longer phrase takes the shorter one's weight and the shorter one goes.
    def inside(short, long_):
        a, b = short.split(), long_.split()
        return len(a) < len(b) and any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))
    for ph in sorted(score, key=lambda x: len(x.split())):
        longer = [o for o in score if inside(ph, o)]
        if longer:
            best = max(longer, key=lambda o: score[o])
            score[best] += score[ph]
            where[best] |= where[ph]
            del score[ph]
    out = []
    for ph, sc in score.most_common(60):
        out.append({"phrase": ph, "weight": round(sc, 1), "where": sorted(where[ph])})
        if len(out) >= top:
            break
    for p, sc in places.most_common(2):
        if p not in ("tx",) and len(out) < top + 2:
            out.append({"phrase": p, "weight": round(sc, 1), "where": ["place"]})
    return out


def _grade_words(a: dict) -> dict:
    """Plain words for the analyzer's numbers."""
    if not a or not a.get("ok"):
        return {}
    m = a.get("metrics") or {}
    return {"score": a.get("overall", 0), "grade": a.get("grade", ""),
            "load_seconds": m.get("load_seconds"), "page_weight_kb": m.get("page_weight_kb"),
            "tel_links": m.get("tel_links", 0)}


def profile(place: dict, seo: dict, analysis: dict, name: str = "", domain: str = "") -> dict:
    """One competitor, read. Everything the card shows comes from here."""
    st = (analysis or {}).get("stack") or {}
    det = st.get("detected") or {}
    p = {
        "name": name or (place or {}).get("name", ""),
        "domain": domain,
        "rating": (place or {}).get("rating"),
        "reviews": (place or {}).get("reviews") or 0,
        "site_ok": bool((analysis or {}).get("ok")),
        "site_error": "" if (analysis or {}).get("ok") else ((analysis or {}).get("error") or ""),
        **_grade_words(analysis),
        "cms": st.get("cms") or "",
        "analytics": det.get("analytics") or [],
        "ads": det.get("ads") or [],
        "chat": det.get("chat") or [],
        "booking": det.get("booking") or [],
        "title": (seo or {}).get("title") or "",
        "meta_desc": (seo or {}).get("meta_desc") or "",
        "h1s": (seo or {}).get("h1s") or [],
        "sitemap_urls": (seo or {}).get("sitemap_urls") or 0,
        "noindex": bool((seo or {}).get("noindex")),
        "keywords": keywords_from(seo, name, domain),
        "top_issues": [f.get("title") for f in ((analysis or {}).get("priorities") or [])][:3],
    }
    p["working"] = working_for_them(p)
    p["weak"] = weak_spots(p)
    return p


def working_for_them(p: dict) -> list:
    """What this competitor is doing right - the things worth copying."""
    out = []
    if (p.get("reviews") or 0) >= 100 and (p.get("rating") or 0) >= 4.5:
        out.append("%d reviews at %.1f stars - they ask for reviews and it shows." % (p["reviews"], p["rating"]))
    elif (p.get("reviews") or 0) >= 100:
        out.append("%d Google reviews - a steady review engine." % p["reviews"])
    if p.get("ads"):
        out.append("Running ads (%s tag on the site) - they're paying to be found." % ", ".join(p["ads"][:2]))
    if p.get("analytics"):
        out.append("Measuring traffic (%s)." % ", ".join(p["analytics"][:2]))
    if p.get("booking"):
        out.append("Online booking (%s) - a visitor can commit without calling." % ", ".join(p["booking"][:2]))
    if p.get("chat"):
        out.append("Live chat on the site (%s)." % ", ".join(p["chat"][:2]))
    if (p.get("score") or 0) >= 80:
        slow = any(re.search(r"slow|ttfb|load", t or "", re.I) for t in (p.get("top_issues") or [])) \
            or (p.get("load_seconds") or 0) >= 3
        out.append("A %s site (%s, %s)." % ("well-built" if slow else "fast, clean", p.get("grade"), p.get("score")))
    if (p.get("sitemap_urls") or 0) >= 30:
        out.append("%d pages in their sitemap - they publish, which is how they rank for more searches." % p["sitemap_urls"])
    kws = [k["phrase"] for k in (p.get("keywords") or [])[:3]]
    if kws:
        out.append("Title and headings go after: %s." % ", ".join('"%s"' % k for k in kws))
    return out


def weak_spots(p: dict) -> list:
    """What to say when you're selling against them."""
    out = []
    if not p.get("site_ok"):
        out.append("Their site didn't load when we checked - anyone who clicks them from Google bounces to you.")
        return out
    if (p.get("load_seconds") or 0) >= 4:
        out.append("Slow site (%.1fs to load) - phones give up before the page shows." % p["load_seconds"])
    if not p.get("ads"):
        out.append("No retargeting pixel - a visitor who leaves their site is gone; yours can be brought back.")
    if not p.get("analytics"):
        out.append("No analytics - they can't tell what's working.")
    if (p.get("tel_links") or 0) == 0:
        out.append("No tap-to-call on the site.")
    if p.get("noindex"):
        out.append("Their home page tells Google not to index it.")
    if (p.get("rating") or 5) < 4.0 and (p.get("reviews") or 0) >= 10:
        out.append("%.1f stars on Google - reviews are their soft spot." % p["rating"])
    if (p.get("reviews") or 0) < 20:
        out.append("Only %d Google reviews - easy to out-review." % (p.get("reviews") or 0))
    # The analyzer's own top issues fill in what's left - unless one of the
    # lines above already said it ("No analytics - they can't tell what's
    # working" and "No analytics installed" on the same card).
    said = " ".join(out).lower()
    topics = {"analytic": "analytic", "pixel": "pixel", "retarget": "pixel", "tap-to-call": "tap-to-call",
              "phone": "tap-to-call", "review": "review", "noindex": "index", "index": "index",
              "to load": "slow", "slow": "slow", "ttfb": "slow"}     # not "load": "lazy-loaded" is its own issue
    covered = {v for k, v in topics.items() if k in said}
    for t in (p.get("top_issues") or [])[:3]:
        if not t:
            continue
        tl = t.lower()
        if any(k in tl and v in covered for k, v in topics.items()):
            continue
        out.append(t.rstrip(".") + ".")
        covered |= {v for k, v in topics.items() if k in tl}
    return out


def steal_list(own: dict, comp: dict) -> list:
    """What they do that you don't. Only things worth doing."""
    if not own or not comp.get("site_ok"):
        return []
    out = []
    if comp.get("ads") and not own.get("ads"):
        out.append("They run ads with a retargeting tag; you don't. Put the pixel in before the first campaign.")
    if comp.get("booking") and not own.get("booking"):
        out.append("They take bookings on the site; you don't. Add a book-now button.")
    if comp.get("chat") and not own.get("chat"):
        out.append("They answer questions in a chat widget; you don't.")
    if own.get("reviews") is None:
        # We couldn't find your own Google listing, so there is no "your 0" -
        # saying one would be making it up.
        if (comp.get("reviews") or 0) >= 30:
            out.append("They have %d Google reviews. Review requests are how a count like that gets built."
                       % comp["reviews"])
    elif (comp.get("reviews") or 0) >= 2 * max(1, own.get("reviews") or 0) and (comp.get("reviews") or 0) >= 30:
        out.append("They have %d Google reviews to your %d. Turn on review requests." % (comp["reviews"], own.get("reviews") or 0))
    if (comp.get("sitemap_urls") or 0) >= 3 * max(1, own.get("sitemap_urls") or 0) and (comp.get("sitemap_urls") or 0) >= 20:
        out.append("They have %d pages to your %d - one page per service is how they show up for more searches." % (comp["sitemap_urls"], own.get("sitemap_urls") or 0))
    if (comp.get("score") or 0) - (own.get("score") or 0) >= 15:
        out.append("Their site scores %s to your %s - speed and mobile are worth an afternoon." % (comp.get("score"), own.get("score")))
    theirs = {k["phrase"] for k in (comp.get("keywords") or [])[:6]}
    mine = {k["phrase"] for k in (own.get("keywords") or [])}
    gap = [k for k in theirs if k not in mine]
    if gap:
        out.append("Phrases they target that you don't: %s." % ", ".join('"%s"' % g for g in gap[:4]))
    return out


def changes(prev: dict, cur: dict) -> list:
    """What moved since the last look. Empty when nothing worth saying."""
    if not prev or not cur:
        return []
    out = []
    def num(k):
        return (prev.get(k) or 0), (cur.get(k) or 0)
    a, b = num("reviews")
    if b - a >= 5:
        out.append("+%d Google reviews" % (b - a))
    ra, rb = prev.get("rating"), cur.get("rating")
    if ra and rb and abs(rb - ra) >= 0.2:
        out.append("rating %s → %s" % (ra, rb))
    a, b = num("score")
    if abs(b - a) >= 8:
        out.append("site score %s → %s" % (a, b))
    if (prev.get("title") or "") != (cur.get("title") or "") and cur.get("title"):
        out.append("new title: \"%s\"" % cur["title"][:80])
    if (prev.get("h1s") or [None])[:1] != (cur.get("h1s") or [None])[:1] and cur.get("h1s"):
        out.append("new headline: \"%s\"" % cur["h1s"][0][:80])
    for k, label in (("ads", "ads tag"), ("booking", "online booking"), ("chat", "live chat")):
        if cur.get(k) and not prev.get(k):
            out.append("added %s" % label)
        if prev.get(k) and not cur.get(k):
            out.append("dropped %s" % label)
    a, b = num("sitemap_urls")
    if b - a >= 5:
        out.append("+%d pages on the site" % (b - a))
    if prev.get("site_ok") and not cur.get("site_ok"):
        out.append("site is DOWN")
    if not prev.get("site_ok") and cur.get("site_ok"):
        out.append("site is back up")
    return out
