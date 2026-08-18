#!/usr/bin/env python3
"""
Just Grit — prospect finder (Google Places API, New)

Turns a search phrase into a ready-to-call CSV: name, phone, website, rating,
review count, address — plus an auto-written opener for the obvious gaps.

    python findprospects.py "chinese restaurant" --city "San Antonio, TX"
    python findprospects.py "barber shop" --city "New Braunfels, TX" --pages 3
    python findprospects.py "roofing contractor" --city "San Antonio, TX" --min-reviews 20

Output drops straight into prospects_<slug>.csv, which RUN_CALL_SHEET.command
picks up automatically.

── API KEY ─────────────────────────────────────────────────────────────────
Get one at console.cloud.google.com → APIs & Services → Credentials.
Enable "Places API (New)". Then either:

    export GOOGLE_MAPS_API_KEY="AIza..."

or drop the key in a file next to this script:

    echo "AIza..." > ~/JustGrit/.google_api_key

Restrict the key to the Places API in the console. An unrestricted key that
leaks is somebody else's bill.

── COST ────────────────────────────────────────────────────────────────────
The FIELD MASK below is what decides which SKU you get billed at — asking for
fewer fields is literally cheaper, which is why the mask here is deliberately
minimal. Each page is one billable request and returns up to 20 places, so a
3-page run is 3 requests, not 60. Check current rates and your free monthly
allowance in the Google Cloud console before running big sweeps.

── TERMS ───────────────────────────────────────────────────────────────────
Google restricts how long you may cache Places content; place IDs are the
documented exception. Treat these CSVs as a working call list you refresh, not
a permanent database you resell. Read the current Places API terms before you
build anything durable on top of this.
"""
from __future__ import annotations

import argparse, csv, json, os, pathlib, re, sys, time
import urllib.error, urllib.request
from collections import Counter

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

# Minimal on purpose — every extra field can bump the billing tier.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.businessStatus",
    "places.primaryTypeDisplayName",
    "nextPageToken",
])


def get_key():
    k = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if k:
        return k
    f = pathlib.Path(__file__).parent / ".google_api_key"
    if f.exists():
        k = f.read_text().strip()
        if k:
            return k
    sys.exit(
        "\n  No API key found.\n\n"
        "  Either:  export GOOGLE_MAPS_API_KEY='AIza...'\n"
        "  Or:      echo 'AIza...' > ~/JustGrit/.google_api_key\n\n"
        "  Get one at console.cloud.google.com — enable 'Places API (New)'.\n")


def search(query, key, pages=2, region="us"):
    """One request per page, up to 20 places each."""
    out, token = [], None
    for page in range(pages):
        body = {"textQuery": query, "regionCode": region, "languageCode": "en"}
        if token:
            body["pageToken"] = token
        req = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 403:
                sys.exit(f"\n  403 from Google. Usually: Places API (New) not enabled, "
                         f"or the key is restricted to a different API.\n\n  {detail}\n")
            if e.code == 400:
                sys.exit(f"\n  400 from Google — check the field mask or query.\n\n  {detail}\n")
            sys.exit(f"\n  HTTP {e.code} from Google:\n\n  {detail}\n")
        except Exception as e:
            sys.exit(f"\n  Could not reach Google: {type(e).__name__}: {e}\n")

        got = data.get("places", []) or []
        out.extend(got)
        token = data.get("nextPageToken")
        print(f"    page {page + 1}: {len(got)} places"
              + ("" if token else "  (no more)"))
        if not token:
            break
        time.sleep(2)   # next_page_token needs a moment to become valid
    return out


def domain_of(url):
    if not url:
        return ""
    m = re.sub(r"^https?://", "", url.strip()).split("/")[0].lower()
    return m.replace("www.", "")


# Third-party ordering / booking hosts. If a "website" points at one of these,
# the business doesn't actually own its online presence — which IS the pitch.
THIRD_PARTY = {
    "order.online": "DoorDash's white-label storefront",
    "doordash.com": "DoorDash",
    "ubereats.com": "Uber Eats",
    "grubhub.com": "Grubhub",
    "toasttab.com": "Toast",
    "clover.com": "Clover",
    "square.site": "Square's hosted page",
    "facebook.com": "a Facebook page",
    "instagram.com": "an Instagram profile",
    "linktr.ee": "a Linktree",
    "booksy.com": "Booksy",
    "styleseat.com": "StyleSeat",
    "vagaro.com": "Vagaro",
    "squareup.com": "Square",
    "wixsite.com": "a free Wix subdomain",
    "business.site": "a Google Business auto-site",
}


