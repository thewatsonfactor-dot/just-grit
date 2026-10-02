#!/usr/bin/env python3
"""
Just Grit — prospect finder (Google Places API, New)

Turns a search phrase into a ready-to-call CSV: name, phone, website, rating,
review count, address — plus an auto-written opener for the obvious gaps.

    # Single city (the original way — still works exactly as before):
    python findprospects.py "chinese restaurant" --city "San Antonio, TX"
    python findprospects.py "barber shop" --city "New Braunfels, TX" --pages 3
    python findprospects.py "roofing contractor" --city "San Antonio, TX" --min-reviews 20

    # NEW — geographic slicing: sweep every ZIP in a whole metro at once:
    python findprospects.py "roofing contractor" --metro san-antonio
    python findprospects.py "barber shop" --metro corridor --pages 3
    python findprospects.py "hvac" --zips 78130,78132,78155        # ad-hoc list

    # See exactly what it WOULD search — and how many API calls that is —
    # WITHOUT spending a cent:
    python findprospects.py "roofing contractor" --metro san-antonio --dry-run

Output drops into prospects_<slug>.csv (single city) or
prospects_<slug>_<metro>.csv (metro sweep), which RUN_CALL_SHEET.command
picks up automatically.

── HOW SLICING WORKS ────────────────────────────────────────────────────────
A single "roofing contractor in San Antonio" search maxes out at ~60 results,
no matter how big the city is. Slicing gets around that by running the search
once PER ZIP CODE — "roofing contractor in 78201", "...78202", and so on — so
each ZIP gives you its own ~60. The ZIP lists live in metros.json next to this
script; edit that file to change coverage, no Python required.

Three things make a big sweep safe to run:
  • Dedupe across ZIPs — neighboring ZIPs return a lot of the same businesses;
    they're collapsed by place_id and by website domain so the CSV is clean.
  • Resume — results are written to the CSV after every ZIP, and finished ZIPs
    are recorded in a .progress file. If the run dies (or you Ctrl-C it),
    just run the same command again and it picks up where it left off.
  • Rate limiting — a short pause between ZIPs so you're not hammering Google.

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
3-page run is 3 requests, not 60.

With slicing, the math is: (number of ZIPs) × (pages). A 58-ZIP San Antonio
sweep at 2 pages is up to 116 requests. Always run --dry-run first to see the
exact number, and check current rates + your free monthly allowance in the
Google Cloud console before big sweeps.

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


class FatalConfig(Exception):
    """Raised for errors that would fail EVERY search (bad key, API not
    enabled, bad field mask). No point continuing the sweep — we stop."""


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


def search(query, key, pages=2, region="us", page_pause=2.0, retries=2):
    """One request per page, up to 20 places each.

    Returns a list of place dicts. Raises FatalConfig for errors that would
    doom the whole run (403/400). Transient hiccups (429 rate-limit, 5xx,
    network blips) are retried with a short backoff, then that page is skipped
    so a single bad ZIP can't sink an entire metro sweep.
    """
    out, token = [], None
    for page in range(pages):
        body = {"textQuery": query, "regionCode": region, "languageCode": "en"}
        if token:
            body["pageToken"] = token

        data = None
        for attempt in range(retries + 1):
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
                break  # success
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                if e.code == 403:
                    raise FatalConfig(
                        "403 from Google. Usually: Places API (New) not enabled, "
                        f"or the key is restricted to a different API.\n\n  {detail}")
                if e.code == 400:
                    raise FatalConfig(
                        "400 from Google — check the field mask or query.\n\n"
                        f"  {detail}")
                if e.code == 429:
                    wait = 5 * (attempt + 1)
                    print(f"      rate-limited (429) — waiting {wait}s and retrying")
                    time.sleep(wait)
                    continue
                # 5xx and anything else: brief pause, then retry
                print(f"      HTTP {e.code} — retrying in 3s")
                time.sleep(3)
                continue
            except Exception as e:
                print(f"      couldn't reach Google ({type(e).__name__}) — retrying in 3s")
                time.sleep(3)
                continue

        if data is None:
            print("      gave up on this page after retries — moving on")
            break

        got = data.get("places", []) or []
        out.extend(got)
        token = data.get("nextPageToken")
        print(f"      page {page + 1}: {len(got)} places"
              + ("" if token else "  (no more)"))
        if not token:
            break
        time.sleep(page_pause)   # next_page_token needs a moment to become valid
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
        return ("HOT — no website on the Google profile",
                "your Google listing doesn't link to a website — anyone who finds "
                "you on Maps gets a phone number and nothing else")

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


# ── Slicing helpers ──────────────────────────────────────────────────────────

CSV_COLS = ["company", "domain", "phone", "city", "gap", "say", "email",
            "rating", "reviews", "address", "place_id", "source_zip"]


def load_metros(path):
    """Load metros.json. Returns {} if the file isn't there yet."""
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"\n  metros.json is not valid JSON: {e}\n")
    # drop comment keys (anything starting with '_')
    return {k: v for k, v in data.items() if not k.startswith("_")}


