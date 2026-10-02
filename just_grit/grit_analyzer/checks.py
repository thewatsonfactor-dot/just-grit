"""The checks — one function per category, plus marketing-stack detection.

Every finding must answer three questions: what we found (a fact with a
number), why it matters (in customers or dollars, never jargon), and the fix
(an instruction you can hand a developer). A check that can't answer all
three doesn't ship.

Structured data beats regex: a streetAddress in JSON-LD is authoritative over
text parsing. Mixed content counts only real subresource loads. When in
doubt, the check stays quiet — a wrong finding in a cold email costs
credibility.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .fetch import FetchResult


def finding(key, title, evidence, impact, fix, severity="warning", points=8):
    return {
        "key": key, "title": title, "evidence": evidence,
        "impact": impact, "fix": fix, "severity": severity, "points": points,
    }


# ---------------------------------------------------------------- parsing --

def parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def json_ld_blocks(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                out.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return out


def declared_subresources(soup: BeautifulSoup, base: str) -> list[tuple[str, str]]:
    """All subresources the page actually loads: scripts, stylesheets, images."""
    subs = []
    for s in soup.find_all("script", src=True):
        subs.append((urljoin(base, s["src"]), "script"))
    for l in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        if l.get("href"):
            subs.append((urljoin(base, l["href"]), "css"))
    for i in soup.find_all("img", src=True):
        src = i["src"]
        if not src.startswith("data:"):
            subs.append((urljoin(base, src), "img"))
    return subs


def render_blocking_count(soup: BeautifulSoup) -> int:
    head = soup.find("head")
    if not head:
        return 0
    n = 0
    for s in head.find_all("script", src=True):
        if not (s.has_attr("async") or s.has_attr("defer") or
                s.get("type") == "module"):
            n += 1
    for l in head.find_all("link", rel=lambda v: v and "stylesheet" in v):
        if not l.get("media") or l.get("media") in ("all", "screen"):
            n += 1
    return n


# ------------------------------------------------------------------ speed --

def check_speed(res: FetchResult, soup: BeautifulSoup) -> tuple[int, list]:
    f = []
    score = 100

    if res.load_seconds > 3:
        sev = "critical" if res.load_seconds > 6 else "serious"
        pts = 30 if res.load_seconds > 6 else 18
        f.append(finding(
            "slow_load", "The page is slow to load",
            f"The homepage took {res.load_seconds:.1f} seconds to deliver — and that's "
            "measured from a fast connection; real phones on cell service are slower.",
            "Half of visitors give up on a page that takes over 3 seconds. Every "
            "abandoned visit is a customer who calls the next company on Google.",
            "Compress images, remove unused scripts, and turn on caching at the host "
            "or put the site behind a CDN like Cloudflare (free tier is fine).",
            sev, pts))
        score -= pts

    if res.ttfb_ms > 800:
        sev = "serious" if res.ttfb_ms > 1500 else "warning"
        pts = 15 if res.ttfb_ms > 1500 else 8
        f.append(finding(
            "slow_ttfb", f"Slow server response — {res.ttfb_ms} ms TTFB",
            f"The server took {res.ttfb_ms} ms to send the first byte "
            "(under 500 ms is healthy).",
            "Slow first response drags every page on the site and hurts Google "
            "ranking, so fewer people find the business at all.",
            "Ask the host about caching, or move to a faster plan/host. On "
            "WordPress, a page-cache plugin usually fixes this in an afternoon.",
            sev, pts))
        score -= pts

    # page weight — HTML + probed sample, labeled an estimate
    declared = declared_subresources(soup, res.final_url)
    total_declared = len(declared)
    est_weight_kb = round((res.html_bytes + res.probed_bytes) / 1024)
    if res.probed_count and total_declared > res.probed_count:
        est_weight_kb = round(
            (res.html_bytes + res.probed_bytes * (total_declared / res.probed_count))
            / 1024)
    if est_weight_kb > 3500:
        f.append(finding(
            "heavy_page", "The page is very heavy",
            f"Estimated page weight is about {est_weight_kb:,} KB across "
            f"{total_declared} files — most contractor homepages should be under 2,000 KB.",
            "Heavy pages are slow on phones, and most local customers are on phones.",
            "Export photos at the size they display (a 400px-wide photo doesn't need "
            "a 4000px file) and serve them as WebP.",
            "serious", 12))
        score -= 12

    rb = render_blocking_count(soup)
    if rb > 12:
        pts = 12 if rb > 25 else 8
        f.append(finding(
            "render_blocking", f"{rb} files block the page from showing",
            f"{rb} scripts and stylesheets must fully download before anything "
            "appears on screen.",
            "Visitors stare at a blank screen — on a phone that reads as 'broken' "
            "and they hit back.",
            "Add `defer` to non-essential scripts and inline the small amount of "
            "CSS needed for the top of the page.",
            "serious" if rb > 25 else "warning", pts))
        score -= pts

    imgs = soup.find_all("img")
    if len(imgs) >= 8:
        not_lazy = [i for i in imgs[3:] if i.get("loading") != "lazy"]
        if len(not_lazy) > max(4, len(imgs) * 0.4):
            f.append(finding(
                "no_lazy", f"{len(not_lazy)} of {len(imgs)} images not lazy-loaded",
                f"{len(not_lazy)} images below the fold download immediately instead "
                "of when the visitor scrolls to them.",
                "The visible part of the page waits behind photos nobody has "
                "scrolled to yet.",
                'Add `loading="lazy"` to every image below the first screen. '
                "One attribute, no redesign.",
                "warning", 6))
            score -= 6
        no_dims = [i for i in imgs if not (i.get("width") and i.get("height"))]
        if len(no_dims) > len(imgs) * 0.5:
            f.append(finding(
                "no_img_dims", "Images load without reserved space",
                f"{len(no_dims)} of {len(imgs)} images don't declare width/height, "
                "so the page jumps around as they arrive.",
                "Visitors tap the wrong thing when buttons shift mid-tap — an "
                "irritated visitor is a lost call.",
                "Add width and height attributes to <img> tags so the browser "
                "reserves the space.",
                "notice", 4))
            score -= 4

    if res.html_bytes > 60_000 and not res.content_encoding:
        f.append(finding(
            "no_compression", "Text isn't compressed in transit",
            f"The page HTML is {res.html_bytes // 1024} KB and the server sends it "
            "uncompressed (no gzip/brotli).",
            "The same page could arrive in a third of the time for free.",
            "Enable gzip or brotli compression at the web server or host control "
            "panel — it's a checkbox on most hosts.",
            "warning", 6))
        score -= 6

    return max(0, score), f


# ----------------------------------------------------------------- search --

def check_search(res: FetchResult, soup: BeautifulSoup,
                 sitemap_ok: bool | None) -> tuple[int, list]:
    f = []
    score = 100
    host = res.host

    title = soup.find("title")
    title_text = (title.get_text() if title else "").strip()
    if not title_text:
        f.append(finding(
            "no_title", "The page has no title",
            "The <title> tag is missing or empty.",
            "The title is the blue headline on Google. Without it, Google invents "
            "one — and it won't say what you want it to say.",
            f'Add a title like "<Trade> in <City> | <Company Name>" — e.g. '
            f'"Roofing in San Antonio | {host}".',
            "critical", 18))
        score -= 18
    elif len(title_text) < 12 or len(title_text) > 70:
        f.append(finding(
            "weak_title", "The page title is the wrong length",
            f'The title is "{title_text[:60]}" ({len(title_text)} characters); '
            "Google displays roughly 50–60.",
            "A truncated or thin title wins fewer clicks from the people already "
            "searching for this service.",
            "Rewrite to ~50–60 characters: service + city + company name.",
            "warning", 6))
        score -= 6

    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if not meta_desc or not (meta_desc.get("content") or "").strip():
        f.append(finding(
            "no_meta_desc", "No search-result description",
            "The meta description is missing, so Google picks random page text "
            "for the two lines under the headline.",
            "Those two lines are free ad copy on every search result. Random text "
            "converts worse than a written pitch.",
            "Add a 150-character meta description: what you do, where, and why "
            "you — end with a call to action.",
            "serious", 10))
        score -= 10

    h1s = soup.find_all("h1")
    if not h1s:
        f.append(finding(
            "no_h1", "No main headline (H1)",
            "The page has no H1 heading.",
            "Google uses the H1 to understand what the page is about; without it "
            "the page ranks for less.",
            "Make the main visible headline an <h1> that names the service and "
            "the city.",
            "warning", 8))
        score -= 8
    elif len(h1s) > 3:
        f.append(finding(
            "many_h1", f"{len(h1s)} competing main headlines",
            f"The page marks {len(h1s)} different headlines as the H1.",
            "Google can't tell which one is the point of the page.",
            "Keep one H1; demote the rest to H2/H3.",
            "notice", 3))
        score -= 3

    if soup.find("meta", attrs={"name": re.compile("robots", re.I),
                                "content": re.compile("noindex", re.I)}):
        f.append(finding(
            "noindex", "The page tells Google not to list it",
            'A "noindex" tag is on the homepage.',
            "The site is invisible in search on purpose — usually a leftover from "
            "the developer's staging setup. This is lost revenue every single day.",
            "Remove the noindex robots meta tag and request re-indexing in Google "
            "Search Console.",
            "critical", 25))
        score -= 25

    if not soup.find("link", rel=lambda v: v and "canonical" in v):
        f.append(finding(
            "no_canonical", "No canonical address set",
            "The page doesn't declare its canonical URL.",
            "www/non-www and http/https copies can split ranking credit between "
            "duplicate versions of the same page.",
            'Add <link rel="canonical" href="https://…/"> to the page head.',
            "notice", 4))
        score -= 4

    if sitemap_ok is False:
        f.append(finding(
            "no_sitemap", "No sitemap for Google",
            "No sitemap.xml was found at the usual address or declared in robots.txt.",
            "A sitemap is how Google discovers service pages; without one, deeper "
            "pages get found late or never.",
            "Generate a sitemap (every CMS has a plugin or setting) and submit it "
            "in Google Search Console.",
            "warning", 6))
        score -= 6

    imgs = soup.find_all("img")
    if imgs:
        no_alt = [i for i in imgs if not (i.get("alt") or "").strip()]
        if len(no_alt) > len(imgs) * 0.5:
            f.append(finding(
                "no_alt", f"{len(no_alt)} of {len(imgs)} images unlabeled",
                f"{len(no_alt)} images have no alt text.",
                "Google Images sends real local traffic ('roof repair san antonio' "
                "image searches), and unlabeled photos can't rank.",
                'Add short alt text naming what\'s shown: "metal roof replacement, '
                'New Braunfels".',
                "notice", 4))
            score -= 4

    if not json_ld_blocks(soup):
        f.append(finding(
            "no_schema", "No structured data at all",
            "The page carries no schema.org markup.",
            "Structured data is how you get review stars and business info "
            "directly on the search result — competitors with it look bigger "
            "on the same page.",
            "Add JSON-LD structured data — at minimum LocalBusiness (see the "
            "local-presence section).",
            "warning", 6))
        score -= 6

    if not soup.find("meta", property=re.compile(r"^og:", re.I)):
        f.append(finding(
            "no_og", "Links share badly on social",
            "No Open Graph tags — a shared link shows no chosen image or headline.",
            "Every referral in a neighborhood Facebook group renders as a bare "
            "gray link instead of a photo card people click.",
            "Add og:title, og:description and og:image tags to the page head.",
            "notice", 4))
        score -= 4

    return max(0, score), f


# ------------------------------------------------------------- conversion --

CTA_WORDS = re.compile(
    r"free (quote|estimate|inspection|consult)|get (a )?(quote|estimate)|"
    r"request (a )?(quote|estimate|service)|schedule|book (now|online|an?)|"
    r"call (us|now|today)|contact us", re.I)

TRUST_WORDS = re.compile(
    r"licensed|insured|bonded|bbb|accredited|warranty|guarantee[d]?|"
    r"years? (of )?experience|since (19|20)\d\d|family[- ]owned|veteran[- ]owned",
    re.I)

REVIEW_WORDS = re.compile(
    r"reviews?|testimonials?|google rating|[45](\.\d)?\s*(stars?|★)|what our "
    r"customers", re.I)


def check_conversion(res: FetchResult, soup: BeautifulSoup,
                     contact_page: FetchResult | None = None) -> tuple[int, list]:
    f = []
    score = 100
    text = soup.get_text(" ", strip=True)
    html = res.html

    tel_links = soup.select('a[href^="tel:"]')
    phone_in_text = re.search(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", text)
    if not tel_links:
        if phone_in_text:
            f.append(finding(
                "no_tap_to_call", "The phone number isn't tap-to-call",
                f"The number {phone_in_text.group(0)} appears on the page as plain "
                "text — tapping it on a phone does nothing.",
                "Most local customers find you on their phone. If tapping the "
                "number doesn't start a call, a real share of them won't dial it "
                "by hand — they tap the next roofer instead.",
                'Wrap the number in a link: <a href="tel:+12105551234">'
                "(210) 555-1234</a>. Five-minute fix.",
                "serious", 15))
            score -= 15
        else:
            f.append(finding(
                "no_phone", "No phone number on the homepage",
                "No phone number was found anywhere on the homepage.",
                "For a local service business the call IS the conversion. A "
                "homepage without a number sends buyers to a competitor.",
                "Put a tap-to-call phone number in the header, visible on every "
                "screen size.",
                "critical", 22))
            score -= 22

    forms = soup.find_all("form")
    contact_has_form = bool(contact_page and contact_page.ok and
                            parse(contact_page.html).find("form"))
    if not forms:
        if contact_has_form:
            f.append(finding(
                "form_only_contact", "No contact form on the homepage",
                "There's a form on the contact page, but the homepage itself has "
                "no way to request a quote.",
                "Every extra click before the ask loses a slice of visitors — "
                "the homepage should be able to close on its own.",
                "Add a short 3-field form (name, phone, what do you need) to the "
                "homepage, above the footer.",
                "warning", 8))
            score -= 8
        else:
            f.append(finding(
                "no_form", "No contact form found",
                "Neither the homepage nor the contact page has a lead form."
                if contact_page is not None else
                "The homepage has no contact form.",
                "After-hours visitors can't call — a form is how the 9 PM "
                "researcher becomes a morning appointment. No form, no lead.",
                "Add a 3-field quote form (name, phone, need). Wire it to email "
                "and reply within the hour during business time.",
                "serious", 14))
            score -= 14

    # CTA above the fold — approximate: look in the first 40% of the body HTML
    body = soup.find("body")
    body_html = str(body)[: max(2000, int(len(str(body)) * 0.4))] if body else html[:4000]
    if not (CTA_WORDS.search(BeautifulSoup(body_html, "html.parser")
                             .get_text(" ", strip=True))
            or 'href="tel:' in body_html):
        f.append(finding(
            "cta_buried", "No call-to-action near the top",
            "In the first screen of the page there's no 'call', 'free estimate' "
            "or booking prompt.",
            "Visitors decide in seconds. If the first screen doesn't tell them "
            "what to do, most never scroll to find out.",
            'Put one clear button in the top section: "Call now" or '
            '"Free estimate", tap-to-call on mobile.',
            "warning", 8))
        score -= 8

    if not soup.find("meta", attrs={"name": "viewport"}):
        f.append(finding(
            "no_viewport", "Not built for phones",
            "The page has no mobile viewport tag, so phones show a shrunken "
            "desktop layout.",
            "Google ranks mobile-hostile sites lower, and pinch-zooming "
            "customers give up fast.",
            'Add <meta name="viewport" content="width=device-width, '
            'initial-scale=1"> and check the layout on a phone.',
            "serious", 12))
        score -= 12

    if not TRUST_WORDS.search(text):
        f.append(finding(
            "no_trust", "No trust signals on the page",
            "Nothing on the homepage says licensed, insured, guaranteed, or "
            "years in business.",
            "Homeowners are letting strangers onto their roof — proof of "
            "legitimacy is often the tiebreaker between two quotes.",
            "Add a short trust strip: license #, insured, years in business, "
            "any association badges.",
            "warning", 8))
        score -= 8

    if not REVIEW_WORDS.search(text):
        f.append(finding(
            "no_reviews_shown", "Customer reviews aren't shown",
            "The homepage doesn't display ratings, review counts, or testimonials.",
            "You've earned the stars on Google — a homepage that doesn't show "
            "them makes a 4.8★ company look like an unknown.",
            "Embed the Google rating and 2–3 short named testimonials near the "
            "top of the page.",
            "warning", 8))
        score -= 8

    return max(0, score), f


# ------------------------------------------------------------------ local --

# Counting locations, so a report never tells a ten-clinic group to publish one
# address. The first version let the middle of a match run past a ZIP into the
# next address - it produced "78217 2430 E. Southcross Blvd" and miscounted.
# A ZIP ends an address, so we mark that boundary and forbid crossing it.
_SUFFIX = (r"St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Hwy|"
           r"Highway|Loop|Pkwy|Parkway|Ct|Court|Way|Cir|Circle|Trail|Trl")
_ZIP_END = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")
_ADDR_RX = re.compile(
    r"\b\d{1,6}\s+(?:(?!\d{5}\b)[A-Za-z0-9.\'#-]+\s+){0,6}?(?:" + _SUFFIX + r")\b",
    re.I)


def street_addresses(text):
    """Distinct street addresses on the page, as matched."""
    marked = _ZIP_END.sub(r"\1 \2 ~ ", text or "")
    hits, seen, out = [m.group(0).strip() for m in _ADDR_RX.finditer(marked)], set(), []
    for h in hits:
        k = re.sub(r"[^a-z0-9]", "", h.lower())[:28]
        if k and k not in seen:
            seen.add(k)
            out.append(h)
    return out


def count_locations(text, soup):
    """How many distinct physical locations this page appears to describe.

    Two independent signals - addresses in the text and Google Maps embeds -
    and we take the larger, because a page usually lists more addresses than
    it embeds maps for."""
    maps = 0
    try:
        maps = len([i for i in soup.find_all("iframe")
                    if "google.com/maps" in (i.get("src") or "")])
    except Exception:
        pass
    return max(len(street_addresses(text)), maps, stated_location_count(text))


_COUNT_RX = re.compile(
    r"\b(\d{1,3})\s+(?:convenient\s+|great\s+|neighborhood\s+|area\s+)?locations\b", re.I)


def stated_location_count(text):
    """A chain that says "12 locations" has told us the number outright.

    Cheaper and far more reliable than counting addresses, and it works on the
    common case where the homepage carries no address at all.
    """
    best = 0
    for m in _COUNT_RX.finditer(text or ""):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 2 <= n <= 500:            # "1 locations" is a typo; 900 is marketing
            best = max(best, n)
    return best


def locations_index(soup):
    """Does this page point at a locations directory?

    Counting addresses on the homepage misses the most common shape of all:
    a chain whose homepage carries no address at all because every address
    lives behind a "Locations" link. The Wash Tub reads as a one-location
    business by address count and gets told to put its address in the footer -
    advice that is wrong, and wrong in a way a prospect notices immediately.
    """
    try:
        links = soup.find_all("a")
    except Exception:
        return False
    for a in links:
        href = (a.get("href") or "").lower()
        label = " ".join((a.get_text(" ", strip=True) or "").lower().split())
        if re.search(r"locations|store-?locator|find-a-(location|store|shop|wash)|"
                     r"where-we-are|near-?(you|me)", href):
            return True
        # Plural is the tell. A single-location business links "Location";
        # a chain links "Locations". Singular alone is not enough to suppress
        # the address check, so it is deliberately not matched here.
        if re.search(r"\blocations\b|\bstore locator\b|\bfind a (location|store|wash)\b|"
                     r"\bnear (you|me)\b", label):
            return True
    return False


def looks_like_chain(text, soup):
    """Multi-location, by any of three independent signals.

    Any one is enough. The cost of a false negative (telling a 12-store chain
    to add one address) is a prospect who stops reading; the cost of a false
    positive (skipping the address check on a single-location business) is one
    missing minor finding. Those are not symmetric, so this leans toward
    calling it a chain.
    """
    return (count_locations(text, soup) >= 2
            or multi_location_pages(soup)
            or locations_index(soup))


def multi_location_pages(soup):
    """Does the site look like it has a page per location, rather than anchors?"""
    try:
        hrefs = [a.get("href") or "" for a in soup.find_all("a")]
    except Exception:
        return False
    real = [h for h in hrefs
            if re.search(r"/(locations?|offices?|clinics?)/[a-z0-9\-]{2,}", h, re.I)
            and not h.lstrip().startswith("#")]
    return len(set(real)) >= 2

def check_local(res: FetchResult, soup: BeautifulSoup) -> tuple[int, list]:
    f = []
    score = 100
    blocks = json_ld_blocks(soup)
    text = soup.get_text(" ", strip=True)

    types = {str(b.get("@type", "")).lower() for b in blocks}
    local_types = {"localbusiness", "roofingcontractor", "homeandconstructionbusiness",
                   "generalcontractor", "plumber", "electrician", "hvacbusiness",
                   "professionalservice", "restaurant", "store", "dentist",
                   "medicalbusiness", "autorepair", "legalservice"}
    has_local_schema = bool(types & local_types) or any(
        "localbusiness" in t for t in types)

    # How many distinct locations does this page actually describe? Telling a
    # ten-clinic group to "add LocalBusiness JSON-LD with name, phone, address"
    # produces schema claiming the whole company sits at one address - worse
    # than none. Count first, then prescribe.
    n_locs = count_locations(text, soup)
    chain  = looks_like_chain(text, soup)

    if not has_local_schema:
        if n_locs >= 2 or chain:
            f.append(finding(
                "no_localbusiness",
                ("No structured data for %d locations" % n_locs) if n_locs >= 2
                else "No structured data for the locations",
                ("This page describes %d locations and carries no schema.org markup "
                 "for any of them." % n_locs) if n_locs >= 2 else
                "This looks like a multi-location business, and neither the homepage "
                "nor its markup identifies any of the locations to Google.",
                "Structured data is how a site tells Google that each of these is a "
                "separate business with its own address and phone. Without it Google "
                + (("is inferring %d locations from prose - and inference is how one "
                   "gets left out of the map pack.") % n_locs if n_locs >= 2 else
                   "is left to infer them, and inference is how a location gets left "
                   "out of the map pack."),
                "One LocalBusiness block PER LOCATION, each on its own page, tied "
                "together under a parent Organization. Not one block for the whole "
                + (("company - a single address claiming to represent %d locations is "
                   "worse than none.") % n_locs if n_locs >= 2 else
                   "company - a single address claiming to represent every location is "
                   "worse than none."),
                "serious", 25))
            score -= 25
        else:
            f.append(finding(
                "no_localbusiness", "No LocalBusiness structured data",
                "The page has no LocalBusiness schema markup identifying the company, "
                "phone, address, and service area to Google.",
                "This is how Google connects the website to the map listing. Without "
                "it you're weaker in the map pack \u2014 where most local jobs actually "
                "come from.",
                "Add LocalBusiness JSON-LD with name, phone, address, geo, hours, and "
                "areaServed. One script tag; a developer needs 30 minutes.",
                "serious", 20))
            score -= 20

    # Many locations sharing one URL is its own finding, and usually the bigger one.
    if n_locs >= 3 and not multi_location_pages(soup):
        f.append(finding(
            "locations_one_page", "%d locations share a single page" % n_locs,
            "All %d locations appear as sections of one page rather than each "
            "having its own." % n_locs,
            "Google ranks pages, not paragraphs. Somebody searching for your trade "
            "in one suburb is looking for a page about that suburb - so %d locations "
            "compete for the authority of a single URL instead of each earning their "
            "own." % n_locs,
            "A real page per location, each with that address, phone, hours, map and "
            "directions. Keep the current page as the hub that links to all of them.",
            "serious", 25))
        score -= 25

    # NAP — structured data is authoritative over text parsing (kills false positives)
    has_address = any(b.get("address") for b in blocks)
    if not has_address:
        addr_rx = re.search(
            r"\b\d{2,5}\s+[A-Z][A-Za-z0-9 .]{3,40}\b(St|Street|Ave|Avenue|Rd|Road|"
            r"Blvd|Dr|Drive|Ln|Lane|Hwy|Loop|Pkwy|Suite|Ste)\b", text)
        has_address = bool(addr_rx)
    # A chain's homepage is not supposed to carry one street address - the
    # addresses live on the location pages. Flagging it says "we only read one
    # page and didn't notice what kind of company you are."
    if not has_address and not chain:
        f.append(finding(
            "no_address", "No street address on the homepage",
            "No physical address appears on the homepage (in text or markup). "
            "Other pages were not read for this check.",
            "Customers and Google both use the address to decide you're a real "
            "local company and not a lead-gen front.",
            "Put the full address in the footer and in LocalBusiness markup.",
            "warning", 10))
        score -= 10

    if not soup.select('a[href*="google.com/maps"], a[href*="g.page"], '
                       'a[href*="maps.app.goo.gl"]'):
        if chain:
            f.append(finding(
                "no_gbp_link", "Location pages don't link their Google listings",
                "No link to a Google Business Profile anywhere on this page.",
                "A chain has one Google profile per location, and each one should be "
                "linked from that location's own page - that is the connection Google "
                "uses to tie the store to the site.",
                "Link each location page to that location's Google Business Profile "
                "and embed its map. Not one profile on the homepage.",
                "notice", 4))
            score -= 4
        else:
            f.append(finding(
                "no_gbp_link", "No link to the Google Business Profile",
                "The site never links to its Google listing or map.",
                "The site and the Google profile should feed each other — reviews, "
                "directions, and the map pack all live there.",
                "Link the footer address to the Google Business Profile and embed "
                "the map on the contact page.",
                "notice", 6))
            score -= 6

    if not re.search(r"serv(e|ing|ice area)|areas? we (serve|cover)|proudly "
                     r"serving|(san antonio|new braunfels|austin|texas|tx)\b",
                     text, re.I):
        f.append(finding(
            "no_service_area", "No named service area",
            "The page never says what cities or areas the company serves.",
            "'Roofer' ranks nowhere; 'roofer in New Braunfels' ranks. Naming "
            "the area is how you show up for the town you actually work in.",
            "Add a 'Proudly serving San Antonio, New Braunfels, and the Hill "
            "Country' line and per-city service pages when ready.",
            "warning", 10))
        score -= 10

    return max(0, score), f


# ------------------------------------------------------------------ trust --

def check_trust(res: FetchResult, soup: BeautifulSoup) -> tuple[int, list]:
    f = []
    score = 100

    if not res.https:
        f.append(finding(
            "no_https", "The site isn't secure (no HTTPS)",
            "The site loads over plain HTTP.",
            'Browsers brand it "Not Secure" in the address bar — customers see '
            "that warning right as they decide whether to hand over a phone "
            "number. Google also ranks HTTP sites lower.",
            "Install a free TLS certificate (Let's Encrypt, or one click on most "
            "hosts) and redirect all traffic to https.",
            "critical", 30))
        score -= 30
    else:
        if res.http_redirects_to_https is False:
            f.append(finding(
                "no_https_redirect", "The insecure address doesn't redirect",
                "http:// serves the site without forwarding to https://.",
                "Old links and typed addresses land on the insecure copy — same "
                "'Not Secure' warning, plus Google sees two competing sites.",
                "Add a permanent 301 redirect from http to https at the server.",
                "serious", 12))
            score -= 12
        if res.mixed_content:
            f.append(finding(
                "mixed_content", f"{len(res.mixed_content)} insecure files on a "
                "secure page",
                f"{len(res.mixed_content)} scripts/styles/images load over plain "
                "http (e.g. {0}).".format(res.mixed_content[0][:60]),
                "Browsers block or flag these — broken images and padlock "
                "warnings on an otherwise secure site.",
                "Change those URLs to https:// (or protocol-relative) in the "
                "templates.",
                "warning", 8))
            score -= 8

    if len(res.redirect_chain) > 3:
        f.append(finding(
            "redirect_chain", f"{len(res.redirect_chain) - 1} redirects before "
            "the page loads",
            " → ".join(u[:40] for u in res.redirect_chain[:4]) + "…",
            "Each hop adds delay and leaks a little ranking credit.",
            "Point the domain straight at the final address with a single "
            "redirect.",
            "notice", 5))
        score -= 5

    for form in parse(res.html).find_all("form"):
        action = (form.get("action") or "").strip()
        if action.startswith("http://"):
            f.append(finding(
                "insecure_form", "A form submits over an insecure connection",
                f"A form posts to {action[:60]} over plain http.",
                "Whatever the customer types — name, phone, address — travels "
                "unencrypted.",
                "Change the form action to https.",
                "critical", 18))
            score -= 18
            break

    return max(0, score), f


# ------------------------------------------------------------------ stack --

STACK_SIGNATURES = {
    "analytics": [
        ("Google Analytics", r"googletagmanager\.com/gtag|google-analytics\.com|gtag\("),
        ("Google Tag Manager", r"googletagmanager\.com/gtm\.js"),
        ("Plausible", r"plausible\.io/js"),
        ("Fathom", r"cdn\.usefathom\.com"),
        ("Matomo", r"matomo\.js|_paq"),
        ("MS Clarity", r"clarity\.ms"),
        ("Hotjar", r"static\.hotjar\.com"),
    ],
    "ads": [
        ("Meta Pixel", r"connect\.facebook\.net/[^\"']*fbevents|fbq\("),
        ("Google Ads", r"googleads\.g\.doubleclick|gtag\(['\"]config['\"],\s*['\"]AW-"),
        ("TikTok Pixel", r"analytics\.tiktok\.com"),
        ("LinkedIn Insight", r"snap\.licdn\.com"),
        ("Pinterest Tag", r"s\.pinimg\.com/ct"),
        ("Reddit Pixel", r"redditstatic\.com/ads"),
        ("Snap Pixel", r"sc-static\.net/scevent"),
    ],
    "email": [
        ("Mailchimp", r"chimpstatic|list-manage\.com"),
        ("Klaviyo", r"static\.klaviyo\.com"),
        ("HubSpot", r"js\.hs-scripts\.com|hsforms"),
        ("ActiveCampaign", r"trackcmp\.net"),
        ("Beehiiv", r"embeds\.beehiiv\.com"),
    ],
    "chat": [
        ("Podium", r"podium\.com|podiumwebchat"),
        ("Intercom", r"widget\.intercom\.io"),
        ("Drift", r"js\.driftt\.com"),
        ("Tawk.to", r"embed\.tawk\.to"),
        ("LiveChat", r"cdn\.livechatinc\.com"),
        ("Tidio", r"code\.tidio\.co"),
    ],
    "booking": [
        ("Calendly", r"assets\.calendly\.com|calendly\.com/"),
        ("Housecall Pro", r"housecallpro"),
        ("ServiceTitan", r"servicetitan"),
        ("Jobber", r"getjobber"),
        ("Acuity", r"acuityscheduling"),
    ],
}

CMS_SIGNATURES = [
    ("WordPress", r"wp-content|wp-includes"),
    ("Wix", r"wixstatic\.com|wix\.com"),
    ("Squarespace", r"squarespace\.com|sqsp\.net"),
    ("Shopify", r"cdn\.shopify\.com"),
    ("GoDaddy Builder", r"godaddy\.com|wsimg\.com"),
    ("Duda", r"dudaone|duda\.co|dd-cdn"),
    ("Webflow", r"assets\.website-files\.com|webflow"),
    ("Weebly", r"weebly\.com|editmysite"),
    ("Joomla", r"/media/jui/|joomla"),
    ("Drupal", r"drupal-|sites/default/files"),
]


def detect_stack(html: str) -> dict:
    detected: dict[str, list[str]] = {}
    for category, sigs in STACK_SIGNATURES.items():
        hits = [name for name, rx in sigs if re.search(rx, html, re.I)]
        if hits:
            detected[category] = hits
    cms = next((name for name, rx in CMS_SIGNATURES if re.search(rx, html, re.I)),
               None)
    return {
        "cms": cms,
        "detected": detected,
        "has_analytics": "analytics" in detected,
        "has_retargeting": "ads" in detected,
    }