def auto_gap(p, locations):
    """Write the gap note and the spoken line, where the data makes it obvious."""
    site = p.get("websiteUri") or ""
    dom = domain_of(site)
    name = (p.get("displayName") or {}).get("text", "")
    reviews = p.get("userRatingCount") or 0
    rating = p.get("rating")

    for host, label in THIRD_PARTY.items():
        if host in dom:
            return (f"No owned site — points at {label}",
                    f"the website listed on your Google profile is actually {label}, "
                    f"not something you own")

    if not site:
        return ("HOT — no website at all on the Google profile",
                "you don't have a website listed on your Google profile at all — "
                "people searching for you find a phone number and nothing else")

    n = locations.get(name, 1)
    if n >= 3:
        return (f"{n} locations found in this search",
                f"you've got at least {n} locations showing up, and I couldn't find "
                f"one place where you can see all of them at once")

    if reviews and reviews >= 200 and rating and rating >= 4.3:
        return (f"{reviews} reviews at {rating}★ — strong reputation",
                f"you've got {reviews} reviews sitting at {rating} stars, and almost "
                f"none of that is showing up on your own website")

    return ("", "")   # let the site scanner find the angle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help='e.g. "chinese restaurant" or "roofing contractor"')
    ap.add_argument("--city", default="San Antonio, TX")
    ap.add_argument("--pages", type=int, default=2,
                    help="1 page = 1 billable request = up to 20 places (max 3)")
    ap.add_argument("--min-reviews", type=int, default=0)
    ap.add_argument("--max-reviews", type=int, default=0,
                    help="filter out the big chains, e.g. --max-reviews 2000")
    ap.add_argument("--out", default=None)
    ap.add_argument("--append", action="store_true",
                    help="add to the file instead of replacing it")
    a = ap.parse_args()

    key = get_key()
    q = f"{a.query} in {a.city}"
    print(f"\n  Searching Google Places: {q!r}")
    places = search(q, key, pages=min(a.pages, 3))

    # count repeated names so multi-location operators get flagged
    locations = Counter((p.get("displayName") or {}).get("text", "") for p in places)

    slug = re.sub(r"[^a-z0-9]+", "-", a.query.lower()).strip("-")
    out = pathlib.Path(a.out or f"prospects_{slug}.csv")

    seen_domains, seen_ids = set(), set()
    if a.append and out.exists():
        for r in csv.DictReader(open(out, encoding="utf-8-sig")):
            if r.get("domain"):
                seen_domains.add(r["domain"].lower())
            if r.get("place_id"):
                seen_ids.add(r["place_id"])

    rows, no_site, skipped = [], [], 0
    for p in places:
        if p.get("businessStatus") not in (None, "OPERATIONAL"):
            skipped += 1
            continue
        name = (p.get("displayName") or {}).get("text", "").strip()
        pid = p.get("id", "")
        reviews = p.get("userRatingCount") or 0
        if a.min_reviews and reviews < a.min_reviews:
            skipped += 1; continue
        if a.max_reviews and reviews > a.max_reviews:
            skipped += 1; continue
        if pid in seen_ids:
            continue

        dom = domain_of(p.get("websiteUri"))
        gap, say = auto_gap(p, locations)

        if not dom:
            no_site.append((name, p.get("nationalPhoneNumber") or "—"))
        elif dom in seen_domains:
            continue
        else:
            seen_domains.add(dom)
        seen_ids.add(pid)

        rows.append({
            "company": name,
            "domain": dom,
            "phone": p.get("nationalPhoneNumber") or "",
            "city": a.city,
            "gap": gap,
            "say": say,
            "email": "",
            "rating": p.get("rating") or "",
            "reviews": reviews,
            "address": p.get("formattedAddress") or "",
            "place_id": pid,
        })

    cols = ["company", "domain", "phone", "city", "gap", "say", "email",
            "rating", "reviews", "address", "place_id"]
    mode = "a" if (a.append and out.exists()) else "w"
    with open(out, mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        if mode == "w":
            w.writeheader()
        w.writerows(rows)

    print(f"\n  {len(rows)} prospects → {out}")
    if skipped:
        print(f"  {skipped} filtered out (closed, or outside the review range)")

    hot = [r for r in rows if r["gap"].startswith("HOT")]
    thirdparty = [r for r in rows if "points at" in r["gap"]]
    multi = [r for r in rows if "locations found" in r["gap"]]

    if hot:
        print(f"\n  🔥 {len(hot)} with NO website at all — call these first:")
        for r in hot[:8]:
            print(f"     {r['company'][:36]:<38}{r['phone']}")
    if thirdparty:
        print(f"\n  {len(thirdparty)} don't own their web presence:")
        for r in thirdparty[:8]:
            print(f"     {r['company'][:36]:<38}{r['domain']}")
    if multi:
        print(f"\n  {len(multi)} multi-location operators:")
        for r in multi[:8]:
            print(f"     {r['company'][:36]:<38}{r['gap']}")

    print(f"\n  Next:  double-click RUN_CALL_SHEET.command and pick this list.\n")


if __name__ == "__main__":
    main()
