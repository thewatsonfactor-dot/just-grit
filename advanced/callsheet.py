#!/usr/bin/env python3
"""
Just Grit — Daily Call Sheet generator (The Watson Factor)

Takes a prospect CSV, scores every site against the local analyzer, and writes
a printable call sheet: who to dial today, the one specific fact to open with,
and the exact words to say.

    python callsheet.py prospects.csv --calls 8

Input CSV needs at minimum a `domain` column. `company`, `phone`, `employees`,
`revenue`, and `city` are used when present.

The sheet leads with the FINDING, never the score. A score of 94 opens nothing;
"you have no way to capture a lead who won't call you" opens everything.
"""
from __future__ import annotations

import argparse, csv, datetime as dt, html, json, pathlib, sys
import urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "http://127.0.0.1:8080"

# ── Which findings actually open a phone call, best first. ──────────────────
# Matched on TITLE TEXT, not id — the analyzer's ids change between versions and
# v2.1 emits none at all. Titles are stable because they're written for humans.
# Ordered by how fast a busy owner understands the problem. Anything jargony
# ("LocalBusiness structured data") is deliberately absent: it is a real finding
# but an unsayable sentence, and a bad opener costs you the call.
OPENERS = [
 ("no phone number",      "there's no phone number anywhere on your homepage"),
 ("not set up for mobile","your site isn't built for phones — people have to pinch and zoom to read it"),
 ("tap-to-call",          "your phone number isn't tappable when someone's on their phone"),
 ("not use https",        "your site shows a 'Not secure' warning in the browser"),
 ("insecure connection",  "your contact form is sending customer info unencrypted"),
 ("no contact form",      "there's no contact form — if somebody won't call, you never hear from them"),
 ("no analytics",         "there's no analytics on the site at all, so nothing you do is being measured"),
 ("tells google not to",  "your site is currently telling Google not to list it"),
 ("took",                 "your site takes {load} seconds to load"),
 ("slow server response", "your server takes {ttfb}ms just to start responding"),
 ("very heavy",           "your homepage is heavy enough that it really drags on a phone"),
 ("block the page",       "you've got files blocking the page from showing up at all"),
 ("no call-to-action",    "there's nothing near the top telling a visitor what to actually do"),
 ("no retargeting",       "you've got no retargeting pixel, so anybody who visits and leaves is gone for good"),
 ("reviews aren't shown", "none of your reviews are on your own website"),
 ("no trust signals",     "there's nothing on the page telling people why they should trust you"),
 ("no search-result description",
                          "when you show up in Google there's no description under your name — Google makes one up"),
 ("not lazy-loaded",      "your page downloads every single photo up front, even the ones nobody scrolls to"),
 ("no named service area","your site never says which parts of town you actually serve"),
]

VERTICALS = {
 "construction": {
   "label": "Construction / trades",
   "pivot": "when a job comes in, how does it get from that phone call to somebody actually scheduled and on site?",
   "listen": "spreadsheets, whiteboards, \"we just call each other,\" double entry, someone re-typing the same job into three places",
   "we_do":  "we build custom software for construction companies",
 },
 "restaurant": {
   "label": "Restaurants / food service",
   "pivot": "how do people order from you right now — phone, your own site, or the third-party apps?",
   "listen": "DoorDash/UberEats taking 15-30% per order, phone orders written on paper, no way to reach past customers, menu updates that take a week",
   "we_do":  "we build ordering apps and custom software for restaurants",
 },
 "appointment": {
   "label": "Barbershops / salons / appointment businesses",
   "pivot": "how do people book with you — do they call, DM you, or is there an app?",
   "listen": "booking by text or Instagram DM, no-shows with no reminders, a paper book, one person fielding every message",
   "we_do":  "we build booking and customer apps for appointment businesses",
 },
 "multilocation": {
   "label": "Multi-location operators",
   "pivot": "how do you see what's happening across all your locations — do you have to call each one to find out?",
   "listen": "calling each store for numbers, separate spreadsheets per location, no single dashboard, reports that arrive days late",
   "we_do":  "we build dashboards and operations software for multi-location businesses",
 },
 "generic": {
   "label": "General",
   "pivot": "walk me through what happens when a new customer contacts you — where does that go?",
   "listen": "manual re-entry, spreadsheets, sticky notes, one person who is the system",
   "we_do":  "we build custom software and automation",
 },
}

