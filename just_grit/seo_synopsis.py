"""
SEO synopsis — a plain-English read on how findable a site is.

The analyzer already SCORES search visibility. A score isn't something you can
say to a business owner. This reads the concrete facts off the page — the title
they actually wrote, whether Google is told where they operate, how much content
is on the page — and writes a few paragraphs a non-technical person can act on.

Hard rule, same as every other check: only claim what we measured. We cannot see
rankings, traffic, backlinks, or competitors from one page fetch, and the output
says so out loud rather than implying we can.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from grit_analyzer import USER_AGENT

TIMEOUT = 15

# Texas markets first — this is who Just Grit sells to. Falls back to a generic
# "City, ST" pattern so it still works anywhere.
KNOWN_CITIES = [
    "san antonio", "new braunfels", "austin", "seguin", "boerne", "schertz",
    "converse", "helotes", "canyon lake", "bulverde", "selma", "cibolo",
    "universal city", "live oak", "alamo heights", "stone oak", "spring branch",
    "houston", "dallas", "fort worth", "el paso", "corpus christi", "laredo",
]

STOP = set("""a an and the or of for to in on at is are was be by with from your you
our we us this that it its as if then than so but not no yes all any can will""".split())


# ── bot walls ───────────────────────────────────────────────────────────────
# Cloudflare and friends serve an interstitial that has its own title, its own
# (tiny) word count, and — critically — its own `noindex` tag. Reading SEO off
# that page produces confident nonsense: it will tell you a healthy site is
# hidden from Google. That is the single worst thing this tool could say on a
# sales call, so a blocked fetch must abort, never degrade into a synopsis.
BOT_WALL_TITLES = re.compile(
    r"just a moment|checking your browser|attention required|access denied|"
    r"security check|ddos protection|please wait|verify you are human|"
    r"one more step|are you a robot|challenge", re.I)

BOT_WALL_BODY = re.compile(
    r"cf-browser-verification|cf_chl_|__cf_chl|/cdn-cgi/challenge|"
    r"ray id|enable javascript and cookies to continue|checking if the site connection", re.I)


def looks_blocked(html: str, title: str, status: int) -> bool:
    if status in (401, 403, 429, 503):
        return True
    if title and BOT_WALL_TITLES.search(title):
        return True
    if BOT_WALL_BODY.search(html[:20000] or ""):
        return True
    return False


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def read_seo(domain: str) -> dict:
    """One fetch, all the concrete SEO facts. Returns {} if unreachable."""
    url = domain if domain.startswith("http") else f"https://{domain}"
    out: dict = {"ok": False}
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                          headers={"User-Agent": USER_AGENT}) as cl:
            r = cl.get(url)
            html = r.text
            out["final_url"] = str(r.url)
            out["status"] = r.status_code
            base = f"{r.url.scheme}://{r.url.host}"
            # robots.txt + sitemap are separate, cheap, and tell us a lot
            try:
                rb = cl.get(base + "/robots.txt")
                out["robots_txt"] = rb.status_code == 200
                sm = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb.text) if rb.status_code == 200 else []
            except Exception:
                out["robots_txt"], sm = False, []
            sitemap_urls = 0
            for cand in (sm + [base + "/sitemap.xml", base + "/sitemap_index.xml"])[:3]:
                try:
                    s = cl.get(cand)
                    if s.status_code == 200 and ("<urlset" in s.text[:3000] or "<sitemapindex" in s.text[:3000]):
                        sitemap_urls = len(re.findall(r"<loc>", s.text))
                        break
                except Exception:
                    continue
            out["sitemap_urls"] = sitemap_urls
    except Exception as e:
        out["error"] = f"{type(e).__name__}"
        return out

    soup = BeautifulSoup(html, "html.parser")
    title = _txt(soup.title) if soup.title else ""

    if looks_blocked(html, title, out.get("status", 200)):
        out["ok"] = False
        out["blocked"] = True
        out["blocked_title"] = title
        return out

    out["ok"] = True
    out["title"] = title
    out["title_len"] = len(title)

    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = (md.get("content") or "").strip() if md else ""
    out["meta_desc"] = desc
    out["meta_desc_len"] = len(desc)

    h1s = [_txt(h) for h in soup.find_all("h1")]
    out["h1s"] = h1s
    out["h2s"] = [_txt(h) for h in soup.find_all("h2")][:12]

    rob = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots_meta = (rob.get("content") or "").lower() if rob else ""
    out["noindex"] = "noindex" in robots_meta

    can = soup.find("link", rel=lambda v: v and "canonical" in
                    [x.lower() for x in (v if isinstance(v, list) else [v])])
    out["canonical"] = (can.get("href") or "") if can else ""

    og = {m.get("property"): (m.get("content") or "")
          for m in soup.find_all("meta", property=re.compile("^og:", re.I))}
    out["og_title"] = bool(og.get("og:title"))
    out["og_image"] = bool(og.get("og:image"))

    # structured data
    types: set = set()
    def walk(n):
        if isinstance(n, dict):
            t = n.get("@type")
            for x in (t if isinstance(t, list) else [t]):
                if isinstance(x, str):
                    types.add(x)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    for sc in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            walk(json.loads(sc.string or sc.get_text() or ""))
        except Exception:
            continue
    out["schema_types"] = sorted(types)
    LOCAL = {"LocalBusiness", "HomeAndConstructionBusiness", "RoofingContractor",
             "Restaurant", "Plumber", "Electrician", "HVACBusiness", "Store",
             "ProfessionalService", "GeneralContractor", "Dentist", "HealthAndBeautyBusiness",
             "BarOrPub", "CafeOrCoffeeShop", "HairSalon", "BeautySalon", "Contractor"}
    out["has_local_schema"] = bool(types & LOCAL)

    imgs = soup.find_all("img")
    out["images"] = len(imgs)
    out["images_no_alt"] = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    host = re.sub(r"^www\.", "", (out.get("final_url") or "").split("/")[2] if "//" in out.get("final_url","") else "")
    internal = external = 0
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if h.startswith("/") or host in h:
            internal += 1
        else:
            external += 1
    out["links_internal"], out["links_external"] = internal, external

    for junk in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        junk.extract()
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    words = [w.lower().strip(".,!?;:()\"'") for w in body.split()]
    out["word_count"] = len(words)

    blob = (title + " " + desc + " " + " ".join(h1s) + " " + body).lower()
    found = [c for c in KNOWN_CITIES if c in blob]
    if not found:
        m = re.findall(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?),\s*(?:TX|Texas|[A-Z]{2})\b", body)
        found = [x.lower() for x in m[:3]]
    out["cities"] = sorted(set(found))[:6]
    out["city_in_title"] = any(c in title.lower() for c in out["cities"])

    meaningful = [w for w in words if w and w not in STOP and len(w) > 3 and w.isalpha()]
    freq: dict = {}
    for w in meaningful:
        freq[w] = freq.get(w, 0) + 1
    out["top_words"] = sorted(freq.items(), key=lambda kv: -kv[1])[:8]
    return out


def build_synopsis(f: dict, search_score: Optional[int] = None) -> dict:
    """Turn the facts into paragraphs plus a prioritized action list."""
    if not f.get("ok"):
        if f.get("blocked"):
            return {
                "ok": False, "blocked": True,
                "verdict": "We could not see this site — a security screen answered instead.",
                "paragraphs": [
                    "This site sits behind a bot-protection screen (Cloudflare or similar), so "
                    "our request got a security check page rather than the real homepage. "
                    "**We are not able to say anything about this site's SEO**, and anything we "
                    "did say would actually be describing the security screen — which carries "
                    "its own title, its own thin content, and its own instruction telling Google "
                    "not to index it.",
                    "This is not a problem with their website, and it is not a finding. Google's "
                    "crawler is normally allowed straight through these screens. To review this "
                    "one, open it in a browser and look manually.",
                ],
                "actions": [], "facts": {}, "unknowns": [
                    "Everything — the real page was never reached",
                ]}
        return {"ok": False,
                "verdict": "We couldn't load the site to read its SEO.",
                "paragraphs": ["The site did not respond, so there is nothing to report. "
                               "It may be down, or it may be refusing automated visitors."],
                "actions": [], "facts": {}, "unknowns": []}

    P, A = [], []

    # ── the headline: is it even indexable ──
    if f.get("noindex"):
        P.append("**This site is currently telling Google not to list it.** There's a "
                 "`noindex` instruction on the page, which means it will not appear in "
                 "search results at all, no matter what else is right. This is almost "
                 "always a staging setting that went live by accident.")
        A.append(("Critical", "Remove the noindex tag, then request re-indexing in Google Search Console."))

    # ── what Google shows in results ──
    t, tl = f.get("title") or "", f.get("title_len", 0)
    d, dl = f.get("meta_desc") or "", f.get("meta_desc_len", 0)
    cities = f.get("cities") or []

    if not t:
        P.append("**The page has no title.** The title is the blue clickable line in Google "
                 "results — the single most valuable piece of text the site owns. Without one, "
                 "Google invents something, usually badly.")
        A.append(("Critical", "Write a title under 60 characters: main service, then city, then business name."))
    else:
        bits = [f'The title reads "{t}" ({tl} characters']
        bits.append(" — it will be cut off in results" if tl > 65
                    else " — shorter than it needs to be" if tl < 25 else " — a good length")
        line = "".join(bits) + ")."
        if cities and not f.get("city_in_title"):
            line += (f" It doesn't name a city, even though the page mentions "
                     f"{cities[0].title()} — local searches are where this business competes.")
        elif f.get("city_in_title"):
            line += " It names the market, which is the right instinct for local search."
        P.append(line)
        if tl > 65:
            A.append(("Fix", "Trim the title under 60 characters, service and city first."))
        if cities and not f.get("city_in_title"):
            A.append(("Fix", f"Put {cities[0].title()} in the title tag."))

    if not d:
        P.append("**There's no description.** That's the two-line pitch under the link in "
                 "Google. Left blank, Google scrapes whatever text it likes — often a "
                 "navigation menu.")
        A.append(("Fix", "Write a 140–155 character description naming the service, the city, and a reason to click."))
    elif dl > 165:
        P.append(f"The description runs {dl} characters, so the end gets cut off in results — "
                 "usually the part with the call to action.")
        A.append(("Fix", "Trim the description to 155 characters with the offer in the first half."))

    # ── does Google know what and where this business is ──
    if f.get("has_local_schema"):
        P.append("The site does include LocalBusiness structured data, which is what feeds "
                 "hours, address and phone into Google's map results and voice search. That's "
                 "a real advantage — most local sites don't have it.")
    else:
        types = f.get("schema_types") or []
        detail = (f" It has other structured data ({', '.join(types[:4])}), but nothing that "
                  f"identifies it as a local business.") if types else " There's no structured data at all."
        P.append("**Google is not being told this is a local business.**" + detail +
                 " This is the markup behind the map listing, the star ratings and the "
                 "hours that show directly in search results.")
        A.append(("High", "Add LocalBusiness structured data with name, address, phone, hours and service area."))

    # ── content depth ──
    wc = f.get("word_count", 0)
    if wc < 250:
        P.append(f"There are only about {wc} words on the homepage. Google has very little "
                 "to work with, and thin pages struggle to rank for anything beyond the "
                 "business name.")
        A.append(("High", "Get the homepage to 500+ words covering the services and the areas served."))
    elif wc < 500:
        P.append(f"The homepage has roughly {wc} words — workable, but light. Pages that rank "
                 "for competitive local terms usually carry more.")
        A.append(("Fix", "Expand the homepage toward 700 words, one section per service."))
    else:
        P.append(f"The homepage carries about {wc} words, which is enough substance for Google "
                 "to understand what the business does.")

    # ── the plumbing ──
    plumbing = []
    sm = f.get("sitemap_urls", 0)
    plumbing.append(f"a sitemap listing {sm} pages" if sm else "no XML sitemap")
    plumbing.append("a robots.txt" if f.get("robots_txt") else "no robots.txt")
    plumbing.append("a canonical tag" if f.get("canonical") else "no canonical tag")
    P.append("Technical plumbing: " + ", ".join(plumbing) + ". " +
             (f"{f.get('links_internal',0)} internal links connect it together."
              if f.get("links_internal") else
              "Almost no internal linking, which caps how well any single page can rank."))
    if not sm:
        A.append(("Fix", "Generate an XML sitemap and reference it in robots.txt."))
    if not f.get("canonical"):
        A.append(("Minor", "Add a self-referencing canonical tag to every page."))
    if f.get("links_internal", 0) < 5:
        A.append(("Fix", "Link from the homepage to every service page and city page."))

    # ── images and sharing ──
    ims, noalt = f.get("images", 0), f.get("images_no_alt", 0)
    if ims and noalt / max(ims, 1) > 0.4:
        P.append(f"{noalt} of {ims} images have no alt text. For a local business that's real "
                 "traffic left on the table — project photos are how image search finds you.")
        A.append(("Fix", "Describe every photo in alt text, naming the city where it's true."))
    if not (f.get("og_title") and f.get("og_image")):
        A.append(("Minor", "Add Open Graph title and image so shared links render with a picture."))

    # ── local coverage ──
    if cities:
        P.append("Service area named on the page: " + ", ".join(c.title() for c in cities[:4]) + ".")
    else:
        P.append("**The page never names a city or service area.** 'Roofing' competes "
                 "nationally; 'roofing in New Braunfels' competes with a handful of firms. "
                 "If the market isn't named, it can't be ranked for.")
        A.append(("High", "Name the city in the title, the H1, and a service-area section."))

    order = {"Critical": 0, "High": 1, "Fix": 2, "Minor": 3}
    A.sort(key=lambda x: order.get(x[0], 9))

    verdict = ("This site is invisible in search by its own instruction." if f.get("noindex")
               else "Search fundamentals are in good shape." if search_score is not None and search_score >= 90
               else "The basics are mostly there, with clear gaps to close."
               if search_score is not None and search_score >= 70
               else "There is real work to do before this site competes in search.")

    return {
        "ok": True,
        "verdict": verdict,
        "paragraphs": P,
        "actions": [{"priority": p, "text": t} for p, t in A],
        "facts": {
            "title": t, "title_len": tl, "meta_desc_len": dl,
            "h1": (f.get("h1s") or [""])[0], "h1_count": len(f.get("h1s") or []),
            "word_count": wc, "sitemap_urls": sm,
            "schema": ", ".join(f.get("schema_types") or []) or "none",
            "local_schema": f.get("has_local_schema"),
            "images_no_alt": f"{noalt} of {ims}",
            "internal_links": f.get("links_internal", 0),
            "cities": ", ".join(c.title() for c in cities) or "none named",
            "top_words": ", ".join(w for w, _ in (f.get("top_words") or [])[:6]),
        },
        # Being explicit about the limits is what keeps this honest on a sales call.
        "unknowns": [
            "Where the site actually ranks for any search term",
            "How much traffic it gets, and from which keywords",
            "Who links to it (backlinks) and how strong those links are",
            "How it compares to specific competitors in the results page",
            "Google Business Profile completeness, reviews and photo activity",
        ],
    }


def synopsis_for(domain: str, search_score: Optional[int] = None) -> dict:
    return build_synopsis(read_seo(domain), search_score)
