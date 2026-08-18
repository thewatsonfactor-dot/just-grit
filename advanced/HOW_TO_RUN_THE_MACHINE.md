# The machine — daily operating guide

Everything lives in `~/JustGrit`. Two double-clickable files, two scripts, one CSV
per market. That's the whole system.

---

## Every morning (5 minutes)

**Double-click `RUN_CALL_SHEET.command`.**
It starts the analyzer, asks which list and which vertical, scans every site, and
opens today's call sheet in your browser. Cmd-P prints it.

Then make the calls. Eight is the number.

---

## The one file that matters: your prospect CSV

Name it `prospects_<market>.csv` and it shows up in the menu automatically.

```csv
company,domain,phone,city,gap,say
Golden Wok,goldenwoksa.com,(210) 615-8282,San Antonio,"no owned ordering","you don't have any way for people to order directly from you — it all goes through Uber Eats"
```

| Column | Required | What it does |
|---|---|---|
| `domain` | **yes** | what gets scanned |
| `company` | no | display name |
| `phone` | no | printed on the sheet so you're not looking it up mid-dial |
| `city` | no | context |
| `gap` | no | your internal research note |
| `say` | no | **the sentence you say out loud** — overrides everything |
| `email` | no | needed only for the emailer |

**`say` is the most valuable column in the system.** When you've done real research
— "they're on DoorDash's storefront and paying a cut" — that beats anything the
scanner can infer from HTML. The scanner is the fallback, not the star.

---

## Building the list — Google Maps

**Double-click `FIND_PROSPECTS.command`.** Type what you're after ("barber shop",
"roofing contractor", "taqueria"), pick a city, and it pulls straight from Google
Places into a call-ready CSV — name, phone, website, rating, review count, address.

First run asks for a Google Maps API key. Two minutes:

1. `console.cloud.google.com` → create a project
2. APIs & Services → Library → **Places API (New)** → Enable
3. Credentials → Create credentials → API key
4. Click the key → **restrict it to Places API (New)** — an unrestricted key that
   leaks becomes somebody else's bill on your card

It saves the key to `.google_api_key`, readable only by you.

### What it flags automatically

The finder writes the `say` line for you when the data makes the gap obvious:

| What Google shows | What it means | Auto-written opener |
|---|---|---|
| No website at all | 🔥 hottest lead in the batch | "you don't have a website on your Google profile at all" |
| Site points at `order.online` | DoorDash's white-label — they don't own it | "the website on your Google profile is actually DoorDash's, not yours" |
| Site is a Facebook/Instagram/Linktree | no real web presence | same pitch, different host |
| Site is Booksy / StyleSeat / Vagaro | booking is rented, not owned | appointment-app pitch |
| Same name, 3+ locations | multi-location coordination pain | "you've got at least 4 locations and no single place to see them all" |
| 200+ reviews at 4.3★+ | strong reputation going unused | "all those reviews and almost none of it is on your own site" |

That third-party detection is the highest-value thing in the finder. One API call
tells you whether a business owns its own front door — and if it doesn't, you have
a real, specific, provable pitch before you dial.

### Cost control

The **field mask** in `findprospects.py` decides which billing tier you're charged
at, which is why it's deliberately minimal. One page = one request = up to 20
businesses, so a 3-page sweep costs 3 requests, not 60. Check current rates and
your free monthly allowance in the Google Cloud console before big runs.

**On Google's terms:** they restrict how long Places content may be cached (place
IDs are the documented exception). Treat these CSVs as a working call list you
refresh — not a permanent database you resell. Read the current Places API terms
before building anything durable on top of it.

### Still worth doing by hand

Job boards. Anyone hiring a dispatcher, scheduler, or office admin is publicly
announcing a process problem, and no API surfaces that. Strongest automation signal
there is.

## Email

```bash
./just_grit/.venv/bin/python emailer.py prospects_restaurants.csv --vertical restaurant
```

Writes one file per prospect into `emails/`. **It does not send.** Read each one,
send it from your own inbox, then log it:

```bash
./just_grit/.venv/bin/python emailer.py --log nichas.com
```

Logging matters — it's what stops you emailing the same shop twice.

**Before your first send, open `emailer.py` and set `ADDRESS`** to a real physical
postal address. CAN-SPAM requires it in commercial email. The script warns you until
you do.

Anyone who says stop goes into `suppressed_domains.txt`, one domain per line. The
emailer checks it before writing anything, every run.

### Send from your real inbox

For 8–20 emails a day to local businesses, send from your actual address. Better
deliverability than any cold-email tool, and it's a real person writing. You only
need a separate sending domain if you go past ~50/day — and you're nowhere near that.

**Never text a cold prospect.** TCPA damages run $500–1,500 *per message*.

---

## The verticals

Pick the one that matches the list. It changes the pivot question — the sentence
that turns a website chat into a software deal.

| Vertical | The question that finds the deal |
|---|---|
| `restaurant` | "How do people order from you right now — phone, your own site, or the apps?" |
| `construction` | "When a job comes in, how does it get from that call to somebody scheduled?" |
| `appointment` | "How do people book with you — call, DM, or an app?" |
| `multilocation` | "How do you see what's happening across all your locations?" |
| `generic` | "Walk me through what happens when a new customer contacts you." |

---

## What you're actually selling

Not a website fix. The call sheet opens with the website because it's concrete and
verifiable and it gets you talking. The **deal** is whatever they tell you about how
work gets from the phone call to the crew, the kitchen, or the chair.

Ask for a **$1,500 operations review**, never a build, on call one. Ninety minutes,
written findings, credited toward the build if they go ahead. It qualifies hard, it
pays for your time either way, and the deliverable becomes the scope — so you stop
writing specs for free.

---

## If something breaks

**"Site did not load" on everything** — the analyzer isn't running. `RUN_CALL_SHEET`
starts it, or double-click `START.command`.

**Terminal window closed** — that window *was* the server. Reopen it.

**A finding reads like jargon on the sheet** — add a `say` column for that row. Your
sentence always wins.