def build_units(a, metros):
    """Figure out the list of searches to run.

    Returns (units, out_path, progress_path) where units is a list of
    (label, query_string). label is the ZIP (or 'city') and is what gets
    recorded as done in the progress file and stored in the source_zip column.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", a.query.lower()).strip("-")

    # 1) explicit ad-hoc ZIP list
    if a.zips:
        zips = [z.strip() for z in a.zips.split(",") if z.strip()]
        state = a.state or "TX"
        units = [(z, f"{a.query} in {z}, {state}") for z in zips]
        out = pathlib.Path(a.out or f"prospects_{slug}_zips.csv")
        prog = pathlib.Path(f".progress_{slug}_zips.json")
        return units, out, prog

    # 2) a named metro from metros.json
    if a.metro:
        if not metros:
            sys.exit("\n  --metro needs metros.json next to the script, and I "
                     "couldn't find it (or it was empty).\n")
        key = a.metro.lower()
        if key not in metros:
            avail = ", ".join(sorted(metros)) or "(none)"
            sys.exit(f"\n  Unknown metro '{a.metro}'. Available: {avail}\n")
        m = metros[key]
        state = a.state or m.get("state", "TX")
        zips = list(m.get("zips", []))
        if not zips:
            sys.exit(f"\n  Metro '{a.metro}' has no ZIPs listed in metros.json.\n")
        units = [(z, f"{a.query} in {z}, {state}") for z in zips]
        out = pathlib.Path(a.out or f"prospects_{slug}_{key}.csv")
        prog = pathlib.Path(f".progress_{slug}_{key}.json")
        return units, out, prog

    # 3) original single-city behaviour (unchanged)
    units = [("city", f"{a.query} in {a.city}")]
    out = pathlib.Path(a.out or f"prospects_{slug}.csv")
    prog = pathlib.Path(f".progress_{slug}_city.json")
    return units, out, prog


def load_progress(prog_path):
    if prog_path.exists():
        try:
            return set(json.loads(prog_path.read_text()).get("completed", []))
        except Exception:
            return set()
    return set()


def save_progress(prog_path, completed):
    prog_path.write_text(json.dumps({"completed": sorted(completed)}, indent=0))


def load_seen_from_csv(out_path):
    """Rebuild the dedupe sets from an existing CSV so a resumed run doesn't
    re-add businesses it already wrote."""
    seen_ids, seen_domains = set(), set()
    if out_path.exists():
        try:
            for r in csv.DictReader(open(out_path, encoding="utf-8-sig")):
                if r.get("place_id"):
                    seen_ids.add(r["place_id"])
                if r.get("domain"):
                    seen_domains.add(r["domain"].lower())
        except Exception:
            pass
    return seen_ids, seen_domains


def rows_from_places(places, a, city_label, seen_ids, seen_domains, source_zip):
    """Filter + dedupe a batch of places into CSV rows. Mutates the seen sets."""
    # count repeated names *within this batch* so multi-location operators get
    # flagged (same as the original behaviour).
    locations = Counter((p.get("displayName") or {}).get("text", "") for p in places)

    rows, skipped = [], 0
    for p in places:
        if p.get("businessStatus") not in (None, "OPERATIONAL"):
            skipped += 1
            continue
        reviews = p.get("userRatingCount") or 0
        if a.min_reviews and reviews < a.min_reviews:
            skipped += 1; continue
        if a.max_reviews and reviews > a.max_reviews:
            skipped += 1; continue

        pid = p.get("id", "")
        if pid and pid in seen_ids:
            continue

        name = (p.get("displayName") or {}).get("text", "").strip()
        dom = domain_of(p.get("websiteUri"))
        gap, say = auto_gap(p, locations)

        if dom:
            if dom in seen_domains:
                continue
            seen_domains.add(dom)
        if pid:
            seen_ids.add(pid)

        rows.append({
            "company": name,
            "domain": dom,
            "phone": p.get("nationalPhoneNumber") or "",
            "city": city_label,
            "gap": gap,
            "say": say,
            "email": "",
            "rating": p.get("rating") or "",
            "reviews": reviews,
            "address": p.get("formattedAddress") or "",
            "place_id": pid,
            "source_zip": source_zip,
        })
    return rows, skipped


def print_summary(out_path):
    """Read the finished CSV and print the hot lists (works after a resume,
    since it reads everything on disk, not just this run's rows)."""
    if not out_path.exists():
        return
    rows = list(csv.DictReader(open(out_path, encoding="utf-8-sig")))
    print(f"\n  {len(rows)} prospects total → {out_path}")

    hot = [r for r in rows if (r.get("gap") or "").startswith("HOT")]
    thirdparty = [r for r in rows if "points at" in (r.get("gap") or "")]
    multi = [r for r in rows if "locations found" in (r.get("gap") or "")]

    if hot:
        print(f"\n  🔥 {len(hot)} with NO website at all — call these first:")
        for r in hot[:12]:
            print(f"     {r['company'][:36]:<38}{r['phone']}")
    if thirdparty:
        print(f"\n  {len(thirdparty)} don't own their web presence:")
        for r in thirdparty[:12]:
            print(f"     {r['company'][:36]:<38}{r['domain']}")
    if multi:
        print(f"\n  {len(multi)} multi-location operators:")
        for r in multi[:12]:
            print(f"     {r['company'][:36]:<38}{r['gap']}")

    print(f"\n  Next:  double-click RUN_CALL_SHEET.command and pick this list.\n")


def main():
    ap = argparse.ArgumentParser(
        description="Find prospects from Google Places — one city, or a whole "
                    "metro sliced by ZIP code.")
    ap.add_argument("query", help='e.g. "chinese restaurant" or "roofing contractor"')

    # location: pick ONE of these (or none, for the classic single-city run)
    ap.add_argument("--city", default="San Antonio, TX",
                    help="single-city search (the original behaviour)")
    ap.add_argument("--metro",
                    help="sweep every ZIP in a metro from metros.json "
                         "(e.g. san-antonio, new-braunfels, seguin, corridor)")
    ap.add_argument("--zips",
                    help='ad-hoc comma-separated ZIP list, e.g. "78130,78132,78155"')
    ap.add_argument("--state", default=None,
                    help="state appended to ZIP searches (default: metro's state, or TX)")

    ap.add_argument("--pages", type=int, default=2,
                    help="1 page = 1 billable request = up to 20 places (max 3), PER ZIP")
    ap.add_argument("--min-reviews", type=int, default=0)
    ap.add_argument("--max-reviews", type=int, default=0,
                    help="filter out the big chains, e.g. --max-reviews 2000")
    ap.add_argument("--out", default=None)

    # slicing controls
    ap.add_argument("--metros-file",
                    default=str(pathlib.Path(__file__).parent / "metros.json"),
                    help="where the metro→ZIP definitions live")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="seconds to pause between ZIPs (be kind to the API)")
    ap.add_argument("--max-zips", type=int, default=0,
                    help="only run the first N ZIPs (handy for a cheap test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the searches + API-call count and exit — NO calls, NO cost")
    ap.add_argument("--restart", action="store_true",
                    help="ignore any saved progress and start the sweep from scratch")
    args = ap.parse_args()

    metros = load_metros(args.metros_file)
    units, out, prog = build_units(args, metros)

    # City label stored in the 'city' column. For metro/zip runs, use the metro
    # label if we have one, else the ZIP list header.
    if args.metro and args.metro.lower() in metros:
        city_label = metros[args.metro.lower()].get("label", args.metro)
    elif args.zips:
        city_label = "custom ZIPs"
    else:
        city_label = args.city

    pages = min(args.pages, 3)

    # ── dry run: show the plan, spend nothing ────────────────────────────────
    if args.dry_run:
        shown = units if args.max_zips <= 0 else units[:args.max_zips]
        print(f"\n  DRY RUN — no API calls, no cost.\n")
        print(f"  Query phrase : {args.query!r}")
        print(f"  Searches     : {len(shown)} ZIP(s) × {pages} page(s) "
              f"= up to {len(shown) * pages} billable requests")
        print(f"  Output would : {out}")
        print(f"\n  First few searches:")
        for label, q in shown[:8]:
            print(f"     [{label}]  {q}")
        if len(shown) > 8:
            print(f"     … and {len(shown) - 8} more")
        print(f"\n  Run it for real by dropping --dry-run.\n")
        return

    key = get_key()

    # ── resume bookkeeping ──────────────────────────────────────────────────
    completed = set() if args.restart else load_progress(prog)
    if args.restart and out.exists():
        # start the CSV fresh too
        out.unlink()
    seen_ids, seen_domains = load_seen_from_csv(out)

    todo = [(lbl, q) for (lbl, q) in units if lbl not in completed]
    if args.max_zips > 0:
        todo = todo[:args.max_zips]

    if not todo:
        print(f"\n  Nothing to do — all {len(units)} ZIP(s) already completed.")
        print(f"  (Use --restart to run the whole sweep again.)")
        print_summary(out)
        return

    print(f"\n  Sweeping {len(todo)} ZIP(s)"
          + (f"  ({len(completed)} already done, skipping)" if completed else "")
          + f"  ·  {pages} page(s) each  ·  up to {len(todo) * pages} requests\n")

    # open CSV in append mode; write the header only if the file is new/empty
    new_file = (not out.exists()) or out.stat().st_size == 0
    csv_fh = open(out, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_fh, fieldnames=CSV_COLS)
    if new_file:
        writer.writeheader()
        csv_fh.flush()

    total_new = 0
    try:
        for i, (label, q) in enumerate(todo, 1):
            print(f"  [{i}/{len(todo)}]  {q}")
            try:
                places = search(q, key, pages=pages)
            except FatalConfig as e:
                # Something that would break every search. Stop, but everything
                # so far is already saved — rerun to resume once it's fixed.
                csv_fh.close()
                sys.exit(f"\n  Stopping — {e}\n\n  Progress saved; fix the above "
                         f"and rerun the same command to resume.\n")

            rows, skipped = rows_from_places(
                places, args, city_label, seen_ids, seen_domains, label)
            if rows:
                writer.writerows(rows)
                csv_fh.flush()          # persist after every ZIP → safe to resume
            total_new += len(rows)

            completed.add(label)
            save_progress(prog, completed)

            note = f"+{len(rows)} new"
            if skipped:
                note += f", {skipped} filtered"
            print(f"          {note}   (running total: {total_new})")

            if i < len(todo) and args.sleep > 0:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        csv_fh.close()
        print(f"\n\n  Stopped by you. {total_new} new prospect(s) saved so far.")
        print(f"  Rerun the same command to pick up where you left off.\n")
        return
    finally:
        if not csv_fh.closed:
            csv_fh.close()

    print(f"\n  Done. {total_new} new this run.")
    print_summary(out)


if __name__ == "__main__":
    main()
