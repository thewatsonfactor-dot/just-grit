#!/usr/bin/env python3
"""
Just Grit — outreach email builder (The Watson Factor)

Same CSV as the call sheet. Writes one ready-to-send email per prospect,
personalized off their actual site scan, with the compliance footer already
in place.

    python emailer.py prospects_restaurants.csv --vertical restaurant

It writes files. It does NOT send. Sending is a human decision, every time —
one bad automated blast costs you a domain reputation you can't buy back.

Guardrails, all on by default:
  • suppression list checked before anything is written
  • outreach_log.csv records who was contacted and when — no double-touching
  • CAN-SPAM footer (physical address + how to opt out) on every message
  • no email written for a prospect whose site failed to scan (nothing to say)
"""
from __future__ import annotations

import argparse, csv, datetime as dt, json, pathlib, sys
import urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "http://127.0.0.1:8080"

# ── EDIT THESE ONCE ─────────────────────────────────────────────────────────
SENDER      = "<<YOUR NAME>>"
COMPANY     = "The Watson Factor Development"
PHONE       = "<<YOUR PHONE>>"
SITE        = "<<YOUR SITE>>"
# CAN-SPAM requires a real physical postal address in every commercial email.
ADDRESS     = "<<YOUR STREET ADDRESS>>, San Antonio, TX <<ZIP>>"
# ────────────────────────────────────────────────────────────────────────────

SUPPRESSION = "suppressed_domains.txt"
LOG         = "outreach_log.csv"

ANGLE = {
    "restaurant":    "We build ordering apps for restaurants — the kind where the "
                     "order comes to you and the customer stays yours.",
    "construction":  "We build custom software for construction companies — mostly "
                     "taking the scheduling and paperwork off whiteboards and spreadsheets.",
    "appointment":   "We build booking apps for appointment businesses — online booking, "
                     "automatic reminders, fewer no-shows.",
    "multilocation": "We build dashboards for multi-location operators — one screen "
                     "instead of calling each location for numbers.",
    "generic":       "We build custom software and automation for local businesses.",
}


def load_suppression():
    p = pathlib.Path(SUPPRESSION)
    if not p.exists():
        return set()
    return {l.strip().lower().replace("www.", "")
            for l in p.read_text().splitlines()
            if l.strip() and not l.startswith("#")}


def already_contacted():
    p = pathlib.Path(LOG)
    if not p.exists():
        return {}
    out = {}
    for r in csv.DictReader(open(p, encoding="utf-8")):
        out[(r.get("domain") or "").lower()] = r.get("date", "")
    return out


def scan(row):
    d = (row.get("domain") or "").strip()
    if not d:
        return None
    try:
        u = f"{API}/analyze?" + urllib.parse.urlencode({"url": d, "fast": "true"})
        with urllib.request.urlopen(u, timeout=90) as r:
            return {**row, "rep": json.load(r)}
    except Exception as e:
        return {**row, "rep": {"ok": False, "error": str(e)}}


def build(row, vertical):
    rep = row["rep"]
    company = row.get("company") or row["domain"]
    say = (row.get("say") or "").strip()

    problems = [f for f in (rep.get("findings") or []) if f.get("severity") != "good"]
    top = [f["title"] for f in problems[:3]]

    if say:
        hook = say
    elif top:
        t = top[0]
        hook = t[0].lower() + t[1:]
    else:
        hook = "a couple of small things worth a look"

    subject = f"{company} — one thing I noticed"

    bullets = "\n".join(f"  • {t}" for t in top) if top else ""
    findings_block = (
        f"\nWhile I was in there the scan also flagged:\n{bullets}\n"
        if bullets else "\n")

    body = f"""Hi,

I run a small software shop here in San Antonio. I was looking at {row['domain']} \
this morning and noticed {hook}.

Not a sales email — I just built a tool that scores local business websites and \
yours came through it, so I figured I'd pass along what it found.
{findings_block}
{ANGLE.get(vertical, ANGLE['generic'])} If that's ever worth a conversation, I'm easy \
to reach. If not, no hard feelings — just tell me and I won't email you again.

{SENDER}
{COMPANY}
{PHONE} · {SITE}

—
{ADDRESS}
Reply "stop" and I'll remove you permanently. You're getting this once.
"""
    return subject, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--vertical", default="generic", choices=list(ANGLE))
    ap.add_argument("--outdir", default="emails")
    a = ap.parse_args()

    if "<<" in ADDRESS:
        print("\n  ⚠  Set ADDRESS at the top of emailer.py before sending anything.")
        print("     CAN-SPAM requires a real physical postal address in commercial email.\n")

    rows = [r for r in csv.DictReader(open(a.csv, encoding="utf-8-sig"))
            if (r.get("domain") or "").strip()]
    supp, seen = load_suppression(), already_contacted()

    skipped = []
    todo = []
    for r in rows:
        d = r["domain"].lower().replace("www.", "")
        if d in supp:
            skipped.append((r.get("company", d), "on suppression list")); continue
        if d in seen:
            skipped.append((r.get("company", d), f"already contacted {seen[d]}")); continue
        todo.append(r)

    if not todo:
        print("Nothing to write — everything is suppressed or already contacted.")
        for c, why in skipped:
            print(f"   skip  {c:<34}{why}")
        return

    print(f"Scanning {len(todo)} sites…")
    with ThreadPoolExecutor(max_workers=5) as p:
        items = [x for x in p.map(scan, todo) if x]

    outdir = pathlib.Path(a.outdir); outdir.mkdir(exist_ok=True)
    written = []
    for it in items:
        if not it["rep"].get("ok"):
            skipped.append((it.get("company", it["domain"]), "site did not scan"))
            continue
        subj, body = build(it, a.vertical)
        slug = it["domain"].replace(".", "-")
        f = outdir / f"{slug}.txt"
        to = (it.get("email") or "").strip() or "<<NO EMAIL — find one or call instead>>"
        f.write_text(f"To: {to}\nSubject: {subj}\n\n{body}", encoding="utf-8")
        written.append((it, subj, to))

    for c, why in skipped:
        print(f"   skip  {c:<34}{why}")
    print(f"\n  {len(written)} emails written to {outdir}/\n")
    for it, subj, to in written:
        flag = "" if "@" in to else "   ← needs an address"
        print(f"   {it.get('company', it['domain'])[:32]:<34}{to[:34]:<36}{flag}")

    print(f"\n  Review every one before sending. When you send, log it:")
    print(f"     python emailer.py --log <domain>\n")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--log":
        p = pathlib.Path(LOG)
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["domain", "date"])
            w.writerow([sys.argv[2].lower().replace("www.", ""), dt.date.today()])
        print(f"logged {sys.argv[2]}")
    else:
        main()
