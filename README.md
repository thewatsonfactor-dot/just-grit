# Just Grit

**A sales engine for a local-service business, run by one person.**
Just Grit finds local businesses, reads their websites, writes the first email
in the owner's own voice, sends it inside safe hours, reads the replies, and
keeps every conversation, deal, follow-up and proposal in one place.

Built by [The Watson Factor](https://thewatsonfactor.dev), New Braunfels, TX.
It runs two businesses from one codebase: The Watson Factor (custom software
for local businesses) and HomeRepair Tech (home maintenance plans, sold to
property managers, HOA boards, commercial buildings and realtors).

> Status: in daily production use by its author on a Mac. Version `1.3.1`.
> About 23,000 lines of Python, 210 routes, 72 test files, no front-end build step.

---

## What it does

**1. Find leads.** Searches Google Places for a trade in a town, reads each
business's own website, and pulls out the phone number and a real email
address. When the home towns are used up it moves on to nearby towns. A hard
monthly cap on Google requests keeps the bill at zero.

**2. Write and send.** Every lead gets an email that sounds like the owner
wrote it. The app learns the owner's voice from their sent mail, rewrites
anything that reads like a bot, and checks the draft against a "slop test"
before it can be sent. Nothing sends until a person approves it, unless the
owner switches Autopilot on.

**3. Read the replies.** The inbox is scanned and each reply is sorted: bounce,
auto-reply, out of office, "stop", or a person. "Stop" ends the sequence and
adds the domain to a suppression list that every part of the app honors.
Real replies jump to the top of the call list.

**4. Work the deal.** A CRM with stages, people, deals priced from the offer
catalog, notes, a follow-up date that shows on the Calendar, and proposals,
bids and contracts attached to the account.

**5. Market the business.** Social posts to Facebook and Instagram, campaigns
with captions written from the picture, a picture library, reputation
tracking for the business's own Google listing, and a competitor watch.

**6. Answer the phone.** Click-to-call, missed-call text-back, and webhooks
for Telnyx AI receptionists so a call turns into a lead with the caller's
details and an alert to the owner.

---

## Two businesses, no shared data

Isolation is by file, not by a `workspace` column. Each business has its own
SQLite database, so a query running for one business physically cannot see the
other's rows. A column would have needed a `WHERE` clause on hundreds of
statements, and the first one anyone forgot would have mixed the lead lists
with no error. The cost is that there is no cross-workspace query, which is
the right constraint anyway. See `just_grit/workspaces.py` for the full reasoning.

| | The Watson Factor | HomeRepair Tech |
|---|---|---|
| Sells | AI receptionist, website, reviews and rewards, ordering app, AI vision | Protect Basic, Essential and Pro plans, audits, property reports, portfolio plans |
| Sells to | Local restaurants and service businesses | Property managers, HOA and condo boards, commercial buildings, realtors, homeowners |
| Database | `justgrit.db` | `justgrit-homerepair.db` |

New workspaces can be created from Setup, and a workspace's configuration can
be exported as a snapshot file and loaded into the next customer's workspace.

---

## What's in the app

| Area | Tabs |
|---|---|
| Overview | **Today** (who to call now, replies first), **Calendar** (posts, emails, follow-ups and texts on one week) |
| Outreach | **Outbox** (drafts, autopilot, lead refill), **Leads** (one table with search, tags, CSV in and out, pipeline board, find more) |
| Marketing | **Social**, **Campaigns**, **Site checks** (the website analyzer) |
| Insights | **What's working**, **Competitors**, **Reputation**, **Adoption**, **Revenue** |
| Account | **Setup** (keys, identity, mailbox, offers, snapshots) |

Plus public Help, Terms, Privacy and Status pages, and a one-page "what we do"
sheet at `/welcome/offers` written separately for each business.

---

## The safeguards (why it is safe to leave on)

- **Human approval by default.** Drafts wait in the Outbox. Autopilot has three
  modes: off, follow-ups only, all outreach.
- **Sending windows by audience.** HOA boards get weekday evenings and
  Tuesday to Thursday mornings; offices get weekday business hours. Hourly and
  daily caps apply, and nobody gets two emails inside 48 hours.
- **Suppression is permanent and shared.** A "stop" reply, an unsubscribe, or a
  lost deal writes the domain to `suppressed_domains.txt`, which the analyzer,
  the sender and the lead finder all read.
- **Bounces end an address, not a business.** The lead moves to the next
  person at that company and the dead address is remembered.
- **No claims it can't back.** Offer copy never says "free" for something that
  costs money, never promises a repair price, and never claims a result.
- **Google spend is capped.** Every billable request is reserved against a
  monthly limit before it is made.
- **Sent equals drafted.** `sentcheck.py` compares what went out with what
  was drafted.
- **A person is always on a live conversation.** Moving a lead to Review or
  Proposal, or setting a follow-up date, stops the automatic sequence for it.
  A hand-set follow-up never sends anything by itself.

---

## The website analyzer

The original module, still shipped inside the app and usable on its own. It
scores a site out of 100 and turns each finding into a fact, why it costs the
customer money, and the fix.

Speed 25 · Search visibility 25 · Turning visitors into calls 25 ·
Local presence 15 · Trust and basics 10.

```bash
cd just_grit
python -m grit_analyzer.cli example.com --report out.html
python -m grit_analyzer.cli --batch prospects.txt        # ranked worst-first
python -m grit_analyzer.cli --compare a.com b.com c.com  # competitor mode
python -m grit_analyzer.cli --history example.com        # score over time
python -m grit_analyzer.cli --suppress somedomain.com    # never scan again
```

Endpoints: `GET /analyze`, `GET /report`, `POST /batch`, `POST /compare`,
`GET /history`, `GET /health`. Per-IP rate limits, a 15-minute cache, scans
stored in SQLite, robots.txt respected. Full notes in `docs/ANALYZER.md`.

---

## Architecture

- **Python 3.10+, FastAPI, uvicorn.** One process serves the API and the
  dashboard from the same origin, so there is no CORS setup.
- **SQLite in WAL mode**, one file per workspace. No server to run.
- **Plain HTML, CSS and JavaScript** for the dashboard (`static/dashboard.html`).
  No bundler and nothing to compile.
- **Background loops** inside the app handle the mailbox scan, autopilot,
  lead refill and the social publishing queue.
- **Single-file API** in `webapp.py` (about 14,000 lines) with focused
  modules around it:

| Module | Job |
|---|---|
| `workspaces.py` | Which business is this request for |
| `sequences.py`, `voice.py`, `slop.py`, `windows.py` | Email copy, the owner's voice, the bot-sounding test, sending windows |
| `inbox.py`, `sentcheck.py`, `mailer.py` | Read replies and classify them, verify sends, send mail |
| `research.py`, `inbound.py` | Read a prospect's site for selling points, sort inbound leads |
| `triggers.py`, `telnyx_calls.py`, `textback.py` | Event rules, click-to-call, missed-call text-back |
| `campaigns.py`, `captions.py`, `socialweek.py`, `social.py` | Briefs, post text, a finished week of posts, the publish queue |
| `facebook.py`, `instagram.py`, `metricool.py`, `media.py` | Publishing and scheduling, media hosting |
| `imagegen.py`, `library.py` | Bring-your-own image generation, picture library |
| `reputation.py`, `competitors.py`, `usage.py` | Google listing watch, competitor watch, in-app messages |
| `leavebehind.py`, `seo_synopsis.py`, `deliverability.py` | Door-knock sheet, plain-English SEO read, SPF/DKIM/DMARC check |
| `snapshots.py`, `pages.py`, `auth.py` | Config export and import, legal and help pages, sign in by email code |
| `grit_analyzer/` | The website analyzer package |

---

## Run it

```bash
git clone https://github.com/thewatsonfactor-dot/just-grit.git
cd just-grit/just_grit
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn webapp:app --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

Databases are created on first start in the folder above `just_grit/`
(override with `JUST_GRIT_DATA`). Everything else is set in the app under
**Setup**.

### Configuration

Secrets live in the database through the Setup screen. They are never read
from the repository and never returned by an API: the app reports only a
`has_*` flag for each key.

| Setting | Where |
|---|---|
| Google Maps / Places key | Setup, or `GOOGLE_MAPS_API_KEY` |
| Mailbox (IMAP) and sending (SMTP) | Setup, per workspace |
| Telnyx key and numbers (calls, texts, AI receptionist) | Setup |
| Facebook and Instagram, Metricool, image provider | Setup, Social |

Optional environment variables: `JUST_GRIT_DATA`, `GRIT_DB_PATH`,
`GRIT_RATE_PER_MINUTE`, `GRIT_BATCH_PER_HOUR`, `GRIT_CACHE_TTL_SECONDS`,
`GRIT_CORS_ORIGINS`, `GRIT_CHROME`, `JUST_GRIT_NO_LOOP`.

### Run it all day (macOS)

`just_grit/ops/` installs three launchd jobs: the app (restarts itself and
starts at login), a nightly backup kept 30 days, and a watchdog that texts the
owner if `/health` stops answering. A release script installs a bundle,
runs the tests, and rolls itself back if they fail. See `just_grit/ops/README.md`.

```bash
bash just_grit/ops/install.sh
launchctl kickstart -k "gui/$(id -u)/com.justgrit.app"   # restart by hand
```

### Putting it on the internet

The author runs it behind a Cloudflare tunnel with Cloudflare Access in front.
Only `/welcome/*` and `/webhooks/*` are meant to be reachable without signing
in (link previews, the public offer page, call webhooks). Sign-in is by a code
emailed to an allow-listed address.

---

## Tests

```bash
cd just_grit
./.venv/bin/pip install pytest
./.venv/bin/python -m pytest tests -q
```

72 test files cover the analyzer, autopilot pacing, reply classification and
stop handling, follow-up sequences, lead refill, calendar, workspace
isolation, social publishing and more.

---

## Repository layout

```
just_grit/
  webapp.py            the app: API, background loops, business rules
  api.py               stand-alone analyzer API
  grit_analyzer/       website analyzer (fetch, checks, scoring, report, PDF)
  static/              dashboard, login, landing and public assets
  tests/               pytest suite
  ops/                 launchd jobs, backup, watchdog, release script
  docs/playbook/       the working notes behind each feature
  CHANGELOG.md         what changed, newest first
advanced/              earlier command-line prospecting scripts
```

## What is deliberately not in this repository

Databases, API keys, mailbox credentials, the suppression list, prospect
exports, generated reports, backups and logs. All are covered by `.gitignore`.
Before the first public push, check `git status` for anything unexpected.

## License

All rights reserved by The Watson Factor until a license is added.
