"""Read a prospect's website for the things that help you sell them.

The background scan answers "is this website any good" — a question that
matters when you sell websites. It says nothing about who runs the company,
who to ask for, how big they are, or what they already use. For HomeRepair
selling maintenance to property managers, those ARE the qualification.

Everything here is conservative on purpose. A wrong name in "Hi Mike," is
worse than "Hi there," so a person is only returned when the page pairs a
plausible human name with a real job title, or when an email's local part is
unmistakably a person. Guessing is not extraction.
"""
from __future__ import annotations
import re, urllib.parse

Q = chr(34) + chr(39)
HREF_RE   = "href=[" + Q + "]([^" + Q + "#]+)[" + Q + "]"
MAILTO_RE = "href=[" + Q + "]mailto:([^" + Q + "?]+)"
EMAIL_RE  = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Mailboxes that are a department, not a person.
GENERIC = {"info", "contact", "hello", "office", "admin", "support", "sales",
           "leasing", "rentals", "rent", "maintenance", "service", "help",
           "team", "mail", "inquiries", "enquiries", "general", "accounting",
           "billing", "webmaster", "noreply", "no-reply", "privacy", "legal",
           "careers", "jobs", "marketing", "applications", "apply"}

# Titles worth having, best first. The order IS the ranking - whoever holds
# the highest title becomes the primary contact.
TITLES = [
    (r"\b(owner|founder|principal|president|CEO)\b",              "Owner",              10),
    (r"\bdesignated broker\b|\bbroker[/ -]?owner\b",              "Broker/Owner",        9),
    (r"\bbroker\b",                                               "Broker",              8),
    (r"\b(?:director|head) of (?:property )?management\b",        "Director of Mgmt",    7),
    (r"\bproperty manager\b|\bportfolio manager\b",               "Property Manager",    6),
    (r"\bmaintenance (?:manager|coordinator|director|supervisor)\b", "Maintenance Lead",  6),
    (r"\bleasing (?:manager|agent|consultant)\b",                 "Leasing",             3),
    (r"\b(?:realtor|agent|associate)\b",                          "Agent",               2),
]

NAME = r"[A-Z][a-z]{1,15}(?:\s+[A-Z]\.?)?\s+[A-Z][a-z'\-]{1,20}"

# Words that look like names to a regex and are not.
NOT_A_NAME = re.compile(
    r"\b(property|management|real|estate|home|house|rental|leasing|maintenance|"
    r"contact|about|our|team|service|company|group|inc|llc|san|antonio|new|"
    r"braunfels|texas|privacy|policy|terms|equal|housing|fair|read|more|learn|"
    r"view|all|rights|reserved)\b", re.I)

PORTALS = {
    "appfolio": "AppFolio", "buildium": "Buildium", "propertyware": "Propertyware",
    "rentmanager": "Rent Manager", "rentvine": "Rentvine", "doorloop": "DoorLoop",
    "tenantcloud": "TenantCloud", "yardi": "Yardi", "entrata": "Entrata",
    "rentcafe": "RentCafe", "managebuilding": "Buildium",
}

CONTACT_WORDS = re.compile(
    r"contact|about|team|staff|meet|leadership|our-?people|who-?we-?are|owner|agents?|"
    r"management|founder|principals?|board|directors", re.I)

UNITS_RE = re.compile(
    r"([0-9][0-9,]{1,6})\s*\+?\s*(units?|doors?|homes?|properties|rentals?)"
    r"(?:\s+(?:under management|managed|in our portfolio))?", re.I)

AFTER_HOURS = re.compile(r"after[- ]hours|24/7|emergency maintenance|24 hour", re.I)
REQUEST_FORM = re.compile(r"maintenance request|submit a request|work order|service request", re.I)


def strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def looks_like_person(local: str) -> bool:
    l = re.sub(r"[^a-z]", "", local.lower())
    return bool(l) and local.lower().split("+")[0] not in GENERIC and len(l) >= 3


def name_from_local(local: str) -> str:
    """mike.hernandez -> Mike Hernandez. Only when it's clearly two names."""
    parts = [p for p in re.split(r"[._\-]", local) if p.isalpha() and len(p) > 1]
    if len(parts) >= 2 and all(len(p) >= 3 for p in parts[:2]):
        return " ".join(p.capitalize() for p in parts[:2])
    return ""


def title_of(text: str):
    for pat, label, rank in TITLES:
        if re.search(pat, text, re.I):
            return label, rank
    return "", 0