SCRIPT = """OPEN (do not pitch)
  "Hi, is {first_contact} around? — My name's Daniel with The Watson Factor,
   we're a software shop here in San Antonio. This isn't a sales call, I'll be
   30 seconds. I was looking at {company}'s website this morning and noticed
   {finding}. Is that something you already know about?"

THEN SHUT UP. Let them answer.

IF THEY ENGAGE — pivot off the website to the real question:
  "Honestly the website's the small stuff. The reason I called — {we_do}.
   Can I ask, {pivot}"

LISTEN FOR: {listen}. That is the deal.

THE ASK (never pitch a build on call one):
  "That's the exact thing we fix. What I'd suggest — we do a paid operations
   review, $1,500. I spend 90 minutes with you and whoever runs the day-to-day,
   map where the time goes, and you get a written breakdown of what to fix and
   what it'd cost. If you build it with us, the $1,500 comes off the price.
   Worth putting on the calendar?"

IF NO / NOT NOW:
  "Fair enough. Want me to send the site report anyway? Costs you nothing and
   your web guy can act on it." -> get the email, send it, log it, move on.

NEVER: text them without written consent. TCPA is $500-1,500 per message.
"""


def scan(row):
    d = (row.get("domain") or "").strip()
    if not d:
        return None
    try:
        u = f"{API}/analyze?" + urllib.parse.urlencode({"url": d, "fast": "true"})
        with urllib.request.urlopen(u, timeout=90) as r:
            return {**row, "rep": json.load(r)}
    except Exception as e:
        return {**row, "rep": {"ok": False, "error": f"{type(e).__name__}: {e}"}}


def pick_opener(rep, row=None):
    """The one sentence to say out loud.

    A `gap` column in the CSV always wins. That is human research — knowing a
    restaurant is stuck on DoorDash's storefront beats anything the scanner can
    infer from markup, and it opens a far better conversation.
    """
    # A `say` column is the spoken sentence, used verbatim. Research shorthand
    # ("Toast pickup + DoorDash delivery") is a note, not something you say out
    # loud — so `gap` stays internal and only `say` reaches the script.
    if row and (row.get("say") or "").strip():
        return row["say"].strip(), None

    m = rep.get("metrics") or {}
    probs = [f for f in (rep.get("findings") or []) if f.get("severity") != "good"]
    for needle, phrasing in OPENERS:
        for f in probs:
            if needle in (f.get("title") or "").lower():
                return phrasing.format(load=m.get("load_seconds", "?"),
                                       ttfb=m.get("ttfb_ms", "?")), f
    if probs:
        t = probs[0]["title"]
        return t[0].lower() + t[1:], probs[0]
    return None, None


def priority(item):
    """Worst sites first — but only counting problems an owner can hear."""
    rep = item["rep"]
    if not rep.get("ok"):
        return (0, 0)          # broken site = best call of the day
    titles = " ".join((f.get("title") or "").lower()
                      for f in (rep.get("findings") or [])
                      if f.get("severity") != "good")
    sayable = sum(1 for needle, _ in OPENERS if needle in titles)
    return (1, -sayable)


