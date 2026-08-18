# Just Grit Website Analyzer — v2

Scout's site-audit engine. First shipped module of Just Grit Marketing, and
the one that does double duty: product feature *and* lead magnet — run it on
a prospect and the report **is** the outreach.

## Run it

```bash
pip install -r requirements.txt
uvicorn api:app --port 8080
open http://localhost:8080/          # Scout cockpit — Prospects tab is live
```

One command serves the analyzer API and the dashboard from the same origin,
so the browser calls `/analyze` and `/batch` with no CORS configuration.

## CLI

```bash
python -m grit_analyzer.cli rhinoroofers.com --report out.html
python -m grit_analyzer.cli --batch prospects.txt        # ranked worst-first
python -m grit_analyzer.cli --compare a.com b.com c.com  # competitor mode
python -m grit_analyzer.cli --history rhinoroofers.com   # score over time
python -m grit_analyzer.cli --suppress somedomain.com    # never scan again
```

## What's new in v2 (vs. the v1 known-limits list)

| v1 limit | v2 |
|---|---|
| No rate limiting | Per-IP token bucket: 10 audits/min, 6 batches/hour (`GRIT_RATE_PER_MINUTE`, `GRIT_BATCH_PER_HOUR`) |
| No suppression list | `suppressed_domains.txt`, honored in API + CLI + batch; `--suppress` to add. HTTP 451 on suppressed domains |
| `allow_origins=["*"]` | CORS **off** by default (same-origin needs none). `GRIT_CORS_ORIGINS=https://yourapp.com` to open specific origins |
| In-process cache only | Cache is TTL'd (15 min) and bounded (500 entries); scans **persist to SQLite** (`grit_scans.db`) |
| No scan history | `/history?url=` — score over time + regression detection ("their site got worse — call now" / "our fix raised it 11 points") |
| Homepage only | Contact page is discovered and crawled; "no contact form" only fires when *neither* page has one, and softens to "form only on contact page" otherwise |
| No competitor mode | `/compare` + `--compare`: N sites ranked, per-competitor "sell against" line, market read |

## Endpoints

| Route | What |
|---|---|
| `GET /` | Scout dashboard |
| `GET /health` | `{ok, version, cached, db}` |
| `GET /analyze?url=&fresh=` | Full audit JSON (cached 15 min unless `fresh=1`) |
| `GET /report?url=` | Client-facing HTML report |
| `POST /batch` | `{urls: [...], fast: true}` → ranked worst-first rows |
| `POST /compare` | `{urls: [2–10]}` → competitor ranking + market read |
| `GET /history?url=` | Stored scans + regression delta |

## Scoring model (unchanged from v1 spec)

Speed 25 · Search visibility 25 · Turning visitors into calls 25 ·
Local presence 15 · Trust & basics 10. Each category starts at 100 and loses
points per finding; overall = weighted average.

Every finding answers three questions: **what we found** (a fact with a
number), **why it matters** (in customers or dollars, never jargon), and
**the fix** (an instruction you can hand a developer).

## Ground rules (non-negotiable)

Honors robots.txt. Identifies itself in the User-Agent. No auth, no form
submission, no vulnerability probing, no personal data. Honors the
suppression list the same way. Costs a little coverage; buys the whole
product's reputation.

False positives are the bug class that matters — a wrong finding in a cold
email costs credibility. When in doubt, the check stays quiet. Structured
data beats regex (JSON-LD address is authoritative over text parsing). Mixed
content counts only real subresource loads, never outbound links.

Everything renders through `esc()` — a hostile page with `<script>` /
`onerror=` payloads in its title and meta description is a regression test
(`tests/test_analyzer.py::test_hostile_page_renders_inert`).

## Tests

```bash
python tests/test_analyzer.py     # or: python -m pytest tests/ -q
```

Spins up a local fixture server (good site / bad site / hostile site /
contact-page site) — no external network needed.

## Postgres / Supabase migration

When the Supabase project is restored, the SQLite schema maps 1:1:

```sql
create table scans (
  id bigint generated always as identity primary key,
  host text not null,
  ts timestamptz not null default now(),
  ok boolean not null,
  overall int, grade text,
  scores_json jsonb, findings_json jsonb,
  stack_json jsonb, metrics_json jsonb,
  opening_line text
);
create index ix_scans_host_ts on scans (host, ts desc);
```

Point `db.py` at Postgres (swap sqlite3 for psycopg/supabase-py) and nothing
else changes.

## Still open (deliberately)

- Real Core Web Vitals (LCP/CLS/INP) need a headless browser at ~10× cost per
  scan — worth it for a paying customer's own site, not for scoring a
  prospect list. Playwright mode is the next step for paid accounts.
- Speed is measured server-side; real phones on cell service are slower. The
  report discloses this.
- Cache and rate-limit counters are in-process — fine for one instance;
  move to Redis when deploying more than one (e.g., behind Cloudflare).