# What the name regex catches that is not a person: "and co-founder",
# "Ramon Co-Owner", "Revenue Drew", "Corporate Operations", "Sited
# Communities" all reached the contacts table and would have gone out as
# "Hi Corporate," - so every name passes this gate on the way in.
NAME_STOP = re.compile(
    r"\b(and|the|our|both|with|from|for|owner|owners|co-?owner|founder|co-?founder|president|"
    r"vice|principal|partner|partners|broker|brokers|realtor|realtors|agent|agents|manager|"
    r"managers|director|directors|coordinator|supervisor|assistant|associate|associates|"
    r"revenue|corporate|operations|operation|sales|marketing|leasing|maintenance|property|"
    r"properties|communities|community|management|team|staff|office|group|company|services?|"
    r"solutions|realty|homes?|rentals?|apartments?|estate|holdings|investments?|ceo|cfo|coo|"
    r"llc|inc|corp|hoa|texas|antonio|braunfels|san|new|north|south|east|west|hill|country|"
    r"meet|about|contact|call|email|phone|welcome|read|more|learn|view|click|here|today|"
    r"licensed|certified|member|members|since|years?|experience|serving|trusted|local|front|desk|"
    r"reception|receptionist|customer|client|clients|support|info|billing|admin|built|flat|fee|"
    r"rent|lease|sold|buy|sell|list|listing|best|top|first|quality|premier|elite|pro)\b", re.I)


def clean_person_name(name: str) -> str:
    """A real-looking first-and-last name, or ''. Two to three words, each a
    capitalised word of letters (an initial is fine), none of them a role,
    a place or a marketing word."""
    n = re.sub(r"\s+", " ", (name or "")).strip(" ,.-")
    parts = n.split(" ")
    if len(parts) < 2 or len(parts) > 4:
        return ""
    for w in parts:
        if not re.fullmatch(r"[A-Z][a-z'\-]{1,20}|[A-Z]\.?|de|la|del|da|di|van|von|der|le", w):
            return ""
    if len(parts) == 4 and not any(w in ("de", "la", "del", "da", "di", "van", "von", "der", "le") for w in parts):
        return ""
    if NAME_STOP.search(n):
        return ""
    if len(parts[0]) < 2 or len(parts[-1]) < 2:
        return ""
    return n


CONSUMER_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com",
                 "me.com", "live.com", "msn.com", "att.net", "sbcglobal.net", "satx.rr.com",
                 "rr.com", "comcast.net", "protonmail.com", "proton.me"}


def someone_elses(email: str, root: str) -> bool:
    """An address at another organisation - a city office, a law firm, the
    management company's own domain on a board's site. A person's personal
    mailbox (gmail and friends) is fine; a board member often uses one."""
    dom = (email or "").split("@")[-1].lower().strip()
    if not dom or not root:
        return False
    if dom == root or dom.endswith("." + root) or root.endswith("." + dom):
        return False
    if dom in CONSUMER_MAIL or any(dom.endswith("." + m) for m in CONSUMER_MAIL):
        return False
    return True


def people_in(text: str) -> list:
    """Names paired with a real job title, in either order."""
    out = {}
    for pat, label, rank in TITLES:
        # "Jane Doe, Broker"  /  "Jane Doe - Owner"
        for m in re.finditer(NAME + r"\s*[,\|–—\-]\s*(?:" + pat + ")", text, re.I):
            nm = m.group(0).split(",")[0].split("|")[0].split(" - ")[0].strip(" -–—")
            if not NOT_A_NAME.search(nm):
                out.setdefault(nm, (label, rank))
        # "Owner: Jane Doe" - a separator is REQUIRED. Allowing a bare space
        # matched "Brad Larsen, Owner Christine Kelly - Property Manager" and
        # handed Christine somebody else's title. Two names either side of a
        # title is the common case on a team page, not the rare one.
        for m in re.finditer("(?:" + pat + r")\s*[:\-–—|]\s*(" + NAME + ")", text, re.I):
            nm = m.group(m.lastindex).strip()
            if not NOT_A_NAME.search(nm):
                out.setdefault(nm, (label, rank))
    return [{"name": clean_person_name(n), "role": r[0], "rank": r[1]}
            for n, r in out.items() if clean_person_name(n)]


# Template filler a site builder leaves in the footer. S&S Tire & Auto's
# site says "email@email.com", and the refill wrote them an email to it.
PLACEHOLDER_LOCAL = {"email", "youremail", "your.email", "your-email", "yourname", "your.name",
                     "name", "user", "username", "someone", "somebody", "johndoe", "john.doe",
                     "janedoe", "jane.doe", "test", "testing", "sample", "example", "placeholder",
                     "address", "mail", "me", "you", "abc", "xyz", "noreply", "no-reply", "filler"}
PLACEHOLDER_DOMAIN = {"email.com", "example.com", "example.org", "example.net", "domain.com",
                      "yourdomain.com", "yoursite.com", "yourwebsite.com", "website.com",
                      "company.com", "yourcompany.com", "mysite.com", "mydomain.com", "test.com",
                      "sample.com", "site.com", "business.com", "yourbusiness.com", "address.com"}