def render(items, n, today, V):
    cards = []
    for i, it in enumerate(items[:n], 1):
        rep = it["rep"]
        company = html.escape(it.get("company") or it.get("domain", ""))
        domain = html.escape(it.get("domain", ""))
        phone = html.escape(it.get("phone") or "—")
        meta = " · ".join(x for x in [it.get("employees"), it.get("revenue"),
                                      it.get("city")] if x)

        if not rep.get("ok"):
            opener = "your website didn't load at all when I tried it this morning"
            finding_block = (f'<div class="ev">Site failed to load: '
                             f'{html.escape(rep.get("error", "")[:120])}</div>')
            score = "—"
        else:
            op, f = pick_opener(rep, it)
            opener = op or "a couple of things worth a quick look"
            score = rep.get("overall", "—")
            finding_block = ""
            if f:
                finding_block = (f'<div class="ev"><b>Evidence:</b> '
                                 f'{html.escape(f.get("evidence", ""))}</div>')
            others = [x for x in (rep.get("findings") or [])
                      if x.get("severity") in ("critical", "serious")][:3]
            if others:
                finding_block += "<ul class='oth'>" + "".join(
                    f"<li>{html.escape(x['title'])}</li>" for x in others) + "</ul>"

        script = html.escape(SCRIPT.format(
            company=it.get("company") or it.get("domain", ""),
            first_contact="the owner", finding=opener,
            we_do=V["we_do"], pivot=V["pivot"], listen=V["listen"]))

        cards.append(f"""
        <div class="card">
          <div class="hd">
            <div class="num">{i}</div>
            <div class="who">
              <h2>{company}</h2>
              <div class="sub">{html.escape(domain)}{' · ' + html.escape(meta) if meta else ''}</div>
            </div>
            <div class="ph">{phone}</div>
            <div class="sc">{score}</div>
          </div>
          <div class="say"><span class="lbl">Say this</span>&ldquo;…I noticed {html.escape(opener)}&rdquo;</div>
          {finding_block}
          <details><summary>Full script</summary><pre>{script}</pre></details>
          <div class="log">
            Called ☐ &nbsp; Reached ☐ &nbsp; Emailed report ☐ &nbsp;
            Review booked ☐ &nbsp; Dead ☐ &nbsp;&nbsp; Notes: ______________________________
          </div>
        </div>""")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Call Sheet — {today}</title><style>
 body{{font-family:system-ui,-apple-system,sans-serif;max-width:860px;margin:0 auto;
   padding:28px 22px 70px;background:#0d0d0d;color:#fff;line-height:1.55}}
 h1{{font-size:25px;margin:0 0 4px;letter-spacing:-.02em}}
 .top{{color:#898781;font-size:13.5px;margin-bottom:26px}}
 .card{{background:#16161a;border:1px solid rgba(255,255,255,.1);border-radius:13px;
   padding:17px 19px;margin-bottom:14px}}
 .hd{{display:flex;align-items:center;gap:13px}}
 .num{{flex:0 0 30px;height:30px;border-radius:8px;background:#e8a33d;color:#1a1206;
   display:grid;place-items:center;font-weight:800}}
 .who{{flex:1;min-width:0}} h2{{font-size:16.5px;margin:0;font-weight:680}}
 .sub{{font-size:12.3px;color:#898781}}
 .ph{{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}}
 .sc{{font-size:12px;color:#898781;width:28px;text-align:right}}
 .say{{margin:13px 0 0;padding:12px 14px;background:rgba(232,163,61,.11);
   border-left:3px solid #e8a33d;border-radius:0 8px 8px 0;font-size:15px}}
 .lbl{{display:block;font-size:10px;letter-spacing:.11em;text-transform:uppercase;
   color:#e8a33d;font-weight:700;margin-bottom:3px}}
 .ev{{font-size:12.8px;color:#c3c2b7;margin-top:9px}}
 .oth{{margin:7px 0 0;padding-left:19px;font-size:12.5px;color:#898781}}
 details{{margin-top:11px}} summary{{cursor:pointer;font-size:12.5px;color:#e8a33d}}
 pre{{white-space:pre-wrap;font-size:12.3px;color:#c3c2b7;background:#1d1d21;
   padding:13px;border-radius:8px;margin-top:8px;font-family:ui-monospace,monospace}}
 .log{{margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08);
   font-size:12.3px;color:#898781}}
 @media print{{body{{background:#fff;color:#000;max-width:none}}
   .card{{background:#fff;border-color:#ccc;break-inside:avoid}}
   .say{{background:#f5efe3}} pre,details{{display:none}} .sc{{color:#666}}}}
</style></head><body>
<h1>Call sheet — {today}</h1>
<div class="top">{min(n, len(items))} dials. Ranked by how much is visibly wrong.
 Lead with the finding, never the score. Goal is a $1,500 operations review, not a build.<br><b>Vertical:</b> {V["label"]}</div>
{''.join(cards)}
<div class="top" style="margin-top:30px">
 The website is the door. The deal is whatever they tell you about how work gets
 from the phone call to the crew.</div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="prospect CSV with a `domain` column")
    ap.add_argument("--calls", type=int, default=8, help="how many dials today")
    ap.add_argument("--out", default=None)
    ap.add_argument("--vertical", default="generic", choices=list(VERTICALS),
                    help="picks the pivot question and what to listen for")
    a = ap.parse_args()

    V = VERTICALS[a.vertical]
    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig")))
    rows = [r for r in rows if (r.get("domain") or "").strip()]
    if not rows:
        sys.exit("No rows with a `domain` column.")

    print(f"Scanning {len(rows)} sites…")
    with ThreadPoolExecutor(max_workers=5) as p:
        items = [x for x in p.map(scan, rows) if x]

    items.sort(key=priority)
    today = dt.date.today().strftime("%A, %B %-d")
    out = a.out or f"callsheet_{dt.date.today()}.html"
    pathlib.Path(out).write_text(render(items, a.calls, today, V), encoding="utf-8")

    print(f"\nTop {min(a.calls, len(items))} for today:\n")
    for i, it in enumerate(items[:a.calls], 1):
        rep = it["rep"]
        op, _ = pick_opener(rep, it) if rep.get("ok") else ("site did not load", None)
        name = (it.get("company") or it["domain"])[:34]
        print(f"  {i}. {name:<36}{it.get('phone','—'):<16}{op}")
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
