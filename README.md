# Just Grit

Find local businesses, check their websites, and know exactly what to say when you
call them.

Built by [The Watson Factor](https://thewatsonfactor.dev) — San Antonio, TX.

---

## What it does

1. **Find** — search Google Maps for a type of business in a city
2. **Check** — every website is scored automatically on speed, search visibility,
   whether it converts visitors into calls, local presence, and basic trust
3. **Say** — each business gets one plain-English sentence to open a call with
4. **Track** — call, email, mark booked or not interested; the list remembers

The score is never the pitch. "Your site scored 94" opens nothing — "there's no
phone number on your homepage, so on a phone there's no way to call you" opens
everything.

## Running it

```bash
cd just_grit
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn webapp:app --host 127.0.0.1 --port 8080
```

Then open http://127.0.0.1:8080

On macOS, double-clicking **`JUST GRIT.command`** does all of that and opens the
browser for you.

### Publishing it

`Setup web address.command` puts the app behind a Cloudflare Tunnel at a real
hostname, with **Cloudflare Access** handling Google sign-in at the edge — the
request is authenticated before it ever reaches the machine, so there is no login
code in this app to get wrong. The app additionally verifies the header Access
injects and refuses non-loopback requests without it.

## Layout

```
just_grit/
  webapp.py            the app — API, database, Google Places, email drafts
  static/app.html      the entire interface, one file
  grit_analyzer/       the site-scoring engine
    fetch.py           network layer: timing, redirects, robots.txt, sitemap
    checks.py          the checks, one function per category
    analyzer.py        scoring, plain-spoken phrasing, verdicts
    report.py          the client-facing HTML report
    guard.py           rate limiting + domain suppression
    db.py              scan history
  api.py               the older standalone analyzer service
advanced/              earlier command-line versions, kept for reference
```

## Ground rules in the scanner

Non-negotiable, and they are why this can be pointed at strangers' websites:

- Honors `robots.txt`, and identifies itself honestly in the User-Agent
- No authentication, no form submission, no vulnerability probing
- No personal data — it reads public business information off a public page
- Every finding must answer three things: **what we found** (a fact with a number),
  **why it matters** (in customers or dollars, never jargon), and **the fix** (an
  instruction you can hand to a developer). A check that can't answer all three
  doesn't ship.

False positives are the bug class that matters. A wrong finding in a cold email
doesn't just fail to convert — it costs credibility you can't buy back. When in
doubt, the check stays quiet.

## Outreach rules, enforced in code

- Emails are **drafted, never sent**. A human reads and sends every one.
- Marking a business "not interested" adds its domain to a permanent
  do-not-contact list, checked before anything is written.
- Copying an email marks the record, so nobody gets contacted twice.
- The CAN-SPAM mailing address is a required setting, and drafts say so when it's
  missing.
- **No SMS to cold prospects.** TCPA damages run $500–1,500 per message.

## Notes

- Load times are measured server-side, so real-world mobile is usually *slower*
  than reported. The number is the optimistic case, and the report says so.
- Google restricts how long Places content may be cached; place IDs are the
  documented exception. Treat the prospect list as a working file you refresh, not
  a database to resell.
- The Places **field mask** in `webapp.py` determines which billing tier applies.
  It is deliberately minimal. One search page is one billable request returning up
  to 20 businesses.

## Configuration

Everything lives in the app's Setup tab and is stored in `justgrit.db`, which is
gitignored. Nothing sensitive belongs in this repo.