def is_placeholder_email(addr: str) -> bool:
    """A made-up address nobody reads - never worth writing to."""
    a = (addr or "").strip().lower()
    if "@" not in a:
        return True
    local, _, dom = a.rpartition("@")
    label = dom.split(".")[0]
    if dom in PLACEHOLDER_DOMAIN:
        return True
    if local == label and local in PLACEHOLDER_LOCAL | {"info", "contact", "hello"}:
        return True                      # email@email.com, info@info.com
    if local in {"youremail", "your.email", "yourname", "johndoe", "john.doe", "janedoe",
                 "jane.doe", "someone", "placeholder", "filler"}:
        return True
    return False


def emails_in(html: str, root: str) -> list:
    found = []
    for m in re.finditer(MAILTO_RE, html, re.I):
        found.append(urllib.parse.unquote(m.group(1)).strip().lower())
    found += [e.lower() for e in EMAIL_RE.findall(strip_tags(html))]
    clean, seen = [], set()
    for e in found:
        if "@" not in e or len(e) > 90 or e in seen:
            continue
        if re.search(r"[.](png|jpe?g|gif|svg|webp|css|js)$", e, re.I):
            continue
        if re.search(r"@(sentry|wix|squarespace|godaddy|example|domain)\b", e, re.I):
            continue
        if is_placeholder_email(e):
            continue
        seen.add(e)
        clean.append(e)
    # their own domain first - a template often leaves the site builder's address behind
    own = [e for e in clean if root and e.split("@")[-1].endswith(root)]
    return own + [e for e in clean if e not in own]


def contact_links(html: str, root: str) -> list:
    seen, urls = set(), []
    for m in re.finditer(HREF_RE, html, re.I):
        href = m.group(1).strip()
        if not CONTACT_WORDS.search(href):
            continue
        if href.startswith("/"):
            href = "https://" + root + href
        elif not href.startswith("http"):
            continue
        elif root and root not in href.lower():
            continue
        if href.lower() not in seen:
            seen.add(href.lower())
            urls.append(href)
    return urls


def signals_in(text: str, html: str) -> dict:
    sig = {}
    best = 0
    for m in UNITS_RE.finditer(text):
        try:
            n = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # 100000 let through "2026" (a year), "10,000" (square feet) and
        # "1616" (a street number). A San Antonio property manager with more
        # than a few thousand doors is a national - and a door count that is
        # also a plausible year is not evidence of anything.
        window = text[max(0, m.start()-40):m.end()+40].lower()
        if re.search(r"sq\.?\s?f|square f|\$|suite|ste\.|©|copyright", window) \
           or re.search(r"[-.)]\s?\d{3}[-.\s]?" + re.escape(m.group(1)), window):
            continue
        if 1900 <= n <= 2100:                  # that is a year, not a portfolio
            continue
        if 5 <= n <= 5000 and n > best:
            best, sig["units"] = n, n
            sig["units_phrase"] = m.group(0).strip()
    low = html.lower()
    for needle, label in PORTALS.items():
        if needle in low:
            sig["portal"] = label
            break
    sig["after_hours"] = bool(AFTER_HOURS.search(text))
    sig["request_form"] = bool(REQUEST_FORM.search(text))
    return sig


def research(domain: str, fetch, max_pages: int = 3) -> dict:
    """fetch(url) -> object with .html .status .robots_allowed .error"""
    root = (domain or "").lower().replace("www.", "")
    out = {"emails": [], "people": [], "signals": {}, "pages_read": 0, "why": ""}
    try:
        res = fetch(domain)
    except Exception as e:
        out["why"] = "Could not reach their site (%s)." % type(e).__name__
        return out
    if not getattr(res, "robots_allowed", True):
        out["why"] = "Their robots.txt asks us not to read the page, so we didn't."
        return out
    html = getattr(res, "html", "") or ""
    if not html or (getattr(res, "status", 0) or 0) >= 400:
        out["why"] = getattr(res, "error", "") or ("Site answered HTTP %s." % getattr(res, "status", "?"))
        return out

    pages = [html]
    out["pages_read"] = 1
    for href in contact_links(html, root)[: max_pages - 1]:
        try:
            sub = fetch(href)
        except Exception:
            continue
        if getattr(sub, "robots_allowed", True) and getattr(sub, "html", "") \
           and (getattr(sub, "status", 0) or 0) < 400:
            pages.append(sub.html)
            out["pages_read"] += 1

    all_html = " ".join(pages)
    text = strip_tags(all_html)

    out["emails"] = emails_in(all_html, root)
    out["signals"] = signals_in(text, all_html)

    people = people_in(text)
    # attach an email to a person when the local part matches their name
    for p in people:
        first, last = (p["name"].split()[0].lower(), p["name"].split()[-1].lower())
        for e in out["emails"]:
            loc = e.split("@")[0].lower()
            if loc in (first, last, first + last, first + "." + last,
                       first[0] + last, first + last[0]):
                p["email"] = e
                break
    # a personal mailbox with nobody claiming it is still a person
    named = {p.get("email") for p in people}
    for e in out["emails"]:
        if e in named:
            continue
        loc = e.split("@")[0]
        if looks_like_person(loc):
            nm = clean_person_name(name_from_local(loc))
            if nm and not any(p["name"].lower() == nm.lower() for p in people):
                people.append({"name": nm, "role": "", "rank": 1, "email": e})

    people.sort(key=lambda p: -p.get("rank", 0))
    out["people"] = people
    if not out["emails"] and not people:
        out["why"] = out["why"] or "Read %d page(s); no email or named contact listed." % out["pages_read"]
    return out


def say_line(sig: dict, people: list) -> tuple:
    """A gap and an opening sentence built only from what was actually read."""
    units = sig.get("units")
    if units and not sig.get("after_hours"):
        return ("%d units, no after-hours path" % units,
                "you're managing around %d units and I couldn't find an after-hours "
                "number anywhere on your site - so where does a 9pm water heater call "
                "actually go?" % units)
    if units:
        return ("%d units under management" % units,
                "you're managing around %d units, which is a lot of water heaters and "
                "filters to keep track of" % units)
    if sig.get("portal") and not sig.get("after_hours"):
        return ("%s, no after-hours path" % sig["portal"],
                "you're on %s for requests, but I couldn't find an after-hours number - "
                "so where does a 9pm call go?" % sig["portal"])
    if not sig.get("request_form") and not sig.get("after_hours"):
        return ("No online request path",
                "there's no way on your site for a tenant to report a problem, which "
                "means every one of them becomes a phone call to somebody")
    return ("", "")

# ── fit signals ──────────────────────────────────────────────────────
# For HomeRepair the website's QUALITY is irrelevant - a property manager's
# page speed says nothing about whether they need preventive maintenance.
# What their site does say, if you ask the right questions, is whether they
# are a buyer at all. These two decide targeting before a word is written.

# The disqualifier. A company with its own maintenance crew is not buying
# maintenance; at best it is a displacement conversation, and emailing them
# the "our own crews" line is how you look like you never checked.
# First version matched "Our Team" - a nav link on every property management
# site on earth - and reported 9 of 14 prospects as running their own crews.
# "our|in-house" + a generic noun like team/service is not evidence of anything.
# The noun has to be maintenance itself.
IN_HOUSE = re.compile(
    r"\b(our own|in[- ]?house|on[- ]?staff|full[- ]?time|dedicated)\s+"
    r"(maintenance|repair|handyman)\b"
    r"|\b(our own|in[- ]?house|on[- ]?staff|full[- ]?time)\s+(technicians?|maintenance (team|crew|staff))\b"
    r"|\bwe (employ|staff) (our own )?(maintenance|technicians?|handym[ae]n)\b",
    re.I)
USES_VENDORS = re.compile(
    r"\b(vetted|preferred|trusted|licensed|third[- ]party|approved|independent)\s+"
    r"(vendors?|contractors?)\b|\bvendor network\b|\bnetwork of (vendors?|contractors?)\b",
    re.I)

# A nav menu lists every service a company offers, so a site matching three
# property types tells you nothing about which one it actually is. Report all
# of them and let a human read it - a made-up "primary" is worse than a list.
TYPE_RX = [
    ("short_term",  r"\b(vacation rental|short[- ]term rental|airbnb|vrbo|nightly rate|"
                    r"guest check[- ]?in|turnover clean)\b"),
    ("hoa",         r"\b(homeowners?|property owners?|community) association\b|"
                    r"\bHOA management\b|\bboard of directors\b"),
    ("multifamily", r"\b(apartment communit|multi[- ]?family|leasing office)\w*"),
    ("single_family", r"\b(single[- ]family|rental homes?|houses? for rent)\b"),
]


def fit_signals(text: str, html: str) -> dict:
    """What the site says about whether they can buy this, not how good it is.

    Every value here is weak evidence and is labelled as such. "We have an
    in-house maintenance team" on a marketing page is a claim, not a fact -
    it belongs in a call script as a question to ask, never as a filter that
    silently drops a prospect.
    """
    inh, ven = bool(IN_HOUSE.search(text)), bool(USES_VENDORS.search(text))
    return {
        "maintenance_model": ("in_house" if inh and not ven else
                              "in_house_plus_vendors" if inh and ven else
                              "vendors" if ven else "unknown"),
        "maintenance_quote": (IN_HOUSE.search(text).group(0) if inh else
                              USES_VENDORS.search(text).group(0) if ven else ""),
        "property_types": [n for n, rx in TYPE_RX if re.search(rx, text, re.I)],
    }
