"""
Just Grit — the whole thing, one app.

Find businesses → score their sites → get the sentence to say → call them →
log what happened. No scripts, no CSVs, no terminal.

    ./.venv/bin/uvicorn webapp:app --host 127.0.0.1 --port 8080

Auth is handled by Cloudflare Access at the edge (Google sign-in) before any
request reaches this machine. This app additionally verifies the header Access
injects, so the port can't be used directly by anything else on the network.

── AREA SWEEP (geographic slicing) ───────────────────────────────────────────
A single "roofing contractor in San Antonio" search maxes out at ~60 results.
The "Sweep this area" button runs that search once PER ZIP CODE across a whole
metro, so each ZIP contributes its own ~60. ZIP lists live in metros.json next
to this file — edit that to change coverage. A sweep runs in the background;
the browser polls it and the list fills in as it goes. If it dies partway,
running the same sweep again skips the ZIPs it already finished (resume).
"""
from __future__ import annotations

import csv, html, io, json, os, pathlib, re, sqlite3, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from contextlib import closing
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel, Field

from grit_analyzer.analyzer import analyze, analyze_batch, PLAIN_MAP, PLAIN_ORDER
from grit_analyzer.report import render_report
from grit_analyzer import guard
from grit_analyzer.fetch import fetch_page
import inbox
import snapshots as snap
import triggers as trig

import research as rsrch
import workspaces as ws
import campaigns as camp
import base64
import sequences as seq
import slop
import metricool as mc
import media as mstore
import facebook as fb
import instagram as insta
import social as soc          # plain import: uvicorn runs `webapp:app`
                                 # from inside just_grit/, which is not a package
import telnyx_calls as telnyx
import textback as tb
import sentcheck
import imagegen
import socialkit
import library
import mailer
import windows
import inbound
import competitors as compt
import captions
import socialweek
import leavebehind as lb
import auth
import pages
import reputation as rep
import voice
import zipfile

ROOT = pathlib.Path(__file__).parent
DATA = pathlib.Path(os.environ.get("JUST_GRIT_DATA", "").strip() or ROOT.parent)
                                        # where the .db files live (JUST_GRIT_DATA
                                        # overrides, same as workspaces.py); which file
                                        # is which business is workspaces.py's job
UI = ROOT / "static" / "app.html"           # the simple CRM (kept at /app)
DASH = ROOT / "static" / "dashboard.html"    # the Scout cockpit (served at /)
ASSETS = ROOT / "static" / "assets"          # theme images, served by /asset/<name>
PUBLIC = ROOT / "static" / "public"          # unauthenticated assets (og:image, etc.),
                                             # served by /public/<name> - no require_auth,
                                             # same reasoning as /welcome and /webhooks/*:
                                             # outside crawlers (iMessage, Slack, FB) can't
                                             # get through Cloudflare Access, so anything
                                             # they need to fetch has to live here.
METROS_FILE = ROOT / "metros.json"

app = FastAPI(title="Just Grit", docs_url=None, redoc_url=None)
POOL = ThreadPoolExecutor(max_workers=4)


# ─────────────────────────── storage ───────────────────────────

def db(workspace: Optional[str] = None):
    """Open the database for one business.

    Every query in this file goes through here, and none of them name a file.
    That is the whole isolation mechanism: a request working HomeRepair
    physically cannot see The Watson Factor's rows, because it never has that
    file open. See workspaces.py for why this is a file and not a column."""
    c = sqlite3.connect(DATA / ws.db_filename(workspace), timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db(workspace: Optional[str] = None):
    with closing(db(workspace)) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id TEXT UNIQUE, company TEXT, domain TEXT, phone TEXT,
            city TEXT, address TEXT, rating REAL, reviews INTEGER,
            vertical TEXT DEFAULT 'generic',
            gap TEXT DEFAULT '', say TEXT DEFAULT '', email TEXT DEFAULT '',
            score INTEGER, grade TEXT, opener TEXT DEFAULT '',
            findings TEXT DEFAULT '[]', scan_state TEXT DEFAULT 'pending',
            scan_error TEXT DEFAULT '',
            status TEXT DEFAULT 'new', note TEXT DEFAULT '',
            added_at TEXT, scanned_at TEXT, touched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_status ON prospects(status);
        CREATE INDEX IF NOT EXISTS ix_domain ON prospects(domain);

        -- one row per area-sweep job, so the browser can watch progress
        CREATE TABLE IF NOT EXISTS sweeps (
            id TEXT PRIMARY KEY, label TEXT, query TEXT, area TEXT,
            vertical TEXT DEFAULT 'generic',
            total INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
            found INTEGER DEFAULT 0, added INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
            state TEXT DEFAULT 'running', error TEXT DEFAULT '', current TEXT DEFAULT '',
            started_at TEXT, updated_at TEXT
        );
        -- which ZIPs a given sweep 'label' has already finished, so a re-run
        -- resumes instead of re-charging the API for work already done
        CREATE TABLE IF NOT EXISTS sweep_zips (
            label TEXT, zip TEXT, done_at TEXT, PRIMARY KEY (label, zip)
        );
        -- monthly counter of billable Google Places requests, so we can hold
        -- usage inside the free tier
        CREATE TABLE IF NOT EXISTS api_usage (
            month TEXT PRIMARY KEY, count INTEGER DEFAULT 0
        );

        -- every contact attempt, so the queue can show real history
        CREATE TABLE IF NOT EXISTS touches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER, kind TEXT, outcome TEXT, note TEXT, at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_touch_p ON touches(prospect_id);

        -- People. A business is not a contact; it has them.
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER, name TEXT, role TEXT DEFAULT '',
            email TEXT DEFAULT '', phone TEXT DEFAULT '',
            is_primary INTEGER DEFAULT 0, note TEXT DEFAULT '', added_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_contact_p ON contacts(prospect_id);

        -- Addresses the mail server told us are dead. A record rather than a
        -- destructive edit: blanking the contact's email would lose the only
        -- evidence of who we tried, and "we have no address for them" and
        -- "the address we had is dead" are different problems with different
        -- fixes. best_recipient reads this and moves to the next person.
        -- Inbound calls to this workspace's Telnyx number, one row per call
        -- session. The text-back trigger reads this and nothing else: a text
        -- can only ever be a reply to a row here that ended unanswered.
        CREATE TABLE IF NOT EXISTS inbound_calls (
            call_session_id TEXT PRIMARY KEY,
            call_control_id TEXT DEFAULT '',     -- the caller's own leg
            from_number TEXT DEFAULT '', to_number TEXT DEFAULT '',
            direction TEXT DEFAULT 'incoming',
            started_at TEXT, answered_at TEXT, hung_up_at TEXT,
            hangup_cause TEXT DEFAULT '', forwarded_to TEXT DEFAULT '',
            texted_at TEXT, text_status TEXT DEFAULT '', message_id TEXT DEFAULT '',
            text_body TEXT DEFAULT ''
        );
        -- Anyone who replied STOP. Checked before every text, forever.
        CREATE TABLE IF NOT EXISTS sms_optouts (
            number TEXT PRIMARY KEY, at TEXT, source TEXT DEFAULT ''
        );
        -- Every SMS in or out, so the monthly report and the Today panel
        -- have something to count and a reply is never lost.
        CREATE TABLE IF NOT EXISTS sms_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT, number TEXT, text TEXT DEFAULT '',
            call_session_id TEXT DEFAULT '', message_id TEXT DEFAULT '', at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_sms_num ON sms_log(number);

        -- Every generated picture, whether it worked or not. This is the
        -- cost ledger: at a cent a picture nobody will notice the bill, but
        -- a prompt that keeps failing or a runaway loop shows up here first.
        -- Posts waiting for their time. A row gets here by a person pressing
        -- Schedule, and that press is the approval: the loop below only ever
        -- publishes what a human already decided to publish, at the time they
        -- chose. One attempt per row - a failed post is shown, not retried
        -- into a duplicate.
        CREATE TABLE IF NOT EXISTS social_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            networks TEXT DEFAULT '[]', text TEXT DEFAULT '',
            image_url TEXT DEFAULT '', link TEXT DEFAULT '',
            first_comment TEXT DEFAULT '',
            when_at TEXT, state TEXT DEFAULT 'scheduled',
            result TEXT DEFAULT '', error TEXT DEFAULT '',
            created_at TEXT, published_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_sq_state ON social_queue(state, when_at);

        -- Reputation: the business's own Google listing over time, its
        -- reviews with a reply drafted, and the review requests sent.
        CREATE TABLE IF NOT EXISTS reputation_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, rating REAL, count INTEGER
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT UNIQUE, author TEXT, rating INTEGER,
            text TEXT DEFAULT '', at TEXT, when_text TEXT DEFAULT '', sentiment TEXT DEFAULT '',
            reply_draft TEXT DEFAULT '', replied_at TEXT DEFAULT '', seen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS review_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT, phone TEXT DEFAULT '', email TEXT DEFAULT '',
            channel TEXT, message TEXT, state TEXT DEFAULT 'sent', error TEXT DEFAULT '', at TEXT
        );

        CREATE TABLE IF NOT EXISTS imagegen_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT DEFAULT '', model TEXT DEFAULT '',
            prompt TEXT DEFAULT '', media_id TEXT DEFAULT '',
            response_id TEXT DEFAULT '', ok INTEGER DEFAULT 0,
            error TEXT DEFAULT '', ms INTEGER DEFAULT 0, at TEXT
        );

        -- Promos the business runs on its own social accounts. Pick one in
        -- the post box and the caption is written around it (captions.py).
        CREATE TABLE IF NOT EXISTS social_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, offer TEXT DEFAULT '', details TEXT DEFAULT '',
            audience TEXT DEFAULT '', cta TEXT DEFAULT '', link TEXT DEFAULT '',
            hashtags TEXT DEFAULT '', picture_idea TEXT DEFAULT '',
            starts TEXT DEFAULT '', ends TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS dead_addresses (
            email TEXT PRIMARY KEY, prospect_id INTEGER,
            reason TEXT DEFAULT '', at TEXT
        );

        CREATE TABLE IF NOT EXISTS competitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id TEXT DEFAULT '', name TEXT DEFAULT '', domain TEXT DEFAULT '',
            phone TEXT DEFAULT '', address TEXT DEFAULT '',
            rating REAL, reviews INTEGER DEFAULT 0,
            snapshot TEXT DEFAULT '', prev TEXT DEFAULT '',
            added_at TEXT, last_scan_at TEXT, removed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS media_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id TEXT NOT NULL, name TEXT DEFAULT '',
            source TEXT DEFAULT 'upload', source_ref TEXT DEFAULT '',
            width INTEGER DEFAULT 0, height INTEGER DEFAULT 0,
            bytes INTEGER DEFAULT 0, added_at TEXT, removed_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ml_media ON media_library(media_id);

        -- What you're selling them and for how much. One business can have
        -- several: the paid review, then the build, then the retainer.
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER,
            title TEXT,
            kind TEXT DEFAULT 'build',       -- review | build | retainer | other
            amount REAL DEFAULT 0,           -- one-time value
            mrr REAL DEFAULT 0,              -- monthly recurring, for retainers
            state TEXT DEFAULT 'open',       -- open | won | lost
            opened_at TEXT, closed_at TEXT, note TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_deal_p ON deals(prospect_id);

        -- Outbound email, held for a human. Nothing in this table sends
        -- itself; 'approved' means a person read it and pressed the button.
        CREATE TABLE IF NOT EXISTS outreach (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER,
            offer TEXT DEFAULT '',
            to_addr TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            body TEXT DEFAULT '',
            subject_original TEXT DEFAULT '',
            body_original TEXT DEFAULT '',
            state TEXT DEFAULT 'draft',    -- draft | approved | sent | skipped
            created_at TEXT, decided_at TEXT, sent_at TEXT,
            note TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_out_p ON outreach(prospect_id);
        CREATE INDEX IF NOT EXISTS ix_out_state ON outreach(state);

        -- What Daniel thinks. Two kinds land here:
        --   'note'  he typed something on a tab
        --   'edit'  the app noticed the gap between what it wrote and what
        --           he actually sent - the highest-signal feedback there is,
        --           because he produces it without being asked.
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT DEFAULT 'note',      -- note | edit
            tab TEXT DEFAULT '',
            rating TEXT DEFAULT '',        -- good | bad | idea
            subject_type TEXT DEFAULT '',  -- prospect | outreach | ''
            subject_id INTEGER,
            label TEXT DEFAULT '',         -- human name of what it is about
            body TEXT DEFAULT '',          -- what he wrote, or the diff summary
            before_text TEXT DEFAULT '',
            after_text TEXT DEFAULT '',
            state TEXT DEFAULT 'open',     -- open | applied | wontfix
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_fb_state ON feedback(state);
        CREATE INDEX IF NOT EXISTS ix_fb_tab ON feedback(tab);

        -- Proposals, bids and contracts attached to an account. The bytes live
        -- on disk under files/<workspace>/<prospect>/; this row is the index.
        -- Removing one only sets removed_at - the file stays on disk.
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER, deal_id INTEGER,
            kind TEXT DEFAULT 'other',
            name TEXT DEFAULT '', stored TEXT DEFAULT '',
            mime TEXT DEFAULT '', bytes INTEGER DEFAULT 0,
            note TEXT DEFAULT '', added_at TEXT, removed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_files_p ON files(prospect_id);
        """)
        # Additive migration. Safe on an existing database, safe to re-run.
        for col, decl in [("stage",       "TEXT DEFAULT 'lead'"),
                          ("stage_at",    "TEXT"),
                          ("next_action", "TEXT DEFAULT 'call'"),
                          ("next_due",    "TEXT"),
                          ("step",        "INTEGER DEFAULT 1"),
                          ("touch_count", "INTEGER DEFAULT 0"),
                          # Research carried in from a prospect workbook. The
                          # qualification that decides whether a lead is worth
                          # a Tuesday morning does not live in a site scan.
                          ("offers",       "TEXT DEFAULT ''"),
                          ("complaint",    "TEXT DEFAULT ''"),
                          ("signal",       "TEXT DEFAULT ''"),
                          ("lead_score",   "INTEGER DEFAULT 0"),
                          ("tier",         "INTEGER DEFAULT 1"),
                          ("lead_quality", "TEXT DEFAULT ''"),
                          ("gate_afford",  "TEXT DEFAULT ''"),
                          ("gate_need",    "TEXT DEFAULT ''"),
                          ("gate_open",    "TEXT DEFAULT ''"),
                          ("confirm",      "TEXT DEFAULT ''"),
                          ("route",        "TEXT DEFAULT ''"),
                          ("market",       "TEXT DEFAULT ''"),
                          ("category",     "TEXT DEFAULT ''"),
                          ("readiness",    "TEXT DEFAULT ''"),
                          ("stack",        "TEXT DEFAULT ''"),
                          ("territory",    "TEXT DEFAULT ''"),
                          ("source",       "TEXT DEFAULT ''"),
                          ("offer_evidence", "TEXT DEFAULT ''"),
                          ("qualified_at",  "TEXT"),
                          ("maps_url",      "TEXT DEFAULT ''"),
                          # Which sequence + angle a HomeRepair prospect is
                          # running, so the follow-up sweep knows which of
                          # sequences.py's steps to draft next without a
                          # second lookup back through the outreach table.
                          ("seq_variant",   "TEXT DEFAULT ''"),
                          # When this cold lead got its one second chance.
                          # Its presence is what stops the reawaken sweep
                          # becoming a carousel that re-approaches the same
                          # people every quarter forever.
                          ("reawakened_at", "TEXT DEFAULT ''"),
                          # When the app last went looking for an email on
                          # their site, so the refill never reads the same
                          # site twice in a week for nothing.
                          ("email_checked_at", "TEXT DEFAULT ''"),
                          # The inbound sort (inbound.py): which of the four
                          # buckets their last message landed in, why, what
                          # they wrote, and the reply drafted for Daniel.
                          ("inbound_bucket", "TEXT DEFAULT ''"),
                          ("inbound_why", "TEXT DEFAULT ''"),
                          ("inbound_text", "TEXT DEFAULT ''"),
                          ("inbound_reply", "TEXT DEFAULT ''"),
                          ("inbound_at", "TEXT DEFAULT ''"),
                          # Owner's own labels on a lead ("hot", "referral",
                          # "Nov callback") - comma-separated, for the Leads
                          # table's filter and the CSV round trip.
                          ("tags", "TEXT DEFAULT ''"),
                          # A follow-up the owner set by hand: a day, an
                          # optional time, and what it is for.
                          ("follow_time", "TEXT DEFAULT ''"),
                          ("follow_note", "TEXT DEFAULT ''")]:
            try:
                c.execute(f"ALTER TABLE prospects ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass          # column already present
        for col, decl in [("subject_original", "TEXT DEFAULT ''"),
                          ("body_original", "TEXT DEFAULT ''"),
                          ("variant", "TEXT DEFAULT ''"),
                          ("reply", "TEXT DEFAULT ''"),
                          ("replied_at", "TEXT DEFAULT ''"),
                          # Which of the sequence's steps this draft is, so a
                          # step-2 follow-up marked "sent" schedules step 3 -
                          # not step 2 again.
                          ("step", "INTEGER DEFAULT 1"),
                          # What the inbox scan found. `reply` stays empty for
                          # a human answer on purpose - the scan will not guess
                          # positive vs negative, so the text waits here for one
                          # tap instead of putting a guess on the scoreboard.
                          ("reply_text", "TEXT DEFAULT ''"),
                          ("reply_from", "TEXT DEFAULT ''"),
                          ("reply_detected_at", "TEXT DEFAULT ''"),
                          # Did the email that left match the draft? The
                          # Outbox hands off to mailto:, so the row only ever
                          # holds what was WRITTEN. '' = never checked;
                          # 'as_drafted' / 'edited' = verified against the
                          # Sent folder (or an Outbox edit before the send).
                          # The scoreboard files 'edited' sends under "yours",
                          # not under a variant whose copy never went out.
                          ("copy_check", "TEXT DEFAULT ''"),
                          ("sent_subject", "TEXT DEFAULT ''"),
                          ("sent_body", "TEXT DEFAULT ''"),
                          # How it left: '' / 'mail' for the mailto: handoff,
                          # 'autopilot' when the app sent it itself over SMTP.
                          # message_id is what threads the next follow-up
                          # under this one - only known for autopilot sends.
                          ("sent_via", "TEXT DEFAULT ''"),
                          ("message_id", "TEXT DEFAULT ''"),
                          ("send_error", "TEXT DEFAULT ''")]:
            try:
                c.execute("ALTER TABLE outreach ADD COLUMN %s %s" % (col, decl))
            except sqlite3.OperationalError:
                pass
        try:
            # which campaign a scheduled post belongs to, for the count on each card
            c.execute("ALTER TABLE social_queue ADD COLUMN campaign_id INTEGER")
        except sqlite3.OperationalError:
            pass
        for col in ("source", "plan_week", "slot", "kind", "stats", "stats_at"):
            try:
                # the week autopilot's rows: which week, which of the five slots
                c.execute("ALTER TABLE social_queue ADD COLUMN %s TEXT DEFAULT ''" % col)
            except sqlite3.OperationalError:
                pass
        # Every customer workspace is a row here instead of an edit to
        # workspaces.py. Only ever created in the PRIMARY database - it is an
        # account-level list, like allowed_emails, not per-customer data.
        if ws.current() == ws.PRIMARY:
            c.execute("""CREATE TABLE IF NOT EXISTS workspace_registry (
                slug TEXT PRIMARY KEY, name TEXT, short TEXT, product TEXT,
                db TEXT, hosts TEXT DEFAULT '[]', accent TEXT DEFAULT '#e8a33d',
                sells TEXT DEFAULT '', buyer TEXT DEFAULT '',
                active INTEGER DEFAULT 1, created_at TEXT)""")

        # Backfill: rows written before the diff existed have no baseline, so
        # treat what is there now as the baseline rather than inventing one.
        c.execute("UPDATE outreach SET subject_original=subject, body_original=body "
                  "WHERE COALESCE(body_original,'') = ''")
        # Backfill: sends the Outbox already caught being edited (there is an
        # 'edit' feedback row for them) were known edits before copy_check
        # existed. Everything else stays '' - unknown is unknown.
        c.execute("""UPDATE outreach SET copy_check='edited',
                        sent_subject=subject,
                        sent_body=COALESCE((SELECT after_text FROM feedback f
                                            WHERE f.kind='edit' AND f.subject_type='outreach'
                                              AND f.subject_id=outreach.id
                                            ORDER BY f.id DESC LIMIT 1), body)
                     WHERE state='sent' AND COALESCE(copy_check,'')=''
                       AND EXISTS (SELECT 1 FROM feedback f WHERE f.kind='edit'
                                   AND f.subject_type='outreach' AND f.subject_id=outreach.id)""")

        # Anything that predates the queue is due now.
        c.execute("UPDATE prospects SET next_due = date('now'), next_action='call', step=1 "
                  "WHERE next_due IS NULL AND status = 'new'")
        # status='research' is a deliberate parking spot: the lead is real but
        # has no one to call yet. The sweep above would put 130 of them into
        # "today", which is the same as having no list at all.
        # Map existing queue statuses onto the pipeline so nothing starts blank.
        c.execute("""UPDATE prospects SET stage = CASE
                        WHEN status = 'booked'                 THEN 'review'
                        WHEN status IN ('dead','cold')         THEN 'lost'
                        WHEN status IN ('called','emailed','working') THEN 'working'
                        ELSE 'lead' END
                     WHERE stage IS NULL OR stage = ''""")
        c.commit()


def suppression_paths():
    """Which do-not-contact list this workspace writes, and which it reads.

    A "dead" or "corporate" mark used to append to one process-wide file that
    gated scanning, importing and drafting in EVERY workspace. Acceptable
    while both workspaces were the owner's; not once a customer's guest can
    press the button - one tap in their workspace silently blocked that
    domain for everyone, including the owner (QT-3).

    Mirrors _settings_ws: the account list belongs to the primary workspace.
    The primary writes it; every other workspace writes its own file and
    reads both, so the owner's global do-not-scan decisions still hold
    everywhere while a customer's decisions stay theirs.

    Returns (write_path, [read_paths]).
    """
    slug = ws.current()
    account = guard.SUPPRESSION_FILE
    if slug == ws.PRIMARY:
        return account, [account]
    own = str(DATA / ("suppressed_domains-%s.txt" % slug))
    return own, [own, account]


def is_suppressed(url_or_domain: str) -> bool:
    return guard.is_suppressed(url_or_domain, suppression_paths()[1])


def suppress(domain: str) -> None:
    guard.suppress(domain, suppression_paths()[0])


def _settings_ws(k):
    """Who owns this setting. Sign-in list, Google key and the request cap are
    account-level — one Google project, one bill, one cap — so they always read
    and write the primary file no matter which business is on screen. Sender
    identity, mailing address and offers belong to the business."""
    return ws.PRIMARY if k in ws.GLOBAL_SETTINGS else None


def setting(k, default=""):
    with closing(db(_settings_ws(k))) as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(k, v):
    with closing(db(_settings_ws(k))) as c:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        c.commit()
    _CADENCE_CACHE.clear()


# Call windows and follow-up gaps are read once per card render, which is a
# database round trip per prospect on a list of 275. Cache them per workspace
# and drop the whole cache on any settings write - a cadence that is one
# request stale is a bug nobody would ever find.
_CADENCE_CACHE: dict = {}


def _cadence(key, default):
    slug = ws.current()
    hit = _CADENCE_CACHE.get((slug, key))
    if hit is not None:
        return hit
    raw = ""
    try:
        raw = setting(key, "")
    except Exception:
        pass
    val = default
    if raw:
        if key == snap.SETTING_WINDOWS:
            val = snap.clean_windows(raw) or default
        elif key == snap.SETTING_GAPS:
            val = snap.clean_gaps(raw) or default
        elif key == snap.SETTING_MAX_STEP:
            val = snap.clean_max_step(raw, default)
        elif key == snap.SETTING_REAWAKEN:
            val = snap.clean_reawaken(raw, default)
    _CADENCE_CACHE[(slug, key)] = val
    return val


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_files = [w["db"] for w in ws.WORKSPACES.values()]
if len(set(_files)) != len(_files):
    raise RuntimeError(
        "Two workspaces are configured with the same database file: %s. "
        "That would silently merge two businesses' leads into one queue." % _files)

for _slug in ws.WORKSPACES:
    init_db(_slug)      # every workspace gets its schema at startup, so opening
                        # a new one is never a half-built database


# ─────────────────────── which business is this ───────────────────────

@app.middleware("http")
async def pick_workspace(request: Request, call_next):
    """Decide which business this request is working, before it touches the DB.

    The hostname wins when it is one we know, and it is not overridable: land
    on justgrit.homerepair.tech and you get HomeRepair, whatever a stale cookie
    from yesterday says. Off a known host — on this Mac at 127.0.0.1 — an
    explicit ?ws= switches and is remembered in a cookie.
    """
    slug = ws.from_host(request.headers.get("host", ""))
    sticky = None
    if not slug:
        asked = (request.query_params.get("ws") or "").strip().lower()
        if asked and ws.exists(asked):
            slug = sticky = asked
        else:
            cookie = (request.cookies.get("jg_ws") or "").strip().lower()
            slug = cookie if ws.exists(cookie) else ws.PRIMARY
    ws.use(slug)
    response = await call_next(request)
    response.headers["x-workspace"] = slug
    if sticky:
        response.set_cookie("jg_ws", sticky, max_age=60 * 60 * 24 * 365,
                            samesite="lax", httponly=False)
    return response


# ─────────────────── free-tier request budget ───────────────────
# Google gives 1,000 free Places Text Search (Enterprise) requests per month.
# We count every billable request and refuse to go past the cap, so the app
# can't spend money unless you deliberately raise the cap. Set the cap to 0 to
# turn the safety off (unlimited / pay past the free tier).

FREE_REQUEST_CAP_DEFAULT = 1000


def month_key():
    return now()[:7]   # "YYYY-MM"


def request_cap():
    try:
        return int(setting("free_request_cap", str(FREE_REQUEST_CAP_DEFAULT)))
    except (TypeError, ValueError):
        return FREE_REQUEST_CAP_DEFAULT


def usage_count():
    with closing(db(ws.PRIMARY)) as c:
        r = c.execute("SELECT count FROM api_usage WHERE month=?", (month_key(),)).fetchone()
    return r["count"] if r else 0


def remaining_requests():
    cap = request_cap()
    if cap <= 0:
        return 10 ** 9   # safety off
    return max(0, cap - usage_count())


def reserve_request():
    """Atomically claim one Places request against the monthly free cap.
    Returns True if allowed (and counted), False if the cap is reached.
    The conditional UPDATE makes this safe even with a sweep and a manual
    Find running at the same time."""
    cap = request_cap()
    with closing(db(ws.PRIMARY)) as c:
        c.execute("INSERT OR IGNORE INTO api_usage(month, count) VALUES(?, 0)", (month_key(),))
        cur = c.execute(
            "UPDATE api_usage SET count = count + 1 "
            "WHERE month = ? AND (? <= 0 OR count < ?)",
            (month_key(), cap, cap))
        c.commit()
        return cur.rowcount > 0


def cap_message():
    return (f"You've used all {request_cap()} of this month's free Google requests. "
            f"They reset on the 1st. To keep going now, raise the cap under the "
            f"budget note (you'd be billed about 3.5¢ per request past the free tier).")


# ─────────────────────── API key resolution ───────────────────────
# The key can come from (in order): the Settings screen (stored in the DB),
# the GOOGLE_MAPS_API_KEY environment variable, or a .google_api_key file on
# disk. The file lets the same key serve both this app and the command-line
# scraper without pasting it twice. All of these are git-ignored.

KEY_FILES = [
    ROOT / ".google_api_key",                    # just_grit/.google_api_key
    ROOT.parent / ".google_api_key",             # project root
    ROOT.parent / "advanced" / ".google_api_key",  # the CLI's location
]


def looks_like_google_key(v: str) -> bool:
    v = (v or "").strip()
    return len(v) == 39 and v.startswith("AIza")


def google_key_source() -> tuple:
    """Return (key, where_it_came_from). Knowing the source is the difference
    between 'replace your key' and 'you already have a good one, Settings is
    covering it up' — which are opposite instructions."""
    # A customer workspace may carry its own key (Phase 3, 2026-09-26): its
    # lead finding then runs on its Google bill, not the owner's.
    own = (setting("google_key_own") or "").strip() if ws.current() != ws.PRIMARY else ""
    if own:
        return own, "this workspace's own key"
    v = (setting("google_key") or "").strip()
    if v:
        return v, "Settings"
    env = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if env:
        return env, "the GOOGLE_MAPS_API_KEY environment variable"
    for f in KEY_FILES:
        try:
            if f.exists():
                k = f.read_text().strip()
                if k:
                    return k, str(f)
        except Exception:
            pass
    return "", ""


def key_on_file() -> str:
    """A well-formed key sitting somewhere Settings is currently outranking."""
    env = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if looks_like_google_key(env):
        return "the GOOGLE_MAPS_API_KEY environment variable"
    for f in KEY_FILES:
        try:
            if f.exists() and looks_like_google_key(f.read_text()):
                return str(f)
        except Exception:
            pass
    return ""


def google_key() -> str:
    return google_key_source()[0]


# ─────────────────────────── auth ───────────────────────────
# Cloudflare Access authenticates with Google at the edge and injects the
# verified email as a header. We trust that header ONLY because nothing but
# the tunnel can reach this process — the app binds to 127.0.0.1 and the
# tunnel is the single ingress. Requests arriving without the header are
# allowed only from the loopback interface, which is how local use works.

ACCESS_HEADER = "cf-access-authenticated-user-email"


_AUTH_READY = set()


def auth_db():
    """The account-level database (PRIMARY) with the sign-in tables present."""
    c = db(ws.PRIMARY)
    key = str(DATA / ws.db_filename(ws.PRIMARY))
    if key not in _AUTH_READY:
        auth.init(c)
        _AUTH_READY.add(key)
    return c


def caller_email(request: Request) -> Optional[str]:
    """Who is this? Cloudflare Access first (it verified them), then our own
    session cookie from /login. Neither = nobody (the loopback rule in
    require_auth decides whether nobody may still get in)."""
    hdr = (request.headers.get(ACCESS_HEADER) or "").strip().lower()
    if hdr:
        return hdr
    tok = (request.cookies.get(auth.COOKIE) or "").strip()
    if tok:
        try:
            with closing(auth_db()) as c:
                return auth.session_email(c, tok)
        except sqlite3.Error:
            return None
    return None


def allowed_emails() -> set[str]:
    raw = setting("allowed_emails", "")
    return {e.strip().lower() for e in re.split(r"[,\s]+", raw) if e.strip()}


def via_cloudflare(request: Request) -> bool:
    """Did this request arrive through the tunnel rather than off this Mac?

    This matters more than it looks. cloudflared connects to the app FROM
    127.0.0.1, so tunneled traffic is indistinguishable from local traffic by
    IP alone — a loopback check would wave the whole internet through. Cloudflare
    stamps every proxied request with cf-ray, so its presence means "came from
    outside" and the Access identity becomes mandatory.
    """
    return bool(request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"))


# ── who may open WHICH workspace ─────────────────────────────────────────
# `allowed_emails` is account-level by design: it is the list of people who
# run this thing. That was fine while the only two workspaces were both
# Daniel's. The moment a customer gets a login it stops being fine - one
# global list means the address added so a client can use their own workspace
# is equally valid on every other one, and the only thing standing between a
# customer and somebody else's leads is a Cloudflare Access policy configured
# by hand, per hostname, correctly, every time.
#
# That is an external config being load-bearing for tenant isolation, which is
# the same class of mistake as a forgotten WHERE clause. So each workspace may
# carry its own `workspace_emails`. When it is set, only those people (plus
# the account owners) get in. When it is empty, behaviour is exactly what it
# was - which is what keeps Watson and HomeRepair working untouched.
def workspace_emails() -> set:
    raw = setting("workspace_emails", "")
    return {e.strip().lower() for e in raw.replace("\n", ",").split(",") if e.strip()}


def require_auth(request: Request) -> str:
    email = caller_email(request)
    if email:
        allow = allowed_emails()
        owner = (not allow) or (email in allow)
        guests = workspace_emails()
        if guests:
            # A tenant-scoped workspace: owners always, guests only here.
            if not (owner or email in guests):
                raise HTTPException(403, f"{email} is not on the allowed list.")
            return email
        if allow and email not in allow:
            raise HTTPException(403, f"{email} is not on the allowed list.")
        if not owner:
            raise HTTPException(403, f"{email} is not on the allowed list.")
        return email

    if via_cloudflare(request):
        # Reached us through the public hostname with no verified identity:
        # no Access header and no session cookie. Send them to /login.
        raise HTTPException(401, "Sign in required.")

    host = (request.client.host if request.client else "") or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return "local"
    raise HTTPException(401, "Sign in required.")


# Settings only an account owner may change. ws.GLOBAL_SETTINGS write the
# PRIMARY file (_settings_ws) whichever business is on screen - so a guest who
# could set `allowed_emails` from their own workspace would be an owner of
# Watson, HomeRepair and every other customer a moment later, and
# `free_request_cap` is the owner's Google bill. `workspace_emails` is who
# gets into a workspace at all, which is the owner's call, not the guests'.
OWNER_ONLY_SETTINGS = ws.GLOBAL_SETTINGS | {"workspace_emails"}


def is_owner(request: Request) -> bool:
    """Account owner (on `allowed_emails`) or a customer's guest (on this
    workspace's `workspace_emails`)? require_auth admits both and, on its
    own, tells nobody which - so anything that reaches past the workspace on
    screen (the owner list, the Google cap, creating workspaces) asks here.
    A caller with no identity has already passed require_auth's loopback
    check, which is the owner at his own Mac."""
    email = caller_email(request)
    if not email:
        return True
    allow = allowed_emails()
    return (not allow) or (email in allow)


def require_owner(request: Request) -> str:
    email = require_auth(request)
    if not is_owner(request):
        raise HTTPException(403, f"{email} is a guest here; only an account "
                                 "owner can do that.")
    return email


# ─────────────────────── Google Places ───────────────────────

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.websiteUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "nextPageToken",
])

THIRD_PARTY = {
    "order.online": "DoorDash's white-label ordering page",
    "doordash.com": "DoorDash", "ubereats.com": "Uber Eats",
    "grubhub.com": "Grubhub", "toasttab.com": "Toast",
    "clover.com": "Clover", "square.site": "a Square hosted page",
    "squareup.com": "Square", "facebook.com": "a Facebook page",
    "instagram.com": "an Instagram profile", "linktr.ee": "a Linktree",
    "booksy.com": "Booksy", "styleseat.com": "StyleSeat",
    "vagaro.com": "Vagaro", "wixsite.com": "a free Wix subdomain",
    "business.site": "a Google auto-generated page",
}


def domain_of(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"^https?://", "", url.strip()).split("/")[0].lower().replace("www.", "")


def places_search(query: str, key: str, pages: int = 2, region: str = "us"):
    out, token = [], None
    for i in range(max(1, min(pages, 3))):
        if not reserve_request():
            break   # monthly free-request cap reached — stop before spending
        body = {"textQuery": query, "regionCode": region, "languageCode": "en"}
        if token:
            body["pageToken"] = token
        req = urllib.request.Request(
            PLACES_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": FIELD_MASK}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code == 403:
                raise HTTPException(400, "Google rejected the key. Check that "
                                         "'Places API (New)' is enabled and the key "
                                         "isn't restricted to a different API.")
            raise HTTPException(400, f"Google returned {e.code}: {msg}")
        except Exception as e:
            raise HTTPException(502, f"Could not reach Google: {type(e).__name__}")
        out.extend(data.get("places") or [])
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(2)
    return out


def auto_angle(p: dict, name_counts: dict):
    site = p.get("websiteUri") or ""
    dom = domain_of(site)
    name = (p.get("displayName") or {}).get("text", "")
    reviews = p.get("userRatingCount") or 0
    rating = p.get("rating")

    # Every line below argues for a website. That is The Watson Factor's
    # product line, and in a workspace that sells something else it is worse
    # than silence: "your Google listing has no website" is true, irrelevant to
    # a property manager, and spends the opening sentence of the call. A
    # workspace with no catalog gets no opener until its own research rules
    # exist — the card still carries the name, phone, rating and review count,
    # which is everything you need to dial.
    if ws.current() not in CATALOG_WORKSPACES:
        return ("", "")

    for host, label in THIRD_PARTY.items():
        if host in dom:
            return (f"Doesn't own their site — points at {label}",
                    f"the website on your Google listing is actually {label}, not "
                    f"something you own")
    if not site:
        # Google not listing a website is a real, checkable fact about their
        # listing. Whether they own a site somewhere else, we don't know — so
        # say the part we can actually stand behind.
        return ("No website on their Google listing",
                "your Google listing doesn't link to a website — anyone who finds "
                "you on Maps gets a phone number and nothing else")
    n = name_counts.get(name, 1)
    if n >= 3:
        return (f"{n} locations in this search",
                f"you've got at least {n} locations, and I couldn't find one place "
                f"where you can see all of them at once")
    if reviews >= 200 and rating and rating >= 4.3:
        return (f"{reviews} reviews at {rating}★",
                f"you've got {reviews} reviews at {rating} stars and almost none of "
                f"that shows up on your own website")
    return ("", "")


# HomeRepair's buyers are not "a kind of business" the way a roofer is - they
# are who to email: a board, an HOA management company, a commercial
# building's owner or manager. Each preset is the Google search that finds
# them plus the category that picks their email sequence (see
# homerepair_segment). Daniel, 2026-09-25: "make sure we start offering
# services to HOAs and commercial buildings for maintenance."
BUYER_PRESETS = {
    "homerepair": [
        {"key": "hoa_board", "label": "Homeowners associations (the board)",
         "query": "homeowners association", "category": "hoa_board",
         "why": "Boards set next year's budget Oct-Dec - the budget-season email."},
        {"key": "hoa_manager", "label": "HOA management companies",
         "query": "HOA management company", "category": "hoa_manager",
         "why": "They run the common areas for dozens of boards."},
        {"key": "property_manager", "label": "Residential property managers",
         "query": "property management company", "category": "property_manager",
         "why": "The maintenance-queue email."},
        {"key": "commercial_manager", "label": "Commercial property managers",
         "query": "commercial property management", "category": "commercial_manager",
         "why": "Office, retail and medical buildings - the tenant-calls email."},
        {"key": "office", "label": "Office buildings", "query": "office building",
         "category": "commercial", "why": "Owner-managed buildings with tenants."},
        {"key": "retail", "label": "Shopping centers & retail plazas", "query": "shopping center",
         "category": "commercial", "why": "Strip centers with a handful of tenants each."},
        {"key": "medical", "label": "Medical office buildings", "query": "medical office building",
         "category": "commercial", "why": "Practices that lease and call the landlord when the AC quits."},
        {"key": "vacation", "label": "Vacation rental managers", "query": "vacation rental management",
         "category": "property_manager", "why": "Turnovers every few days - condition records matter."},
        {"key": "brokerage", "label": "Real estate brokerages", "query": "real estate brokerage",
         "category": "realtor", "why": "The partner toolkit: repair estimates inside the option period, $10/mo."},
        {"key": "agent", "label": "Real estate agents", "query": "real estate agent",
         "category": "realtor", "why": "Same pitch, one agent at a time - a broker's office reaches more."},
    ],
}


def buyer_preset(key: str) -> dict | None:
    for p in BUYER_PRESETS.get(ws.current(), []):
        if p["key"] == (key or ""):
            return p
    return None


def insert_places(places, city, vertical, category: str = ""):
    """Filter + insert a batch of places. Dedupe is automatic: place_id is
    UNIQUE, so a business already in the table just raises IntegrityError and
    is counted as skipped. Returns (added, skipped, new_ids)."""
    counts = {}
    for p in places:
        n = (p.get("displayName") or {}).get("text", "")
        counts[n] = counts.get(n, 0) + 1

    added, skipped, new_ids = 0, 0, []
    with closing(db()) as c:
        for p in places:
            if p.get("businessStatus") not in (None, "OPERATIONAL"):
                skipped += 1
                continue
            pid = p.get("id") or ""
            name = (p.get("displayName") or {}).get("text", "").strip()
            dom = domain_of(p.get("websiteUri"))
            if dom and is_suppressed(dom):
                skipped += 1
                continue
            gap, say = auto_angle(p, counts)
            phone = p.get("nationalPhoneNumber") or ""
            if (dom and c.execute("SELECT 1 FROM prospects WHERE domain=?", (dom,)).fetchone()) or \
               (phone and find_prospect_by_phone(c, telnyx.to_e164(phone) or phone)):
                skipped += 1              # same business, found again under another id
                continue
            try:
                cur = c.execute("""INSERT INTO prospects
                    (place_id, company, domain, phone, city, address, rating, reviews,
                     vertical, gap, say, added_at, next_due, next_action, step, category)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?, date('now'), 'call', 1, ?)""",
                    (pid, name, dom, phone, city,
                     p.get("formattedAddress") or "", p.get("rating"),
                     p.get("userRatingCount") or 0, vertical, gap, say, now(), category or ""))
                added += 1
                new_ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                skipped += 1
        c.commit()
    queue_scans(new_ids)
    return added, skipped, new_ids


# ─────────────────────── metros ───────────────────────

def load_metros():
    try:
        data = json.loads(METROS_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return {}


# ─────────────────────── scanning ───────────────────────


# The analyzer's plain-spoken lines were written for contractors. A few of them
# land wrong on a restaurant or a barbershop, so re-word those per vertical.
# Anything not listed here falls through to the analyzer's own wording.
VERTICAL_PHRASING = {
    "restaurant": {
        "no_form": "there's no way to ask about catering or a big party without "
                   "calling — those are the orders worth the most to you",
        "form_only_contact": "someone wanting to book a party has to hunt for how "
                             "to reach you",
        "no_reviews_shown": "your Google reviews don't show anywhere on your own site — "
                            "you've earned stars nobody sees",
        "no_trust": "nothing on the page says how long you've been around or what "
                    "you're known for",
    },
    "appointment": {
        "no_form": "there's no way to book online — if they can't call right then, "
                   "they book with whoever they can",
        "form_only_contact": "booking takes an extra click that most people never make",
        "no_trust": "nothing on the page says who your people are or how long "
                    "you've been cutting",
    },
    "generic": {
        "no_form": "the only way to reach you from the site is to pick up the phone",
        "form_only_contact": "getting in touch takes an extra click most people skip",
        "no_trust": "nothing on the page tells a first-time visitor why to trust you",
    },
    "multilocation": {
        "no_form": "there's no way for a customer to reach one specific location — "
                   "everything funnels to whoever picks up",
    },
}


def best_opener(d: dict, vertical: str = "generic") -> str:
    """Shortest human sentence for the phone. Reuses the analyzer's own map."""
    findings = [f for f in (d.get("findings") or []) if f.get("severity") != "good"]
    ranked = sorted(
        (f for f in findings if f.get("key") in PLAIN_ORDER),
        key=lambda f: PLAIN_ORDER[f["key"]])
    if ranked:
        key = ranked[0]["key"]
        phrase = VERTICAL_PHRASING.get(vertical, {}).get(key) or PLAIN_MAP[key]
        try:
            return phrase.format(load=(d.get("metrics") or {}).get("load_seconds", 0) or 0)
        except Exception:
            return phrase
    if findings:
        t = findings[0].get("title", "")
        return (t[0].lower() + t[1:]) if t else ""
    return ""


# ─────────────────── figuring out the real business name ───────────────────
# A lead called "goldenwoksa.com" is useless on a phone call. Google Places
# gives us a proper name; a pasted-in domain doesn't. So when a scan runs and
# all we have is a domain, go read the name off the site itself.
#
# Priority: structured data (the business states its own name) → Open Graph
# site name → the <title>, with taglines stripped.

_GENERIC_TITLE_PART = re.compile(
    r"^(home|welcome|index|menu|menus|order online|order|official site|"
    r"official website|home ?page|site|welcome to|contact|contact us|about|"
    r"about us|restaurant|our menu)$", re.I)

_TITLE_SPLIT = re.compile(r"\s+[|–—·•]\s+|\s+-\s+")


def _clean_title(title: str) -> str:
    """'Home | Golden Wok - Best Chinese in SA' -> 'Golden Wok'."""
    parts = [x.strip() for x in _TITLE_SPLIT.split(title or "") if x.strip()]
    parts = [x for x in parts if not _GENERIC_TITLE_PART.match(x)]
    if not parts:
        return ""
    # Taglines are long; the name is usually the shortest surviving piece.
    parts.sort(key=len)
    name = parts[0]
    name = re.sub(r"^(welcome to|the official site of)\s+", "", name, flags=re.I)
    return name.strip(" -–—|·,")


_NOT_A_NAME = re.compile(
    r"^(just a moment|please wait|checking your browser|attention required|"
    r"access denied|forbidden|unauthorized|error|not found|404|403|500|"
    r"coming soon|under construction|domain (is )?for sale|parked|"
    r"this site can.t be reached|default web site page|apache|nginx|"
    r"index of|untitled|new page|site unavailable|maintenance)",
    re.I)


def _plausible_name(name: str) -> bool:
    n = (name or "").strip()
    if not (1 < len(n) < 80):
        return False
    if _NOT_A_NAME.match(n):
        return False
    if not re.search(r"[A-Za-z]", n):
        return False
    return True


def discover_name(domain: str) -> str:
    if not domain:
        return ""
    try:
        import httpx
        from grit_analyzer import USER_AGENT
        with httpx.Client(follow_redirects=True, timeout=12,
                          headers={"User-Agent": USER_AGENT}) as cl:
            r = cl.get(f"https://{domain}")
            html = r.text[:400_000]
    except Exception:
        return ""

    # 1. structured data — the business naming itself
    for block in re.findall(
            r'<script[^>]+ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(isinstance(x, str) and x not in ("WebSite", "WebPage",
                                                        "BreadcrumbList", "SearchAction")
                       for x in types):
                    nm = node.get("name")
                    if isinstance(nm, str) and _plausible_name(nm):
                        return nm.strip()
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    # 2. og:site_name
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
                  html, re.I)
    if m and _plausible_name(m.group(1)):
        return m.group(1).strip()

    # 3. the title, de-tagline'd
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        name = _clean_title(re.sub(r"\s+", " ", m.group(1)))
        if _plausible_name(name):
            return name
    return ""


def needs_name(company: str, domain: str) -> bool:
    """True when 'company' is really just the domain wearing a hat."""
    c = (company or "").strip().lower()
    d = (domain or "").strip().lower()
    if not c:
        return True
    stem = d.split(".")[0]
    return c in (d, stem, "www." + d) or c.replace(" ", "") == stem


def scan_one(pid: int):
    with closing(db()) as c:
        row = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not row:
            return
        c.execute("UPDATE prospects SET scan_state='scanning' WHERE id=?", (pid,))
        c.commit()

    dom = row["domain"]
    if not dom:
        with closing(db()) as c:
            c.execute("UPDATE prospects SET scan_state='no_site', scanned_at=? WHERE id=?",
                      (now(), pid))
            c.commit()
        return

    try:
        d = analyze(dom, deep=False)
        ok = bool(d.get("ok"))

        # If this row is named after its domain, go learn the real name.
        if ok and needs_name(row["company"], dom):
            real = discover_name(dom)
            if real:
                with closing(db()) as c2:
                    c2.execute("UPDATE prospects SET company=? WHERE id=?", (real, pid))
                    c2.commit()
        opener = best_opener(d, row["vertical"] or "generic") if ok else ""
        keep = [{"key": f.get("key"), "title": f.get("title"),
                 "severity": f.get("severity"),
                 "evidence": f.get("evidence"), "fix": f.get("fix")}
                for f in (d.get("findings") or []) if f.get("severity") != "good"][:6]
        # Every finding key, not just the six we show. The campaign readiness
        # gate asks questions the report never displays ("is there a pixel?"),
        # and truncating to the six worst would answer them wrong.
        keys = sorted({f.get("key") for f in (d.get("findings") or []) if f.get("key")})
        stack = d.get("stack") or {}
        gate = json.dumps({"keys": keys,
                           "cms": stack.get("cms"),
                           "analytics": stack.get("has_analytics"),
                           "retargeting": stack.get("has_retargeting"),
                           "detected": stack.get("detected") or {},
                           "scanned_at": now()}) if ok else ""
        with closing(db()) as c:
            c.execute("""UPDATE prospects SET score=?, grade=?, opener=?, findings=?,
                         stack=?, scan_state=?, scan_error=?, scanned_at=? WHERE id=?""",
                      (d.get("overall") if ok else None, d.get("grade") if ok else None,
                       opener, json.dumps(keep), gate,
                       "done" if ok else "failed",
                       "" if ok else str(d.get("error") or "site did not load")[:200],
                       now(), pid))
            c.commit()
    except Exception as e:
        with closing(db()) as c:
            c.execute("UPDATE prospects SET scan_state='failed', scan_error=?, scanned_at=? "
                      "WHERE id=?", (f"{type(e).__name__}: {e}"[:200], now(), pid))
            c.commit()


def queue_scans(ids):
    for i in ids:
        POOL.submit(ws.carry(scan_one, i))


# ─────────────────────── area sweep (background) ───────────────────────

def run_sweep(sweep_id, label, query, state_code, todo, city_label, vertical, pages, key, category=""):
    """Drives one area sweep to completion on a background thread. Writes to
    the DB after every ZIP so the browser sees progress and a crash can resume."""
    def upd(**cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        with closing(db()) as c:
            c.execute(f"UPDATE sweeps SET {sets}, updated_at=? WHERE id=?",
                      (*cols.values(), now(), sweep_id))
            c.commit()
    try:
        for z in todo:
            if remaining_requests() <= 0:
                upd(state="error", current="",
                    error=f"Reached the monthly free Google limit ({request_cap()} requests). "
                          f"Swept up to here — the finished ZIPs are saved, so resume next "
                          f"month, or raise the cap to continue now.")
                return
            upd(current=z)
            try:
                places = places_search(f"{query} in {z}, {state_code}", key, pages)
            except HTTPException as he:
                # 400/403 = bad key or config → every ZIP would fail, so stop.
                if he.status_code in (400, 403):
                    upd(state="error", error=str(he.detail)[:300], current="")
                    return
                places = []   # transient (e.g. 502) → skip this ZIP, keep going
            except Exception:
                places = []

            found = len(places)
            added, skipped, _ = insert_places(places, city_label, vertical, category)
            with closing(db()) as c:
                c.execute("INSERT OR IGNORE INTO sweep_zips(label, zip, done_at) "
                          "VALUES(?,?,?)", (label, z, now()))
                c.execute("""UPDATE sweeps SET done=done+1, found=found+?, added=added+?,
                             skipped=skipped+?, updated_at=? WHERE id=?""",
                          (found, added, skipped, now(), sweep_id))
                c.commit()
            time.sleep(1.0)   # rate limit — be kind to the API between ZIPs

        upd(state="done", current="")
    except Exception as e:
        upd(state="error", error=f"{type(e).__name__}: {e}"[:300], current="")




# ─────────────────────── outreach sequence ───────────────────────
# Most of these deals land on touch three through six. The single biggest
# reason a one-person shop loses one is not rejection — it's losing track.
# Days are counted from the touch before, not from day zero.
SEQUENCE = [
    (1, "call",  0,  "First call — you have their site findings in hand"),
    (2, "email", 2,  "Send the report you said you'd send"),
    (3, "call",  4,  "Second call — reference the email"),
    (4, "email", 7,  "Short check-in, no pitch"),
    (5, "call",  14, "Last call before you let this one go"),
]
STEP_BY_N = {n: (kind, days, why) for n, kind, days, why in SEQUENCE}
LAST_STEP = SEQUENCE[-1][0]


def step_note(n: int) -> str:
    return STEP_BY_N.get(n, ("", 0, ""))[2]


# ── which trades you're working ──────────────────────────────────────
# "You should be able to select trades that you want to work - if I feel
# like hitting just doctors I can email doctors; car dealerships, I can
# select that market." One setting, read by Today and by the Outbox's
# first-touch drafting. Follow-ups are NOT filtered: somebody already
# emailed is owed the rest of the sequence whatever you're working today.
FOCUS_SETTING = "focus_trades"


def focus_trades() -> list:
    try:
        v = json.loads(setting(FOCUS_SETTING, "") or "[]")
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    except ValueError:
        return []


def focus_sql(alias: str = "p") -> tuple:
    """(" AND (...)", params) restricting a prospects query to the trades in
    focus - by category (the fine label: 'Dental', 'Car Wash / Detail') or
    vertical (the coarse one: 'restaurant', 'auto'). Empty focus = no clause."""
    f = focus_trades()
    if not f:
        return "", []
    marks = ",".join("?" * len(f))
    return (" AND (COALESCE(%s.category,'') IN (%s) OR COALESCE(%s.vertical,'') IN (%s))"
            % (alias, marks, alias, marks), f + f)


HIDDEN_TRADES_SETTING = "hidden_trades"


def hidden_trades() -> list:
    """Trades the owner has said he doesn't want to see - off the trade
    pickers and never suggested for a lead search. The leads themselves are
    not touched."""
    try:
        v = json.loads(setting(HIDDEN_TRADES_SETTING, "") or "[]")
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    except ValueError:
        return []


def is_hidden_trade(t: str, hidden=None) -> bool:
    h = {x.lower() for x in (hidden if hidden is not None else hidden_trades())}
    return (t or "").strip().lower() in h


def focus_options(c) -> list:
    """Every trade on file, with how many leads it holds - minus the hidden."""
    hid = hidden_trades()
    out = []
    for r in c.execute("SELECT category AS k, COUNT(*) AS n FROM prospects "
                       "WHERE COALESCE(category,'')<>'' GROUP BY category ORDER BY n DESC"):
        if not is_hidden_trade(r["k"], hid):
            out.append({"key": r["k"], "kind": "category", "n": r["n"]})
    for r in c.execute("SELECT vertical AS k, COUNT(*) AS n FROM prospects "
                       "WHERE COALESCE(vertical,'')<>'' GROUP BY vertical ORDER BY n DESC"):
        if not is_hidden_trade(r["k"], hid):
            out.append({"key": r["k"], "kind": "vertical", "n": r["n"]})
    return out


class HideTradeBody(BaseModel):
    trade: str = Field(..., min_length=1, max_length=80)
    undo: bool = False


@app.post("/api/trades/hide")
def trades_hide(request: Request, body: HideTradeBody):
    """"I like to keep my dashboards clean. I need to be able to delete
    trades I don't want to see." One tap on a chip's x. Also drops it from
    the trades being worked, so a hidden trade can't quietly filter Today."""
    require_auth(request)
    t = body.trade.strip()
    hid = hidden_trades()
    if body.undo:
        hid = [x for x in hid if x.lower() != t.lower()]
    elif not is_hidden_trade(t, hid):
        hid.append(t)
        focus = [x for x in focus_trades() if x.lower() != t.lower()]
        set_setting(FOCUS_SETTING, json.dumps(focus))
    set_setting(HIDDEN_TRADES_SETTING, json.dumps(hid[:100]))
    return {"hidden": hid}


class FocusBody(BaseModel):
    trades: list = []


@app.get("/api/focus")
def focus_get(request: Request):
    require_auth(request)
    with closing(db()) as c:
        return {"trades": focus_trades(), "options": focus_options(c), "hidden": hidden_trades()}


@app.post("/api/focus")
def focus_set(request: Request, body: FocusBody):
    require_auth(request)
    trades = [str(x).strip()[:80] for x in (body.trades or []) if str(x).strip()][:30]
    set_setting(FOCUS_SETTING, json.dumps(trades))
    with closing(db()) as c:
        return {"trades": trades, "options": focus_options(c)}


# ── the tabs talking to each other ───────────────────────────────────
# "All the tabs need to talk to each other in some way so I'm not doing
# double the work." Every one of these is a place where one tab changed a
# lead and another tab kept showing the old truth. The rule now: a lead's
# state is one thing, and whoever changes it calls these.
CLOSED_STATUSES = ("booked", "dead", "corporate")      # nobody writes to these
QUIET_STATUSES = CLOSED_STATUSES + ("cold", "conversation")   # the machine doesn't


def retire_drafts(c, pid: int, reason: str, to_addr: str = "") -> int:
    """Take a lead's waiting emails out of the Outbox. Booked, passed,
    replied, unsubscribed, bounced - in every one of those the draft
    written yesterday must not go out today, and until this existed it
    stayed sendable (and autopilot would have sent it)."""
    q = "UPDATE outreach SET state='skipped', decided_at=?, note=? WHERE prospect_id=? " \
        "AND state IN ('draft','approved')"
    args = [now(), ("Retired: " + reason)[:600], pid]
    if to_addr:
        q += " AND lower(to_addr)=?"
        args.append(to_addr.strip().lower())
    c.execute(q, args)
    return c.execute("SELECT changes()").fetchone()[0]


# ── outreach reply sentiment ─────────────────────────────────────────────
# Used by scan_mailbox to auto-advance a lead from "working" to "warm" when
# someone writes back with genuine interest. Pure keyword matching - no model.
_REPLY_POSITIVE = [
    "interested", "sounds good", "sounds great", "tell me more", "send me more",
    "love to", "would love", "yes please", "yes,", "yes!", "yes i ", "yes we ",
    "how much", "what does it cost", "what's the cost", "pricing", "quote",
    "give me a call", "call me", "reach me at", "schedule", "set up a time",
    "set something up", "when are you available", "can we", "let's connect",
    "let's set", "let's talk", "id like to", "i'd like to", "open to",
    "may be interested", "might be interested", "actually yes", "great timing",
    "good timing", "been looking", "been thinking", "we do need", "we could use",
    "we'd be interested", "please send", "send over", "send the info",
]
_REPLY_NEGATIVE = [
    "not interested", "no thank", "no thanks", "please remove", "don't contact",
    "do not contact", "stop contacting", "stop emailing", "leave me alone",
    "wrong person", "wrong number", "not looking", "don't need", "do not need",
    "already have", "already handled", "we have someone", "taken care of",
    "not for us", "not right for us", "not a fit", "pass on this",
]


def classify_outreach_reply(text: str) -> str:
    """'positive' | 'neutral' | 'negative' for an outreach email back-reply.
    Keyword-only; runs synchronously so it can live inside scan_mailbox."""
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    if any(w in low for w in _REPLY_NEGATIVE):
        return "negative"
    if any(w in low for w in _REPLY_POSITIVE):
        return "positive"
    return "neutral"


def sync_stage(c, pid: int, event: str) -> None:
    """Keep the Pipeline's stage honest about what Today and the Outbox did.
    booked -> review; dead -> lost (and the deals close); a send or a reply
    moves a fresh lead to working; a warm (positive) reply moves working -> warm.
    Never moves a stage backwards."""
    r = c.execute("SELECT stage FROM prospects WHERE id=?", (pid,)).fetchone()
    if not r:
        return
    stage = r["stage"] or "lead"
    new = None
    if event == "booked" and stage in ("lead", "working", "warm"):
        new = "review"
    elif event == "dead" and stage not in ("customer", "lost"):
        new = "lost"
        c.execute("UPDATE deals SET state='lost', closed_at=? WHERE prospect_id=? AND state='open'",
                  (now(), pid))
    elif event == "warm_reply" and stage in ("lead", "working"):
        new = "warm"
    elif event in ("sent", "replied") and stage == "lead":
        new = "working"
    if new and new != stage:
        c.execute("UPDATE prospects SET stage=?, stage_at=? WHERE id=?", (new, now(), pid))
        log(c, pid, "stage", new, "%s → %s (from %s)" % (stage, new, event))


def advance_queue(c, pid: int, outcome: str, days: Optional[int] = None):
    """Move a prospect to its next touch. Returns a short human summary."""
    row = c.execute("SELECT step, touch_count, status FROM prospects WHERE id=?", (pid,)).fetchone()
    if not row:
        return "not found"
    step = int(row["step"] or 1)
    touches = int(row["touch_count"] or 0) + 1
    status = row["status"] or ""

    if outcome == "booked":
        c.execute("UPDATE prospects SET status='booked', next_due=NULL, next_action=NULL, "
                  "touch_count=?, touched_at=? WHERE id=?", (touches, now(), pid))
        retire_drafts(c, pid, "booked")
        sync_stage(c, pid, "booked")
        return "booked — out of the queue"

    if outcome == "dead":
        c.execute("UPDATE prospects SET status='dead', next_due=NULL, next_action=NULL, "
                  "touch_count=?, touched_at=? WHERE id=?", (touches, now(), pid))
        retire_drafts(c, pid, "passed")
        sync_stage(c, pid, "dead")
        return "closed out and added to do-not-contact"

    # A national chain is not a lost deal, it is a lead that should never have
    # been on the list - and the difference matters, because "dead" is the
    # denominator a rejection rate gets computed against. The scraper cannot
    # tell that The Wash Tub has thirty locations; Daniel knows it on sight,
    # and this is how he tells the system once instead of every week.
    if outcome == "corporate":
        c.execute("UPDATE prospects SET status='corporate', next_due=NULL, next_action=NULL, "
                  "touch_count=?, touched_at=? WHERE id=?", (touches, now(), pid))
        retire_drafts(c, pid, "flagged corporate")
        sync_stage(c, pid, "dead")
        return "flagged corporate - off the list for good"

    # An explicit callback date always wins over the sequence.
    if days is not None:
        c.execute("UPDATE prospects SET status='working', next_due=date('now', ?), "
                  "next_action='call', touch_count=?, touched_at=? WHERE id=?",
                  (f"+{int(days)} days", touches, now(), pid))
        return f"back to you in {int(days)} day{'s' if days != 1 else ''}"

    # Somebody who called us, or wrote back, is not on the cold ladder. A
    # "spoke" on their card used to hand them step 2 - EMAIL - and drop the
    # 'conversation' status that keeps the drafting sweep off them. They get
    # another call instead, and stay who they are.
    if status in ("conversation", "inbound"):
        gap = 5 if outcome in ("spoke", "emailed", "sent") else 2
        c.execute("UPDATE prospects SET next_action='call', next_due=date('now', ?), "
                  "touch_count=?, touched_at=? WHERE id=?",
                  (f"+{gap} days", touches, now(), pid))
        return f"they're a live conversation - call again in {gap} days"

    nxt = step + 1
    if nxt > LAST_STEP:
        c.execute("UPDATE prospects SET status='cold', next_due=NULL, next_action=NULL, "
                  "touch_count=?, touched_at=? WHERE id=?", (touches, now(), pid))
        return "sequence finished — moved to cold"

    kind, gap, _why = STEP_BY_N[nxt]
    c.execute("UPDATE prospects SET status='working', step=?, next_action=?, "
              "next_due=date('now', ?), touch_count=?, touched_at=? WHERE id=?",
              (nxt, kind, f"+{gap} days", touches, now(), pid))
    return f"next: {kind} in {gap} day{'s' if gap != 1 else ''}"


# ─────────────────── things that happen on their own ───────────────────
# Until this, the only automatic thing in the system was the follow-up day
# gap. Everything below fires on an event the system was already detecting and
# then discarding. See triggers.py for the decisions and for the one template
# deliberately not copied (open tracking).

CALLBACK_ACTION = "call_back"


def note_touch(c, pid, outcome, note):
    c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at)"
              " VALUES (?,?,?,?,?)", (pid, "system", outcome, note[:300], now()))


def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


# SQLite has no regex, so the stored phone is stripped of the punctuation a
# human types before it is compared. Cheap on a table this size, and the
# alternative - a normalised column - is a migration for one lookup.
_STRIP_PHONE = ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
                "COALESCE(%s,''),'(',''),')',''),'-',''),' ',''),'+',''),'.','')")


def find_prospect_by_phone(c, phone: str):
    """Whoever is already on file at this number, or None.

    Checked in order of certainty: our own synthetic key for a caller we
    have met before, then a business's listed number, then a named contact's.
    The middle one is the one that matters - when a restaurant we emailed
    last month rings the office, this is what makes it *that restaurant*
    calling back instead of a stranger with a Texas area code."""
    n = telnyx.to_e164(phone)
    last10 = _digits(n)[-10:]
    if not last10:
        return None
    row = c.execute("SELECT id FROM prospects WHERE place_id=?", ("phone:" + n,)).fetchone()
    if row:
        return row["id"]
    row = c.execute("SELECT id FROM prospects WHERE %s LIKE ? ORDER BY id LIMIT 1"
                    % (_STRIP_PHONE % "phone"), ("%" + last10,)).fetchone()
    if row:
        return row["id"]
    row = c.execute("SELECT prospect_id FROM contacts WHERE %s LIKE ? ORDER BY id LIMIT 1"
                    % (_STRIP_PHONE % "phone"), ("%" + last10,)).fetchone()
    return row["prospect_id"] if row else None


def inbound_lead(c, phone: str, *, name: str = "", kind: str = "call",
                 outcome: str = "", note: str = "", callback: bool = False,
                 hot: bool = False):
    """Somebody contacted the business. Make sure there is a lead to work.

    Every inbound path lands here - the AI receptionist, a missed call, a
    reply to the text-back - so an inbound caller is one record however
    they reach us, and calling twice does not produce two of them.

    `status='inbound'`, never `'new'`: a startup migration schedules every
    `new` prospect into the OUTBOUND cold-call queue, and cold-calling
    somebody who just rang you is the kind of bug a customer notices.
    """
    n = telnyx.to_e164(phone)
    if not n:
        return None
    pid = find_prospect_by_phone(c, n)
    if pid:
        c.execute("UPDATE prospects SET touched_at=? WHERE id=?", (now(), pid))
        # A number we only knew as a contact's is now a number that calls us.
        c.execute("UPDATE prospects SET place_id=? WHERE id=? AND COALESCE(place_id,'')=''",
                  ("phone:" + n, pid))
    else:
        cur = c.execute(
            "INSERT INTO prospects (place_id, company, phone, city, vertical, status, "
            "stage, added_at, touched_at) VALUES (?,?,?,?,?,'inbound','lead',?,?)",
            ("phone:" + n, (name.strip() or "Inbound caller %s" % n), n,
             setting("default_city", "San Antonio, TX"), "generic", now(), now()))
        pid = cur.lastrowid
    if name.strip():
        dupe = c.execute("SELECT id FROM contacts WHERE prospect_id=? AND %s LIKE ?"
                         % (_STRIP_PHONE % "phone"), (pid, "%" + _digits(n)[-10:])).fetchone()
        if not dupe:
            c.execute("INSERT INTO contacts (prospect_id, name, phone, is_primary, note, added_at) "
                      "VALUES (?,?,?,1,?,?)", (pid, name.strip(), n, "called in", now()))
    log(c, pid, kind, outcome, note)
    if hot:
        # They answered a human. Same treatment an email reply gets.
        put_on_the_phone(c, pid, note or "Replied to the text-back")
    elif callback:
        cur = c.execute("SELECT status, next_due FROM prospects WHERE id=?", (pid,)).fetchone()
        if cur and (cur["status"] or "") not in ("booked", "corporate", "conversation"):
            c.execute("UPDATE prospects SET next_due=date('now'), next_action='call' "
                      "WHERE id=?", (pid,))
    return pid


def sort_inbound(c, pid: int, text: str, *, urgency: str = "", requested_time: str = "",
                 channel: str = "sms") -> dict:
    """Put their message in one of the four buckets and draft the reply
    (inbound.py - the speed-to-lead playbook). Stored on the prospect so
    Today can show the bucket and the draft; nothing is sent here."""
    p = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not p:
        return {}
    d = dict(p)
    repeat = (d.get("status") or "") == "booked"
    srt = inbound.sort(text, urgency=urgency, requested_time=requested_time, repeat=repeat, workspace=ws.current())
    person = c.execute("SELECT name FROM contacts WHERE prospect_id=? ORDER BY is_primary DESC, id LIMIT 1", (pid,)).fetchone()
    name = (person["name"] if person else "") or ""
    if not name and not (d.get("company") or "").startswith("Inbound caller"):
        name = ""
    times = [t.strip() for t in re.split(r"[,\n]", setting("callback_times", "10:00, 15:00")) if t.strip()]
    slots = inbound.next_slots(datetime.now(workspace_tz()), times)
    reply = inbound.draft(srt, text=text, name=name, business=ws.info()["short"],
                          sender=(setting("sender_name", "").strip().split(" ")[0] if setting("sender_name", "").strip() else ""),
                          slots=slots, workspace=ws.current(), referral=setting("referrals", ""), channel=channel)
    c.execute("UPDATE prospects SET inbound_bucket=?, inbound_why=?, inbound_text=?, inbound_reply=?, inbound_at=? WHERE id=?",
              (srt["bucket"], srt["why"], (text or "")[:1000], reply, now(), pid))
    return dict(srt, reply=reply)


def put_on_the_phone(c, pid: int, why: str) -> bool:
    """Somebody wrote back. Put them at the top of today's call list.

    This is a fix as much as a feature. A reply used to clear `next_due`,
    and the queue only shows rows where `next_due IS NOT NULL` - so the
    single warmest lead in the system disappeared from the list the moment
    it became warm. The intent was right (stop the machine talking over a
    person) and the effect was backwards (stop the person too).

    `conversation` keeps them out of the draft sweep and the follow-up
    sweep, which both exclude that status, while leaving them in the call
    queue. The machine stops; the human starts.
    """
    cur = c.execute("SELECT status FROM prospects WHERE id=?", (pid,)).fetchone()
    if not cur or (cur["status"] or "") in ("booked", "corporate"):
        return False
    # A lead marked 'dead' who then writes back or calls has un-passed
    # themselves. Before this they stayed invisible for good.
    c.execute("UPDATE prospects SET status='conversation', next_action=?, "
              "next_due=date('now'), touched_at=? WHERE id=?",
              (CALLBACK_ACTION, now(), pid))
    retire_drafts(c, pid, "they replied - a person is on this now")
    sync_stage(c, pid, "replied")
    note_touch(c, pid, "queued:call_back", why)
    return True


def retarget_after_bounce(c, pid: int, bad_addr: str):
    """The address is dead. The lead is not.

    A bounce used to end the conversation with that business outright.
    Records the dead address, then hands the next contact there to
    `best_recipient` - which is the same ranking rule every other send
    uses, so the replacement is chosen the way a first touch would be, not
    by grabbing whatever is left.

    Returns the address it moved to, or None if that business has no other.
    """
    mark_dead_address(c, bad_addr, pid, "bounce")
    retire_drafts(c, pid, "address bounced", to_addr=bad_addr)
    prow = c.execute("SELECT email, company FROM prospects WHERE id=?", (pid,)).fetchone()
    nxt = trig.usable_addresses(contact_rows(c, pid),
                                (prow["email"] if prow else "") or "",
                                dead_addresses(c))
    if not nxt:
        c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL WHERE id=?", (pid,))
        note_touch(c, pid, "bounce:no_other_address",
                   "%s bounced and there is no other address for them." % bad_addr)
        return None
    addr, name = nxt[0]
    cur = c.execute("SELECT status FROM prospects WHERE id=?", (pid,)).fetchone()
    if cur and (cur["status"] or "") not in ("booked", "dead", "corporate", "conversation"):
        c.execute("UPDATE prospects SET status='working', next_action='email', "
                  "next_due=date('now'), touched_at=? WHERE id=?", (now(), pid))
    note_touch(c, pid, "bounce:retargeted",
               "%s bounced - next is %s%s." % (bad_addr, addr,
                                               (" (%s)" % name) if name else ""))
    return addr


def reawaken_days() -> int:
    return _cadence(snap.SETTING_REAWAKEN, trig.REAWAKEN_DAYS_DEFAULT)


def run_reawaken(c, days=None) -> list:
    """Give a finished-ladder lead one more approach, once.

    A prospect who ran out of sequence goes `cold` and is never looked at
    again - 275 leads accumulate there and nothing ever reads that pile.
    This is one second chance, marked so it cannot repeat: Apollo's
    equivalent template loops, which is how a list turns into a nuisance.

    It comes back as a CALL, not an email. Someone who ignored the whole
    email ladder has already answered that question.
    """
    days = int(days or reawaken_days())
    woken = []
    rows = c.execute("SELECT id, company, status, touched_at, "
                     "COALESCE(reawakened_at,'') AS reawakened_at "
                     "FROM prospects WHERE status='cold'").fetchall()
    for r in rows:
        if not trig.due_for_reawaken(r["status"], r["touched_at"],
                                     r["reawakened_at"], days):
            continue
        c.execute("UPDATE prospects SET status='working', next_action='call', "
                  "next_due=date('now'), reawakened_at=?, touched_at=? WHERE id=?",
                  (now(), now(), r["id"]))
        note_touch(c, r["id"], "reawakened",
                   "Cold since %s - one more try." % (r["touched_at"] or "?")[:10])
        woken.append({"id": r["id"], "company": r["company"],
                      "cold_since": (r["touched_at"] or "")[:10]})
    return woken


# ─────────────────────── routes ───────────────────────

# ─────────────────── serving the page in its own colours ───────────────────

_PAGE_CACHE: dict = {}


def themed(path) -> str:
    """Serve the dashboard already wearing this workspace's theme.

    The alternative — letting the browser fetch /api/me and set the attribute
    from JavaScript — paints The Watson Factor's brass for a frame or two
    first. A flash of the wrong brand is precisely the confusion this theme
    exists to prevent, so the attribute is stamped on before the bytes leave.

    Cached per (file, mtime), so editing dashboard.html still takes effect on
    a plain refresh the way it always has.
    """
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns)
    html = _PAGE_CACHE.get(key)
    if html is None:
        html = path.read_text(encoding="utf-8")
        _PAGE_CACHE.clear()          # one file, one version; don't grow forever
        _PAGE_CACHE[key] = html
    slug = ws.current()
    if slug == "homerepair":
        # its own home-screen icon and label, so the two workspaces can sit
        # side by side on a phone and you can tell them apart
        html = html.replace("/welcome/jg-icon-", "/welcome/jg-icon-hr-").replace(
            '<meta name="apple-mobile-web-app-title" content="Just Grit">',
            '<meta name="apple-mobile-web-app-title" content="JG HomeRepair">', 1)
    return html.replace('<html lang="en" data-theme="dark">',
                        '<html lang="en" data-theme="dark" data-ws="%s">' % slug, 1)


# ─────────────────────────── theme assets ───────────────────────────
# The badge is small enough to inline in the page. The camo field is 137 KB and
# would be re-sent on every single page load if it were, because the dashboard
# is generated per request. So: one route, and the browser caches it a week.

ASSET_TYPES = {"camo.jpg": "image/jpeg"}   # a whitelist, not a lookup. Never
                                           # join a path segment from a URL onto
                                           # a directory and hope for the best.


GRIT_CSS = ROOT / "static" / "grit.css"


@app.get("/grit.css", include_in_schema=False)
def grit_css():
    """The shared design tokens. Public: the sign-in and legal pages wear
    them before anyone is signed in, and there is nothing secret in a
    stylesheet."""
    if not GRIT_CSS.exists():
        raise HTTPException(404, "grit.css missing")
    return FileResponse(GRIT_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/asset/{name}", include_in_schema=False)
def theme_asset(request: Request, name: str):
    require_auth(request)
    media = ASSET_TYPES.get(name)
    if not media:
        raise HTTPException(404, "No such asset.")
    p = ASSETS / name
    if not p.exists():
        raise HTTPException(404, "%s is missing from static/assets/." % name)
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/", include_in_schema=False)
def home(request: Request):
    require_auth(request)
    # The Scout cockpit is the front door; fall back to the simple CRM if the
    # cockpit file isn't present.
    if DASH.exists():
        # no-cache: Safari kept serving a stale dashboard after an update
        # (2026-09-27, the empty calendar) - revalidate every load instead.
        return HTMLResponse(themed(DASH), headers={"Cache-Control": "no-cache"})
    if UI.exists():
        return FileResponse(UI, media_type="text/html")
    raise HTTPException(500, "dashboard.html missing")


LANDING = ROOT / "static" / "landing.html"


@app.get("/welcome", include_in_schema=False)
def welcome():
    """The public landing page. No sign-in - it is the page a stranger reads
    before deciding to book. Cloudflare Access still sits in front of the
    hostname, so it needs the same kind of bypass policy /webhooks/* has,
    for the path /welcome - or host the file anywhere static."""
    if not LANDING.exists():
        raise HTTPException(404, "not found")
    return FileResponse(LANDING, media_type="text/html",
                        headers={"Cache-Control": "public, max-age=300"})


# The public "what we do" page is read by a stranger who has no email open next
# to it, so every card has to stand on its own: a moment they'll recognise, then
# what actually gets built. (The one-line OFFER_COPY is written to follow an
# email opener - "Every one of those is..." - and means nothing on this page.)
# Nothing here claims a result, a price, or a fact about the reader's business.
ONE_SHEET_COPY = {
    "watson": [
        ("AI Receptionist",
         "It's 6:40 on a Friday and someone with a real problem calls. You're under a truck or with another customer, so it goes to voicemail, and they call the next number on the list.",
         "I put an assistant on your line. It picks up every time, finds out what's going on, takes their name and number, and texts you the details. You keep your number, and you call back the ones worth calling."),
        ("Website",
         "Someone nearby searches for what you do and clicks the first thing that loads. If the site is slow, hard to read on a phone, or buries the phone number, they back out and try the next one.",
         "I build a plain, fast page that says what you do and where you do it, with your number at the top so it can be tapped. It's set up so Google can read it and so a customer can reach you in one move."),
        ("Reviews & Rewards",
         "Most of your customers are happy and never say so. The few who aren't are the ones who write it up, and that's the rating the next person sees.",
         "I set up a short text that goes to every customer right after the job, with one tap to your Google page. People who leave a review get a small thank-you from you."),
        ("Ordering App",
         "A customer orders through a delivery app. The app keeps a cut of the order, and it keeps the customer's name and number too, so you can't reach them again.",
         "I build you an ordering page of your own, with your menu and your prices. The orders come to you, you keep the customer list, and nobody takes a commission."),
        ("AI Vision",
         "Something goes wrong on the floor - a table that's waited too long, a gate left open, a lobby that's filling up - and you hear about it in a review the next day.",
         "I connect software to the cameras you already have, or ones I add, and it tells a manager while it's happening. Footage is searchable in plain English, like \"the silver truck around 4pm.\""),
    ],
    # HomeRepair.tech sells upkeep to people who look after buildings, so the
    # page leads with the four buyers it is actually pitched to.
    "homerepair": [
        ("Property managers",
         "A tenant texts at 9 on a weeknight that the water heater is leaking. You're covering a lot of doors, and the vendor who picked up last time can't get there until Thursday.",
         "The tenant snaps a photo and sends it to us. You see it with a plain description and tap approve, and we send a tech inside the dollar limit you set. Every visit leaves a dated photo record on that property."),
        ("HOA and condo boards",
         "The board meets Tuesday. The gate arm is broken, a light is out at the pool, the clubhouse gutters are full, and nobody is sure when anyone last looked at any of it.",
         "We walk the property every month and fix the small stuff while we're there. The board gets photos of everything. When something is bigger, you get a written scope and price before any work starts."),
        ("Commercial buildings",
         "A tenant calls about the AC. A different tenant calls about a leak. Each one is a different vendor and a different phone call for you.",
         "We walk the building on a schedule and handle the small repairs while we're there, so they don't turn into tenant calls. You get one company to call and a dated photo record for each address."),
        ("Realtors",
         "The inspection report lands, and your buyer wants repair numbers before the deadline, which is three days away.",
         "Send us the photos from the report and we turn them into a written estimate the same day. Anything we repair is quoted before the work starts."),
        ("Homeowners",
         "Most repairs that cost real money started as something small that nobody checked: a clogged gutter, an AC that was never serviced, a water heater in its last year.",
         "Protect plans put a tech at the house every season - AC in spring, water heater in summer, gutters in fall, a freeze check in winter. You also get a condition score and a record that stays with the house. Month to month."),
    ],
}

# The line under the page title, and the closing line, per workspace.
ONE_SHEET_SUB = {
    "homerepair": "What HomeRepair.tech does for property managers, HOA boards, commercial buildings and realtors in %s - one page, no pitch deck.",
}
ONE_SHEET_FOOT = {
    "homerepair": "Every one of these is set up one property at a time. Reply to the email that brought you here and it goes straight to %s.",
}


@app.get("/welcome/offers", include_in_schema=False, response_class=HTMLResponse)
def one_sheet():
    """One public page on everything this workspace sells - the 'one-sheeter'
    the last follow-up links to. Under /welcome/ so Cloudflare Access lets a
    stranger read it. Built from the offer catalog and the Setup fields,
    nothing typed twice."""
    cat = workspace_catalog().get("offers") or {}
    w = ws.me()
    name = setting("sender_company", "").strip() or w["name"]
    who = setting("sender_name", "").strip()
    phone = setting("sender_phone", "").strip()
    email_ = setting("sender_email", "").strip()
    site = re.sub(r"^https?://", "", setting("sender_site", "").strip())
    city = setting("default_city", "").strip() or "San Antonio, TX"
    accent = w.get("accent") or "#e8a33d"
    e = html.escape
    rows = []
    own = ONE_SHEET_COPY.get(ws.current())
    if own:
        for oname, scene, what in own:
            rows.append('<section class="o"><h2>%s</h2><p class="cost">%s</p><p class="fix">%s</p></section>'
                        % (e(oname), e(scene), e(what)))
    else:
        for oname, o in cat.items():
            rows.append('<section class="o"><h2>%s</h2><p class="cost">%s</p><p class="fix">%s</p></section>'
                        % (e(oname), e(o.get("cost", "")), e(o.get("fix", ""))))
    contact = " · ".join(x for x in [
        e(who) if who else "",
        ('<a href="tel:%s">%s</a>' % (e(re.sub(r"[^0-9+]", "", phone)), e(phone))) if phone else "",
        ('<a href="mailto:%s">%s</a>' % (e(email_), e(email_))) if email_ else "",
        ('<a href="https://%s">%s</a>' % (e(site), e(site))) if site else ""] if x)
    page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>" + e(name) + " - what we do</title><meta name=\"robots\" content=\"noindex\">"
            "<style>:root{--a:" + accent + "}body{margin:0;background:#0f0f10;color:#e9e6df;"
            "font:16px/1.55 -apple-system,system-ui,Segoe UI,sans-serif}main{max-width:720px;margin:0 auto;"
            "padding:36px 20px 60px}h1{font-size:28px;margin:0 0 4px}.sub{color:#a9a49a;margin:0 0 26px}"
            ".o{border:1px solid #2a2a2c;border-radius:12px;padding:16px 18px;margin:0 0 12px}"
            ".o h2{font-size:18px;margin:0 0 6px;color:var(--a)}.o p{margin:0 0 6px}.cost{color:#c9c4ba}"
            ".q{color:#8f8a80;font-style:italic;font-size:14px}.cta{margin-top:26px;padding:16px 18px;"
            "border-radius:12px;background:#17171a;border:1px solid #2a2a2c}.cta a{color:var(--a);"
            "text-decoration:none;font-weight:600}.foot{color:#7d786f;font-size:13px;margin-top:22px}"
            "</style></head><body><main><h1>" + e(name) + "</h1>"
            "<p class=\"sub\">" + e(ONE_SHEET_SUB.get(ws.current(), "What we do for businesses in %s - one page, no pitch deck.") % city) + "</p>"
            + ("\n".join(rows) or "<p class=\"sub\">Offers are being written - check back shortly.</p>")
            + "<div class=\"cta\"><b>Worth a look?</b> " + contact + "</div>"
            "<p class=\"foot\">" + e(ONE_SHEET_FOOT.get(ws.current(), "Every one of these is set up for one business at a time. Reply to the email that brought you here and it goes straight to %s.") % (who.split()[0] if who else "us")) + "</p>"
            "</main></body></html>")
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=300"})


PUBLIC_ASSET_TYPES = {"og-image.png": "image/png",
    "jg-icon-32.png": "image/png", "jg-icon-180.png": "image/png", "jg-icon-192.png": "image/png", "jg-icon-512.png": "image/png", "jg-icon-hr-32.png": "image/png", "jg-icon-hr-180.png": "image/png", "jg-icon-hr-192.png": "image/png", "jg-icon-hr-512.png": "image/png"}   # allow-list, same rule as
                                                      # ASSET_TYPES above: never join
                                                      # a URL path segment onto a
                                                      # directory and hope for the best.


@app.get("/welcome/{name}", include_in_schema=False)
@app.get("/public/{name}", include_in_schema=False)
def public_asset(name: str):
    """Unauthenticated static files - link-preview crawlers (iMessage, Slack,
    Facebook, etc.) can't get through Cloudflare Access any more than the
    /webhooks/* or /welcome routes can, so anything they need to fetch (right
    now: the og:image for /welcome) has to be served with no auth check, same
    as /welcome itself.

    Two paths, one handler. Cloudflare Access bypasses are PREFIX matches, so
    the policy that already lets the world read /welcome also lets it read
    /welcome/og-image.png - which is why the page points there. /public/ is
    the tidy address and works the day a bypass for it exists."""
    media = PUBLIC_ASSET_TYPES.get(name)
    if not media:
        raise HTTPException(404, "No such asset.")
    p = PUBLIC / name
    if not p.exists():
        raise HTTPException(404, "%s is missing from static/public/." % name)
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/app", include_in_schema=False)
def classic(request: Request):
    """The simple Find / My list / Setup CRM, kept available."""
    require_auth(request)
    if not UI.exists():
        raise HTTPException(500, "app.html missing")
    return FileResponse(UI, media_type="text/html")


VERSION_FILE = ROOT / "VERSION"


def app_version() -> str:
    try:
        return VERSION_FILE.read_text().strip() or "dev"
    except OSError:
        return "dev"


def backup_age_hours():
    """Hours since the newest file in backups/nightly, or None."""
    d = ROOT / "backups" / "nightly"
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    newest = max(p.stat().st_mtime for p in files)
    return (time.time() - newest) / 3600


def health_dict() -> dict:
    ok_db = True
    n = 0
    try:
        with closing(db()) as c:
            n = c.execute("SELECT COUNT(*) n FROM prospects").fetchone()["n"]
    except Exception:
        ok_db = False
    loops, stale = {}, []
    if not os.environ.get("JUST_GRIT_NO_LOOP"):
        for name, every in LOOP_EVERY.items():
            t = HEARTBEAT.get(name)
            age = (time.time() - t) if t else None
            loops[name] = round(age) if age is not None else None
            if age is None or age > every * 3 + 120:
                stale.append(name)
    try:
        st = os.statvfs(str(DATA))
        free_mb = round(st.f_bavail * st.f_frsize / 1e6)
    except OSError:
        free_mb = None
    b = backup_age_hours()
    ok = ok_db and not stale and (free_mb is None or free_mb > 500)
    return {"ok": ok, "version": app_version(), "db": ok_db, "prospects": n, "loops": loops, "stale": stale,
            "disk_free_mb": free_mb, "backup_age_h": round(b, 1) if b is not None else None,
            "workspace": ws.current(), "at": now()}


@app.get("/health", include_in_schema=False)
def health(request: Request):
    """Machine-readable. 200 when everything is fine, 503 when it isn't, so
    a keep-alive or a watchdog can act on the code alone."""
    h = health_dict()
    return Response(content=json.dumps(h), media_type="application/json", status_code=200 if h["ok"] else 503)


# ── the pages a stranger reads: help, terms, privacy, what's new, status ──
CHANGELOG = ROOT / "CHANGELOG.md"


def _operator() -> tuple:
    """(product, legal entity, contact) - primary workspace settings."""
    tok = ws.CURRENT.set(ws.PRIMARY)
    try:
        entity = setting("legal_entity", "").strip() or "The Watson Factor"
        contact = setting("support_email", "").strip() or setting("sender_email", "").strip() or "daniel@thewatsonfactor.com"
    finally:
        ws.CURRENT.reset(tok)
    return "Just Grit", entity, contact


@app.get("/terms", include_in_schema=False)
@app.get("/privacy", include_in_schema=False)
@app.get("/help", include_in_schema=False)
def legal_pages(request: Request):
    kind = request.url.path.strip("/")
    p, e, c = _operator()
    return HTMLResponse(pages.render_page(kind, p, e, c, setting("terms_date", "") or "26 September 2026"))


@app.get("/changelog", include_in_schema=False)
def changelog_page(request: Request):
    p, e, c = _operator()
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        text = "# What's new\n\nNothing written down yet."
    return HTMLResponse(pages.render_changelog(text, p, e, c))


@app.get("/status", include_in_schema=False)
def status_page(request: Request):
    p, e, c = _operator()
    return HTMLResponse(pages.render_status(health_dict(), p, e, c))


# ── your data: out as a zip, or gone ─────────────────────────────────────
SECRET_KEY_RX = re.compile(r"(token|password|secret|_key$|^google_key|api_key|passwd)", re.I)


@app.get("/api/export.zip")
def export_zip(request: Request):
    """Everything in this workspace as CSV files plus the non-secret
    settings, in one zip. Opens in Excel. Keys and passwords are left out
    on purpose - they are the owner's to re-enter, never to copy around."""
    require_auth(request)
    buf = io.BytesIO()
    with closing(db()) as c, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table, fname in [("prospects", "leads.csv"), ("contacts", "contacts.csv"), ("outreach", "emails.csv"),
                             ("touches", "activity.csv"), ("deals", "deals.csv"), ("social_queue", "posts.csv"),
                             ("social_campaigns", "campaigns.csv")]:
            try:
                rows = c.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
            except sqlite3.Error:
                continue
            s = io.StringIO()
            if rows:
                w = csv.DictWriter(s, fieldnames=rows[0].keys())
                w.writeheader()
                for r in rows:
                    w.writerow({k: r[k] for k in r.keys()})
            z.writestr(fname, s.getvalue())
        st = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM settings") if not SECRET_KEY_RX.search(r["k"] or "")}
        z.writestr("settings.json", json.dumps(st, indent=2, sort_keys=True))
        z.writestr("README.txt", "Just Grit export for %s, %s.\nleads.csv, contacts.csv, emails.csv, activity.csv, deals.csv, posts.csv, campaigns.csv open in Excel or Numbers.\nsettings.json is your Setup page without any keys or passwords.\n" % (ws.info()["name"], now()))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="just-grit-%s-%s.zip"' % (ws.current(), local_today())})


class DeleteWorkspaceBody(BaseModel):
    slug: str = ""
    confirm_name: str = ""


@app.post("/api/workspace/delete")
def workspace_delete(request: Request, body: DeleteWorkspaceBody):
    """Delete a customer workspace: its database file moves to
    backups/deleted/ (kept 30 days by the nightly sweep) and it leaves the
    registry. Owner only, never the built-ins, and the name has to be
    typed back exactly - a dialog click is not enough for this one."""
    require_owner(request)
    slug = (body.slug or "").strip().lower()
    if slug in (ws.PRIMARY, "homerepair") or slug not in ws.WORKSPACES:
        raise HTTPException(400, "That workspace can't be deleted from here.")
    info = ws.info(slug)
    if (body.confirm_name or "").strip() != info["name"]:
        raise HTTPException(400, "Type the workspace's name exactly as shown to confirm: %s" % info["name"])
    src = DATA / info["db"]
    dest_dir = ROOT / "backups" / "deleted"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = ""
    if src.exists():
        dest = dest_dir / ("%s-%s.db" % (slug, datetime.now().strftime("%Y%m%d-%H%M%S")))
        src.replace(dest); moved = dest.name
        for extra in (str(src) + "-wal", str(src) + "-shm"):
            try:
                os.remove(extra)
            except OSError:
                pass
    with closing(db(ws.PRIMARY)) as c:
        c.execute("UPDATE %s SET active=0 WHERE slug=?" % ws.REGISTRY_TABLE, (slug,))
        c.commit()
    ws.WORKSPACES.pop(slug, None)
    return {"ok": True, "slug": slug, "kept_as": moved, "note": "Its database is parked in backups/deleted for 30 days, then gone."}


@app.get("/api/me")
def me(request: Request):
    email = require_auth(request)
    return {
        "email": email,
        "signed_in": bool((request.cookies.get(auth.COOKIE) or "").strip()),
        "legal_entity": setting("legal_entity", ""), "support_email": setting("support_email", ""), "alert_phone": setting("alert_phone", ""),
        "version": app_version(),
        "has_google_key": bool(google_key()),
        "google_key_source": google_key_source()[1],
        "has_own_google_key": bool((setting("google_key_own") or "").strip()) if ws.current() != ws.PRIMARY else False,
        "sender_name": setting("sender_name", ""),
        "sender_company": setting("sender_company", ""),
        "sender_phone": setting("sender_phone", ""),
        "sender_site": setting("sender_site", ""),
        "sender_address": setting("sender_address", ""),
        "owner": is_owner(request),
        # The owner list is the owners' business; a guest gets a blank field,
        # which save_settings treats as "left alone", not "cleared".
        "allowed_emails": setting("allowed_emails", "") if is_owner(request) else "",
        # This workspace's own guest list - the customer's logins. Owners see
        # it in Setup -> Who can sign in; a guest gets a blank, same rule.
        "workspace_emails": setting("workspace_emails", "") if is_owner(request) else "",
        "default_city": setting("default_city", "San Antonio, TX"),
        "daily_email_cap": str(daily_email_cap()),
        "sender_email": setting("sender_email", ""),
        "free_request_cap": request_cap(),
        "workspace": ws.me(),
        "workspaces": ws.public(),
        "has_telnyx": bool(telnyx_settings_ready()),
        "telnyx_connection_id": setting("telnyx_connection_id", ""),
        "telnyx_from_number": setting("telnyx_from_number", ""),
        "telnyx_rep_number": setting("telnyx_rep_number", ""),
        # Host and address come back so the Setup fields populate. The
        # password never does - same rule the Google and Telnyx keys follow.
        "imap_host": setting("imap_host", ""),
        "imap_user": setting("imap_user", ""),
        "has_imap_password": bool(setting("imap_password", "").strip()),
        "fb_app_id": setting("fb_app_id", ""),
        "has_fb_app_secret": bool(setting("fb_app_secret", "").strip()),
        "telnyx_webhook_url": telnyx_webhook_url(),
        "telnyx_assistant_webhook_url": telnyx_assistant_webhook_url(),
        "textback": textback_status(),
        "imagegen": imagegen_status(),
        "public_base_url": setting("public_base_url", ""),
        "public_base": public_base(),
        "realtor_page_url": setting("realtor_page_url", ""),
        "pm_page_url": pm_page_url(),
        "never_say": setting("never_say", ""),
        "callback_times": setting("callback_times", "10:00, 15:00"),
        "referrals": setting("referrals", ""),
    }


@app.post("/api/settings/clear_google_key")
def clear_google_key(request: Request):
    """Remove the key stored in Settings so whatever is on disk takes over.

    Saving an empty field can't do this (see save_settings), which meant a
    malformed key pasted into Settings permanently shadowed a good one in
    `.google_api_key` with no way out but a SQL client."""
    require_owner(request)      # the account's key, not the workspace's
    set_setting("google_key", "")
    key, src = google_key_source()
    return {"cleared": True, "now_using": src or "nothing",
            "usable": looks_like_google_key(key)}


@app.post("/api/settings/clear_telnyx_key")
def clear_telnyx_key(request: Request):
    """Same escape hatch as clear_google_key, for the Telnyx API key."""
    require_auth(request)
    set_setting("telnyx_api_key", "")
    return {"cleared": True}


@app.post("/api/settings/clear_imagegen_key")
def clear_imagegen_key(request: Request):
    """Same escape hatch, for the image-generation key. Workspace-level, so
    a customer can disconnect their own provider."""
    require_auth(request)
    set_setting("imagegen_api_key", "")
    return {"cleared": True}


def telnyx_settings_ready() -> bool:
    return all(setting(k, "") for k in
               ("telnyx_api_key", "telnyx_connection_id",
                "telnyx_from_number", "telnyx_rep_number"))


def telnyx_webhook_token() -> str:
    """A long random path segment, generated once and kept, so the webhook
    URL can't be guessed by anyone who doesn't already have it. This is not
    Telnyx's own signed-webhook verification (that needs their public key
    and an extra crypto dependency) - it's the same "unguessable path"
    trade Just Grit already makes elsewhere, sized to what a v1 needs."""
    tok = setting("telnyx_webhook_token", "")
    if not tok:
        tok = uuid.uuid4().hex
        set_setting("telnyx_webhook_token", tok)
    return tok


def clean_base_url(raw: str) -> str:
    """A workspace's public address, or "". Scheme forced to https, trailing
    slash and any path dropped.

    Everything a stranger's server has to reach hangs off this: the Telnyx
    webhooks, the media URLs Meta fetches, the review cards. A trailing
    slash here becomes a double slash in a URL handed to a carrier, which
    fails in a way nobody can read, so it is fixed once, here."""
    u = (raw or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    parts = urllib.parse.urlsplit(u)
    host = (parts.netloc or "").strip().lower()
    if not host or " " in host or "." not in host:
        raise ValueError("%r is not a hostname this can be reached at." % raw)
    return "https://" + host


def public_base() -> str:
    """The base URL for anything outside this app has to fetch.

    The typed setting wins; a workspace that registered a hostname gets that
    for free, so a customer created with a host never has to know this field
    exists. One reader, so the webhook URL in Setup and the media URL handed
    to Meta can never disagree about where this workspace lives."""
    typed = (setting("public_base_url", "") or "").strip()
    if typed:
        try:
            return clean_base_url(typed)
        except ValueError:
            return ""
    host = (ws.info()["hosts"] or [""])[0]
    return "https://" + host if host else ""


def telnyx_webhook_url() -> str:
    base = public_base()
    if not base:
        return ""
    return f"{base}/webhooks/telnyx/voice/{telnyx_webhook_token()}"


def telnyx_assistant_webhook_url() -> str:
    """Same token, different path - this is the URL to paste into the
    Telnyx AI Assistant's Webhook tool config, not the Voice API
    Application's webhook field (that one stays pointed at /voice/)."""
    base = public_base()
    if not base:
        return ""
    return f"{base}/webhooks/telnyx/assistant/{telnyx_webhook_token()}"


def _best_phone(c, pid: int) -> str:
    """The number a human would actually dial: the primary contact's phone
    first (contact_rows already sorts is_primary DESC), else the business's
    own listed number. Mirrors dashboard.html's own bestPhone logic exactly
    so the button dials whatever the card already shows."""
    for row in contact_rows(c, pid):
        if row.get("phone"):
            return row["phone"]
    prow = c.execute("SELECT phone FROM prospects WHERE id=?", (pid,)).fetchone()
    return (prow["phone"] if prow else "") or ""


@app.get("/api/metros")
def list_metros(request: Request):
    require_auth(request)
    metros = load_metros()
    out = [{"key": k, "label": m.get("label", k), "zips": len(m.get("zips", []))}
           for k, m in metros.items()]
    out.sort(key=lambda x: (-x["zips"], x["label"]))
    return {"metros": out}


class Settings(BaseModel):
    google_key: Optional[str] = None
    google_key_own: Optional[str] = None       # a customer workspace's own Maps key (not the account one)
    sender_name: Optional[str] = None
    sender_company: Optional[str] = None
    sender_phone: Optional[str] = None
    sender_site: Optional[str] = None
    sender_address: Optional[str] = None
    allowed_emails: Optional[str] = None
    default_city: Optional[str] = None
    sells: Optional[str] = None
    free_request_cap: Optional[str] = None
    daily_email_cap: Optional[str] = None
    sender_email: Optional[str] = None
    telnyx_api_key: Optional[str] = None
    telnyx_connection_id: Optional[str] = None
    telnyx_from_number: Optional[str] = None
    telnyx_rep_number: Optional[str] = None
    imap_host: Optional[str] = None
    imap_user: Optional[str] = None
    imap_password: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[str] = None
    autopilot_mode: Optional[str] = None
    autopilot_per_hour: Optional[str] = None
    timezone: Optional[str] = None
    textback_enabled: Optional[str] = None          # "1" or ""
    textback_message: Optional[str] = None
    textback_forward_to: Optional[str] = None
    textback_cooldown_hours: Optional[str] = None
    telnyx_messaging_profile_id: Optional[str] = None
    # Image generation for the Social tab - the customer's own provider and
    # key, per workspace, so the picture bill lands on whoever made it.
    imagegen_provider: Optional[str] = None         # meta | elevenlabs | openai | ''
    imagegen_api_key: Optional[str] = None
    imagegen_base_url: Optional[str] = None
    imagegen_model: Optional[str] = None
    imagegen_style: Optional[str] = None            # brand notes prepended to prompts
    workspace_emails: Optional[str] = None          # this workspace's guest logins (owner-only)
    # Where the world reaches THIS workspace. Read by the Telnyx webhooks,
    # the media URLs Meta fetches and the review cards - and until now it
    # could only be set with a SQL client, which made a customer workspace
    # impossible to finish setting up from the app it ships with.
    public_base_url: Optional[str] = None
    realtor_page_url: Optional[str] = None     # HomeRepair: the partner page the realtor emails link to
    pm_page_url: Optional[str] = None          # HomeRepair: the property-manager page the PM emails link to
    legal_entity: Optional[str] = None         # who operates the app (terms, privacy, help footers)
    support_email: Optional[str] = None
    alert_phone: Optional[str] = None           # the watchdog texts this when /health fails
    never_say: Optional[str] = None            # words Daniel never uses - the slop check flags them
    callback_times: Optional[str] = None       # the two times a qualified inbound reply offers
    referrals: Optional[str] = None            # who to point an out-of-scope caller to
    fb_app_id: Optional[str] = None                 # your Meta app - lets a pasted
    fb_app_secret: Optional[str] = None             # token become a lasting Page key


@app.post("/api/settings")
def save_settings(request: Request, body: Settings):
    require_auth(request)
    fields = body.model_dump(exclude_none=True)
    if not is_owner(request):
        # A guest may change their own business's settings, never the
        # account's (OWNER_ONLY_SETTINGS). Both UIs round-trip every Setup
        # field on save, so the blank `allowed_emails` a guest was shown, or
        # a value equal to what is stored, is left alone rather than refused -
        # otherwise a guest could never save their own sender name. Anything
        # that would actually change one is refused whole, nothing written:
        # a blank written through would empty the owner list, and an empty
        # list makes everyone an owner everywhere.
        for k in sorted(k for k in fields if k in OWNER_ONLY_SETTINGS):
            if fields[k].strip() in ("", setting(k, "")):
                del fields[k]
            else:
                raise HTTPException(403, f"{k} belongs to the account; only "
                                         "an owner can change it.")
    if "public_base_url" in fields:
        try:
            fields["public_base_url"] = clean_base_url(fields["public_base_url"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "imagegen_provider" in fields and fields["imagegen_provider"].strip() \
            and fields["imagegen_provider"].strip() not in imagegen.PROVIDERS:
        raise HTTPException(400, "Unknown image provider %r. One of: %s."
                            % (fields["imagegen_provider"], ", ".join(imagegen.PROVIDERS)))
    for k, v in fields.items():
        if k in ("google_key", "google_key_own", "telnyx_api_key", "imap_password",
                 "imagegen_api_key", "fb_app_secret") and not v.strip():
            # /api/me never sends the key back, so the field renders blank and
            # a plain save would wipe it. Skipping empty protects that — but it
            # also made a bad key impossible to remove from inside the app.
            # Deliberate removal goes through /api/settings/clear_google_key.
            continue
        set_setting(k, v.strip())
    return {"ok": True}


class FindBody(BaseModel):
    query: str = Field("", max_length=200)
    city: str = ""
    pages: int = 2
    vertical: str = "generic"
    buyer: str = ""                 # a BUYER_PRESETS key: sets the query and the category


@app.get("/api/find/buyers")
def find_buyers(request: Request):
    require_auth(request)
    return {"buyers": BUYER_PRESETS.get(ws.current(), [])}


@app.post("/api/find")
def find(request: Request, body: FindBody):
    require_auth(request)
    key = google_key()
    if not key:
        raise HTTPException(400, "No Google Maps key yet — add one in Setup.")
    if remaining_requests() <= 0:
        raise HTTPException(429, cap_message())
    city = body.city or setting("default_city", "San Antonio, TX")
    preset = buyer_preset(body.buyer)
    query = (body.query or "").strip() or (preset["query"] if preset else "")
    if len(query) < 2:
        raise HTTPException(400, "Say what kind of business to look for.")
    places = places_search(f"{query} in {city}", key, body.pages)
    added, skipped, _ = insert_places(places, city, body.vertical, preset["category"] if preset else "")
    return {"found": len(places), "added": added, "skipped": skipped, "query": query}


class FindMetroBody(BaseModel):
    query: str = Field("", max_length=200)
    metro: str = ""
    zips: str = ""
    vertical: str = "generic"
    pages: int = 2
    restart: bool = False
    buyer: str = ""


@app.post("/api/find_metro")
def find_metro(request: Request, body: FindMetroBody):
    require_auth(request)
    key = google_key()
    if not key:
        raise HTTPException(400, "No Google Maps key yet — add one in Setup.")
    if remaining_requests() <= 0:
        raise HTTPException(429, cap_message())

    metros = load_metros()
    if body.zips.strip():
        zips = [z.strip() for z in re.split(r"[,\s]+", body.zips) if z.strip()]
        area_key, city_label, state_code = "zips", "custom ZIPs", "TX"
    elif body.metro:
        mk = body.metro.lower()
        if mk not in metros:
            raise HTTPException(400, f"Unknown area '{body.metro}'.")
        m = metros[mk]
        zips = list(m.get("zips", []))
        area_key, city_label = mk, m.get("label", body.metro)
        state_code = m.get("state", "TX")
    else:
        raise HTTPException(400, "Pick an area or paste some ZIP codes.")

    if not zips:
        raise HTTPException(400, "That area has no ZIP codes listed.")

    preset = buyer_preset(body.buyer)
    q = (body.query or "").strip() or (preset["query"] if preset else "")
    if len(q) < 2:
        raise HTTPException(400, "Say what kind of business to look for.")
    category = preset["category"] if preset else ""
    label = f"{q.lower()}|{area_key}"

    with closing(db()) as c:
        if body.restart:
            c.execute("DELETE FROM sweep_zips WHERE label=?", (label,))
            c.commit()
        done = {r["zip"] for r in c.execute(
            "SELECT zip FROM sweep_zips WHERE label=?", (label,)).fetchall()}

    todo = [z for z in zips if z not in done]
    if not todo:
        raise HTTPException(400, "Every ZIP in this area is already done. "
                                 "Use 'start over' to sweep it again.")

    sweep_id = uuid.uuid4().hex
    with closing(db()) as c:
        c.execute("""INSERT INTO sweeps
            (id, label, query, area, vertical, total, started_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (sweep_id, label, q, city_label, body.vertical, len(todo), now(), now()))
        c.commit()

    pages = max(1, min(body.pages, 3))
    t = threading.Thread(
        target=ws.carry(run_sweep, sweep_id, label, q, state_code, todo,
                        city_label, body.vertical, pages, key, category),
        daemon=True)
    t.start()

    return {"sweep_id": sweep_id, "total": len(todo),
            "already_done": len(done), "zips": len(zips), "area": city_label,
            "remaining_requests": remaining_requests()}


@app.get("/api/sweep/{sid}")
def sweep_status(request: Request, sid: str):
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT * FROM sweeps WHERE id=?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "no such sweep")
    return dict(r)


class AddBody(BaseModel):
    text: str
    vertical: str = "generic"
    city: str = ""


@app.post("/api/add")
def add_manual(request: Request, body: AddBody):
    """Paste anything: bare domains, or 'Name, domain, phone' per line."""
    require_auth(request)
    added, new_ids = 0, []
    with closing(db()) as c:
        for line in body.text.splitlines():
            line = line.strip().strip(",")
            if not line:
                continue
            parts = [x.strip() for x in re.split(r"[,\t]", line)]
            company = dom = phone = ""
            for part in parts:
                if not part:
                    continue
                if re.search(r"\d{3}[^\d]*\d{3}[^\d]*\d{4}", part) and not phone:
                    phone = part
                elif re.search(r"\.[a-z]{2,}", part, re.I) and not dom:
                    dom = domain_of(part)
                elif not company:
                    company = part
            if not dom and len(parts) == 1:
                dom = domain_of(parts[0])
            if not dom:
                continue
            if is_suppressed(dom):
                continue
            # Already on file under another source (a Google find, an audit,
            # a caller)? One business, one card - a second copy means two
            # calls on Today, two drafts, and at three copies the chain
            # check retires the real one as a "corporate chain".
            if c.execute("SELECT 1 FROM prospects WHERE domain=? AND domain<>''", (dom,)).fetchone():
                continue
            if phone and find_prospect_by_phone(c, telnyx.to_e164(phone) or phone):
                continue
            try:
                cur = c.execute("""INSERT INTO prospects
                    (place_id, company, domain, phone, city, vertical, added_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (f"manual:{dom}", company or dom, dom, phone,
                     body.city or setting("default_city", ""), body.vertical, now()))
                c.execute("UPDATE prospects SET next_due=date('now'), next_action='call', "
                          "step=1 WHERE id=?", (cur.lastrowid,))
                added += 1
                new_ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                pass
        c.commit()
    queue_scans(new_ids)
    return {"added": added}


# When we have nothing to say about the site — the scan was blocked, or the
# site is genuinely clean — open with the business question instead. Never
# bluff a finding: an owner spots it instantly and the call is over.
PIVOT_OPENER = {
    "restaurant":    "how are folks ordering from you these days — phone, your own "
                     "site, or the delivery apps?",
    "appointment":   "how do people book with you — do they call, message you, or is "
                     "there an app?",
    "construction":  "when a job comes in, how does it get from that call to somebody "
                     "actually scheduled?",
    "multilocation": "how do you see what's happening across your locations — do you "
                     "have to call each one?",
    "generic":       "walk me through what happens when a new customer contacts you.",
}


def fallback_line(vertical: str) -> str:
    return PIVOT_OPENER.get(vertical or "generic", PIVOT_OPENER["generic"])


def row_to_dict(r):
    d = dict(r)
    try:
        d["findings"] = json.loads(d.get("findings") or "[]")
    except Exception:
        d["findings"] = []
    # what to actually say: human research first, scanner second
    if ws.current() == "homerepair":
        d["line"] = (d.get("say") or "").strip() or HR_SAY.get(homerepair_segment(d), HR_SAY["property_manager"])
    else:
        d["line"] = ((d.get("say") or "").strip()
                     or (d.get("opener") or "").strip()
                     or fallback_line(d.get("vertical")))
    # Be explicit when we couldn't read the site, so nothing gets bluffed.
    d["site_unknown"] = d.get("scan_state") in ("failed", "no_site")
    return d


@app.get("/api/prospects")
def list_prospects(request: Request, status: str = "", q: str = "", limit: int = 300):
    require_auth(request)
    # emails_sent / last_emailed come from what actually went out, so the
    # "Emailed" chip is true whatever status the lead is in now.
    sql = ("SELECT prospects.*, "
           "(SELECT COUNT(*) FROM outreach o WHERE o.prospect_id=prospects.id AND o.state='sent') AS emails_sent, "
           "(SELECT MAX(o.sent_at) FROM outreach o WHERE o.prospect_id=prospects.id AND o.state='sent') AS last_emailed "
           "FROM prospects")
    args, where = [], []
    if status == "emailed":
        where.append("EXISTS (SELECT 1 FROM outreach o WHERE o.prospect_id=prospects.id AND o.state='sent')")
    elif status and status != "all":
        where.append("status = ?"); args.append(status)
    if q:
        where.append("(company LIKE ? OR domain LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    # worst sites first, unscanned last, but anything with a human-written
    # angle floats up regardless of score
    if status == "emailed":
        sql += " ORDER BY last_emailed DESC LIMIT ?"
        args.append(limit)
    else:
        sql += """ ORDER BY
        CASE WHEN say <> '' THEN 0 ELSE 1 END,
        CASE WHEN scan_state='no_site' THEN 0 WHEN scan_state='failed' THEN 1 ELSE 2 END,
        COALESCE(score, 999) ASC, id DESC LIMIT ?"""
        args.append(limit)
    with closing(db()) as c:
        rows = [row_to_dict(r) for r in c.execute(sql, args).fetchall()]
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM prospects GROUP BY status").fetchall()}
        counts["all"] = sum(counts.values())
        counts["emailed"] = c.execute(
            "SELECT COUNT(DISTINCT prospect_id) FROM outreach WHERE state='sent'").fetchone()[0]
        pending = c.execute(
            "SELECT COUNT(*) n FROM prospects WHERE scan_state IN ('pending','scanning')"
        ).fetchone()["n"]
    return {"prospects": rows, "counts": counts, "scanning": pending}


class StatusBody(BaseModel):
    status: str
    note: str = ""


@app.post("/api/prospect/{pid}/status")
def set_status(request: Request, pid: int, body: StatusBody):
    require_auth(request)
    if body.status not in ("new", "called", "booked", "dead", "emailed"):
        raise HTTPException(400, "unknown status")
    # The Prospects tab's Called / Booked / Pass used to write one column and
    # nothing else: no touch, no queue move, the draft still in the Outbox,
    # the Pipeline unchanged - so Today asked for the same call again.
    # They are the same actions as Today's buttons, so they take the same path.
    outcome = {"called": "no_answer", "booked": "booked", "dead": "dead",
               "emailed": "emailed"}.get(body.status)
    with closing(db()) as c:
        row = c.execute("SELECT id, domain, next_action FROM prospects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        if body.note:
            c.execute("UPDATE prospects SET note=? WHERE id=?", (body.note, pid))
        if outcome:
            kind = "email" if outcome == "emailed" else "call"
            c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at) VALUES (?,?,?,?,?)",
                      (pid, kind, outcome, body.note, now()))
            if outcome == "emailed":
                mark_waiting_draft_sent(c, pid)
            summary = advance_queue(c, pid, outcome, None)
        else:
            c.execute("UPDATE prospects SET status=?, touched_at=? WHERE id=?",
                      (body.status, now(), pid))
            summary = body.status
        c.commit()
        dom = row["domain"]
    # "dead" means never contact again — honor it in the shared suppression list
    if body.status == "dead" and dom:
        try:
            suppress(dom)
        except Exception:
            pass
    return {"ok": True, "summary": summary}


@app.post("/api/prospect/{pid}/rescan")
def rescan(request: Request, pid: int):
    require_auth(request)
    with closing(db()) as c:
        c.execute("UPDATE prospects SET scan_state='pending' WHERE id=?", (pid,))
        c.commit()
    # Through queue_scans, never a bare POOL.submit: the worker thread does not
    # inherit this request's workspace, so an unwrapped scan_one would read the
    # default and rescan the same id in The Watson Factor's file instead.
    queue_scans([pid])
    return {"ok": True}


# ───────────────────── outbound email ─────────────────────
# The old draft here opened with "I built a tool that checks local business
# websites and yours came through it" and then listed schema-markup defects.
# No owner has ever cared. The workbook proved the better hook: quote the
# thing their own customers already wrote about them.
#
# Nothing here sends. Every draft waits for a person.

# offer -> (subject tail, what it is costing them, what you would do)
# ── whose product line is this ──────────────────────────────────────────
# Everything below - the offers, the evidence rules that pick them, the trade
# phrasings - is The Watson Factor's. HomeRepair Tech sells something entirely
# different (a free Home Health Score into a maintenance plan) to entirely
# different buyers (property managers, landlords, custom builders), so none of
# this copy is even approximately right for it.
#
# Rather than let a HomeRepair draft quietly inherit Watson's pitch, a
# workspace with no catalog refuses to draft. We already shipped one email
# that quoted a restaurant's service complaint and then pitched DoorDash
# commissions; pitching a property manager on an ordering app would be the
# same bug wearing a different hat.
CATALOG_WORKSPACES = {"watson", "homerepair"}


# ── a customer's own pitch, as data ──────────────────────────────────────
# The two built-in businesses keep their copy in the dicts below, because it
# was written and argued over line by line and there is no reason to move it.
# Everyone onboarded after them writes theirs into a settings row instead, so
# a new customer needs a form rather than a developer.
#
# The shape is one entry per offer:
#   {"offers": {"After-Hours Line": {
#        "tail":     "the calls coming in after you close",
#        "cost":     "what it costs them, in their words",
#        "fix":      "what you would do about it",
#        "evidence": "voicemail|after.?hours|never answers",   (optional regex)
#        "question": "When somebody calls %s after seven, where does it go?",
#        "impact":   ["the short line", "the setup line"]}}}
#
# `evidence` is what lets pick_pitch choose this offer from a real finding
# rather than a guess - same rule the built-ins follow: the sentence you quote
# decides the thing you sell.
CATALOG_SETTING = "offer_catalog"


def parse_catalog(raw: str) -> dict:
    """Read a stored catalog. Returns {} for anything unusable.

    Never raises. A broken catalog must degrade to "this workspace has no
    offers yet", which the draft gate already refuses on - not to a traceback
    in the middle of composing somebody's email.
    """
    try:
        d = json.loads(raw or "{}")
    except Exception:
        return {}
    offers = d.get("offers") if isinstance(d, dict) else None
    if not isinstance(offers, dict):
        return {}
    clean = {}
    for name, o in offers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(o, dict):
            continue
        tail, cost, fix = (str(o.get(k) or "").strip() for k in ("tail", "cost", "fix"))
        if not (tail and cost and fix):
            continue          # a half-written offer would ship a half-written email
        entry = {"tail": tail, "cost": cost, "fix": fix}
        rx = str(o.get("evidence") or "").strip()
        if rx:
            try:
                re.compile(rx)
                entry["evidence"] = rx
            except re.error:
                pass          # a bad regex is dropped, the offer still works
        q = str(o.get("question") or "").strip()
        if q and q.count("%s") == 1:
            entry["question"] = q
        imp = o.get("impact")
        if isinstance(imp, list) and len(imp) == 2 and all(isinstance(x, str) for x in imp):
            entry["impact"] = [imp[0].strip(), imp[1].strip()]
        clean[name.strip()] = entry
    return {"offers": clean}


def catalog() -> dict:
    """This workspace's offer catalog, from settings.

    The two built-ins keep a default copy in code, but what is SAVED wins
    everywhere - so the owner can edit, add and delete his own offers from
    Setup like any customer can, instead of being told his product needs a
    developer. {} means "use the code copy" (built-ins) or "nothing yet"."""
    try:
        return parse_catalog(setting(CATALOG_SETTING, ""))
    except Exception:
        return {}


def offer_copy_map() -> dict:
    cat = catalog().get("offers")
    if not cat:
        return OFFER_COPY
    return {k: (v["tail"], v["cost"], v["fix"]) for k, v in cat.items()}


def question_map() -> dict:
    cat = catalog().get("offers")
    if not cat:
        return QUESTION_BY_OFFER
    return {k: v["question"] for k, v in cat.items() if v.get("question")}


def impact_map() -> dict:
    cat = catalog().get("offers")
    if not cat:
        return IMPACT_BY_OFFER
    return {k: tuple(v["impact"]) for k, v in cat.items() if v.get("impact")}


def evidence_rules() -> list:
    """(regex, offer) pairs for whichever workspace is being worked."""
    cat = catalog().get("offers")
    if not cat:
        return EVIDENCE
    return [(v["evidence"], k) for k, v in cat.items() if v.get("evidence")]


def has_catalog(slug=None) -> bool:
    """Does this workspace have offer copy of its own?

    The two built-ins have theirs in code. A customer workspace earns one by
    writing offers into its own settings - which is what keeps the refusal
    below honest as the product grows: a new customer still cannot
    accidentally inherit somebody else's pitch, but they are no longer
    permanently locked out waiting for a developer.
    """
    slug = slug or ws.current()
    if slug in CATALOG_WORKSPACES:
        return True
    try:
        return bool(parse_catalog(setting(CATALOG_SETTING, "")).get("offers"))
    except Exception:
        return False


def catalog_ready(action="draft outreach"):
    if has_catalog():
        return
    w = ws.info()
    raise HTTPException(409,
        "%s has no offer catalog yet, so Just Grit can't %s for it. It would "
        "have to borrow %s's pitch - which sells AI Vision and ordering apps - "
        "to %s. Write %s's offers first."
        % (w["name"], action, ws.WORKSPACES[ws.PRIMARY]["name"], w["buyer"], w["short"]))


OFFER_COPY = {
    # AI Vision is one product with five different arguments. Sending a car
    # wash the sentence about a table going too long without a check-back is
    # how you prove you never looked at their business.
    "AI Vision": (
        "something in your reviews",
        "Every one of those is a customer who won't come back, and a review the next one reads.",
        "If there's a camera pointed at it, it can raise a hand while it's happening "
        "instead of after the review shows up."),
    "Ordering App": (
        "what the delivery apps are taking",
        "On a $40 order the apps keep roughly $12, and they own the customer, not you.",
        "I build you your own ordering page - your menu, your prices, your customer list - "
        "so the repeat orders stop paying a commission."),
    "Website": (
        "you're hard to find on Google",
        "When someone searches your trade here and nothing of yours comes up, that job goes to "
        "whoever does show up.",
        "I build a straight, fast page that says what you do and where, with your number at the "
        "top so it can be tapped."),
    "Reviews & Rewards": (
        "your rating",
        "Most people won't call a business under 4.2 stars. The happy customers just never get asked.",
        "I set up a system that asks every happy customer at the right moment, so the "
        "average climbs on its own."),
    "AI Receptionist": (
        "the calls you're missing",
        "A missed call from a customer with a problem right now is a job that goes to the next "
        "number on the list.",
        "I put something on your line that answers every time, takes the details, and texts "
        "them to you - so nothing sits in a voicemail box."),
}
# "Website" is two different problems wearing one name, and they need
# different sentences. OFFER_COPY's version above is the findability case -
# nobody can find you. But most site-scan findings are the opposite situation:
# they DID find you, and then the page lost them. Telling a Pilates studio
# whose only flaw is an untappable phone number that they're "hard to find on
# Google" asserts a second problem the finding is no evidence for - which is
# the same mismatch one level down from the one pick_pitch just stopped making.
WEBSITE_COPY_ONSITE = (
    "something on your site is costing you calls",
    "They already found you - that's the expensive part - and then the page made it "
    "easy to give up and try the next place.",
    "I build a straight, fast page that says what you do and where, with your number "
    "at the top so it can be tapped.")

DEFAULT_COPY = ("one thing I noticed",
                "Small things like that quietly cost you customers.",
                "I build software for local businesses here and this is a short fix.")

# Same product, said in the language of the trade. Keyed on `vertical`.
AI_VISION_BY_TRADE = {
    "restaurant": (
        "Every one of those is a table that tipped less and a review that scared off the next guest.",
        "A camera over the floor can catch that by itself - a manager gets a ping when a table has gone "
        "too long without a check-back, during the shift instead of after the review."),
    "auto": (
        "One damage claim you can't disprove costs more than the wash did, and the review lasts "
        "a lot longer.",
        "Wherever there's footage, I make it searchable in plain English - "
        "'show me that silver truck at 4pm' - so a claim takes two minutes to settle instead "
        "of your afternoon."),
    "appointment": (
        "Lobby wait is the number one thing people complain about, and they write it up before "
        "they've even left.",
        "A camera on the lobby can flag when the room is backing up or "
        "the front desk is unmanned, so somebody fixes it while the person is still waiting."),
    "contractor": (
        "Every one of those is a job that went to whoever answered instead.",
        "A camera on the yard or the shop can tell you when "
        "something needs you - a delivery arriving, a gate open after hours - "
        "instead of you finding out later."),
}


# ── paint the picture ────────────────────────────────────────────────────
# Daniel, looking at "S&S Tire & Auto - the calls you're missing": "we're
# missing the target. It should be: I see people can't call you from your
# website, and if they do call and you're busy, we have an AI assistant that
# takes down all their info and texts it to you. Here are some things we
# could help you with. Paint a picture. Be creative."
#
# The old AI Receptionist copy stated a principle ("a missed call is a job
# that goes to the next number"). An owner nods at a principle and deletes
# it. A scene he recognises from his own Saturday is what gets a reply. So
# the email is built from three pieces:
#   who   - the customer, in this trade, at the moment they need you;
#   site  - what happens when that customer meets the thing we found on the
#           site (a different sentence for each finding, because "there's no
#           number to tap" is false for a site whose problem is after-hours);
#   busy  - what happens when they do get through and you can't pick up.
# Everything is framed as "picture..." - a scene, not a claim about what
# happened at their shop last week, which we don't know.

# (who, place, busy) by trade. `place` is what the customer calls the next
# one on the list: shop, office, clinic, company, studio, place.
STORY_BY_CATEGORY = [
    (r"vet|animal|pet", (
        "Picture someone whose dog just got into something it shouldn't have. They're scared, "
        "they want to talk to a person right now, and they're looking you up on their phone.",
        "clinic",
        "And when they do get through while the front desk is checking in two people and the "
        "other line is ringing, it goes to voicemail. Scared pet owners don't leave voicemails - "
        "they call the next clinic.")),
    (r"dental|dentist|orthodont", (
        "Picture someone who just cracked a tooth on a Saturday. It hurts, they want in as soon "
        "as possible, and they're looking you up on their phone.",
        "office",
        "And when they do get through while the front desk is with a patient, it goes to "
        "voicemail. Someone in pain doesn't leave a voicemail and wait - they call the next office.")),
    (r"fitness|gym|yoga|pilates|martial|boxing|crossfit|dance|studio", (
        "Picture someone who finally decided - today - that they're going to start. They look "
        "you up on their phone to ask about a first class.",
        "studio",
        "And if they do call while you're in the middle of teaching a class, it rings out. That "
        "motivation lasts about as long as it takes to find the next studio.")),
    (r"barber|salon|hair|nail|lash|brow|spa", (
        "Picture someone who needs to look good for a wedding this Saturday and wants to get in "
        "this week. They look you up on their phone.",
        "place",
        "And if they call while you've got clippers in your hand and a customer in the chair, it "
        "rings out. They book with whoever answers.")),
    (r"urgent care|clinic|medical|optical|eye|optomet|pediatric|physical therapy", (
        "Picture a parent with a sick kid in the back seat, looking you up on their phone to see "
        "if you can take them today.",
        "clinic",
        "And when they do get through while the front desk is buried, it goes to voicemail. A "
        "parent with a sick kid isn't leaving a message - they drive to the next place.")),
]
STORY_BY_VERTICAL = {
    "auto": (
        "Picture somebody with a nail in their tire, pulled over in a parking lot off 1604, "
        "looking up shops on their phone.",
        "shop",
        "And the ones who do call while your guys are under a car? It rings out, and they've "
        "called the next shop before you see the missed call."),
    "restaurant": (
        "Picture 5:30 on a Friday. Somebody wants a table for eight, or trays for a party "
        "tomorrow, and they're looking you up on their phone.",
        "place",
        "And when they do get through in the dinner rush, the phone rings next to a line at the "
        "register and nobody can grab it. That party of eight eats somewhere else."),
    "chiro": (
        "Picture someone who threw their back out lifting a bag of mulch Saturday morning. They "
        "can barely sit in the car, they want in today, and they're looking you up on their phone.",
        "office",
        "And when they do get through while you're with a patient, it goes to voicemail - and the "
        "next office down the road picks up."),
    "appointment": (
        "Picture someone who needs to get in this week and is looking you up on their phone "
        "between errands.",
        "office",
        "And when they do get through while the front desk is with someone, it goes to voicemail. "
        "Most people won't leave one - they call the next office."),
    "contractor": (
        "Picture a homeowner in August with an AC that just quit, or water coming through the "
        "ceiling. They're looking you up on their phone, and they want someone today.",
        "company",
        "And when they do call while you're on a roof, it rings out. Whoever answers first gets "
        "that job - not the best company, the one that picked up."),
    "generic": (
        "Picture someone ready to spend money with you tonight, looking you up on their phone "
        "with one quick question.",
        "place",
        "And when someone does get your number and calls while you're with a customer, it goes "
        "to voicemail. Most people won't leave one - they just call the next place."),
}

# (finding regex, subject tail, what happens on the site, first half of the fix)
STORY_PROOF = [
    (r"no phone number", "there's no phone number on the homepage to tap. People can't call you "
                         "from your website."),
    (r"tap-?to-?call|tapping it does nothing", "your number is on there, but tapping it does "
                         "nothing. People can't call you from your website."),
    (r"after-?hours|request a quote|9 ?pm", "there's no way to ask for a quote unless someone's "
                         "there to answer the phone."),
    (r"only way to reach you|pick up the phone", "the only way to reach you is to call - no form, "
                         "no text, nothing for someone who can't talk right now."),
]

STORY_BY_FINDING = [
    (r"no phone number",
     "people can't call you from your website",
     "They land on your site, look for a number to tap, and there isn't one. They hit back "
     "and tap the next %s.",
     "put your number at the top of your site as a big button they can tap"),
    (r"tap-?to-?call|tapping it does nothing",
     "people can't call you from your website",
     "They land on your site and tap your number, and nothing happens. Nobody copies it down "
     "and dials by hand. They hit back and tap the next %s.",
     "make the number on your site a button that actually dials"),
    (r"after-?hours|request a quote|9 ?pm",
     "the customers who find you after you close",
     "Say it's after hours. They're on your site, ready to ask for a quote, and there's no way "
     "to until you open. By morning the next %s has already written back.",
     "add a quick quote request to your site that texts you the second someone fills it out"),
    (r"only way to reach you|pick up the phone",
     "the customers who can't call right now",
     "They find your site, but the only way to reach you is to call, and right now they can't "
     "talk or you're closed. No way to leave a message, so they move on to a %s that makes it easy.",
     "add a simple 'text us' / request form so the people who can't call right now can still "
     "reach you"),
]

RECEPTIONIST_FIX = (
    "Here's what I'd do. First, %s. Second, an AI assistant on your line: when you can't pick up "
    "it answers, nights and weekends too, gets their name, number and what they need, and texts "
    "it to you within seconds. You call back when your hands are free.")


def receptionist_story(d: dict):
    """(subject tail, picture, fix, proof) for an AI Receptionist email, or None
    when the evidence isn't one of the phone/site findings the scene is
    written for (then the plainer copy stands)."""
    opener = (d.get("opener") or "").strip()
    found = next((f for f in STORY_BY_FINDING if opener and re.search(f[0], opener, re.I)), None)
    if not found:
        return None
    cat = (d.get("category") or "").lower()
    who = next((s for rx, s in STORY_BY_CATEGORY if cat and re.search(rx, cat)), None)
    who = who or STORY_BY_VERTICAL.get(d.get("vertical") or "", STORY_BY_VERTICAL["generic"])
    setup, place, busy = who
    _, tail, site_line, site_fix = found
    picture = "%s %s\n\n%s" % (setup, site_line % place, busy)
    seen = next((t for rx, t in STORY_PROOF if re.search(rx, opener, re.I)), "")
    proof = ("I pulled up %s the way a customer on a phone would, and %s"
             % (d.get("domain") or "your site", seen)) if seen else ""
    return tail, picture, RECEPTIONIST_FIX % site_fix, proof


# "These are some of the things we could help you out with." One line each,
# said the way the owner would say it, and only the ones that fit the trade:
# nobody at a dental office wants a camera pitch, and only restaurants pay
# the delivery apps.
HELP_MENU = {
    "Reviews & Rewards": "More 5-star Google reviews - it asks every happy customer at the right "
                         "moment, so your rating climbs on its own.",
    "Website": "A fast site that shows up when people around here search for what you do.",
    "Ordering App": "Your own online ordering, so you stop handing the delivery apps a third of "
                    "every order.",
    "AI Receptionist": "An AI assistant that answers the calls you can't and texts you the details.",
    "AI Vision": {
        "auto": "Camera footage you can search in plain English - 'silver truck, 4pm' - so a "
                "damage claim takes two minutes to settle.",
        "restaurant": "A camera over the floor that pings a manager when a table has waited too "
                      "long - during the shift, not after the review.",
        "contractor": "A camera on the yard or shop that tells you when something needs you - a "
                      "delivery, a gate left open after hours.",
    },
}
HELP_ORDER = ["Reviews & Rewards", "Ordering App", "AI Vision", "Website", "AI Receptionist"]


# The same menu as one short phrase each, for the sentence a first email
# carries. The playbook's slop test (2026-09-25) flagged the old three-bullet
# list on every Watson Factor email: three neat bullets are a generation
# tell, and they pushed a first touch to 190+ words. One sentence, two
# items, no bullets.
HELP_SHORT = {
    "Reviews & Rewards": "more 5-star Google reviews",
    "Website": "a fast site that shows up in local search",
    "Ordering App": "your own online ordering",
    "AI Receptionist": "an AI assistant that answers the calls you can't",
    "AI Vision": {"auto": "camera footage you can search in plain English",
                  "restaurant": "a camera that pings a manager when a table has waited too long",
                  "contractor": "a camera on the yard that tells you when something needs you"},
}


def help_menu(d: dict, offer: str, skip=(), n: int = 2) -> str:
    """One sentence on the other things we do - never a list - or '' outside
    the primary workspace (a customer's catalog writes its own emails)."""
    if ws.current() != ws.PRIMARY:
        return ""
    sold = set(offer_copy_map())
    vert = d.get("vertical") or ""
    items = []
    for o in HELP_ORDER:
        if o == offer or o in skip or o not in sold:
            continue
        line = HELP_SHORT.get(o)
        if isinstance(line, dict):
            line = line.get(vert)
        if o == "Ordering App" and vert != "restaurant":
            line = None
        if line:
            items.append(line)
        if len(items) >= n:
            break
    if not items:
        return ""
    return "I also do %s, if that's closer to what you need." % " and ".join(items)


# ── who is actually going to read this ───────────────────────────────────
# Half the addresses in the list are shared inboxes. That is not automatically
# a mistake - for a lot of small businesses info@ IS the owner - but it is a
# different reader, and writing to it as though it were the owner personally
# is how an email gets deleted by whoever opens the mail. The rules below
# prefer a real person when we know one, and when we don't, say out loud that
# we know we're writing to a shared inbox.
ROLE_INBOXES = {"info", "contact", "hello", "office", "admin", "support",
                "sales", "mail", "team", "inquiries", "inquiry", "leads",
                "enquiries", "help", "service", "frontdesk", "front desk",
                "reception", "general", "booking", "bookings", "orders",
                "getinfo", "noreply", "no-reply"}


def is_role_inbox(addr: str) -> bool:
    local = (addr or "").split("@")[0].strip().lower()
    local = re.sub(r"[._-]", "", local)
    return local in {re.sub(r"[._-]", "", r) for r in ROLE_INBOXES}


def dead_addresses(c) -> set:
    """Every address in this workspace the mail server has refused."""
    try:
        return {r[0] for r in c.execute("SELECT email FROM dead_addresses") if r[0]}
    except Exception:
        return set()          # table predates this build; nothing is dead yet


def mark_dead_address(c, email: str, pid=None, reason: str = "bounce"):
    addr = (email or "").strip().lower()
    if not addr:
        return
    c.execute("INSERT INTO dead_addresses (email, prospect_id, reason, at) "
              "VALUES (?,?,?,?) ON CONFLICT(email) DO NOTHING",
              (addr, pid, reason, now()))


def best_recipient(contacts, prospect, dead=None):
    """The most human address we have for this business.

    This used to be `contacts[0]["email"] or prospect["email"]`, copied into
    four places - which meant a named owner sitting at position two lost to
    whatever generic address happened to be first. A person who can reply
    beats a shared inbox every time, so a named contact with an address wins,
    then any contact address, then the business's own.

    `dead` is the set of addresses that have bounced. Passing it is what makes
    a bounce move to the next person at the same business instead of ending
    the conversation - the address failed, not the lead.

    Returns (address, name_of_that_person).
    """
    usable = trig.usable_addresses(contacts, (prospect or {}).get("email") or "", dead)
    return usable[0] if usable else ("", "")


# ── is this even a business we can name? ─────────────────────────────────
# Thirteen prospects carry a scraped page title where the company name should
# be. Five are literally called "Home". An email whose subject line reads
# "Home - something on your site is costing you calls" is deleted on sight,
# and it tells the reader exactly how it was made.
JUNK_NAMES = {"home", "index", "welcome", "book online", "order online", "contact",
              "contact us", "about", "about us", "menu", "untitled", "new page",
              "homepage", "main", "default",
              # What a dead or parked site serves instead of a name. Worth
              # keeping the lead - a suspended site is a reason to call, not a
              # reason to drop them - but never worth putting in a subject line.
              "account suspended", "coming soon", "under construction",
              "site not found", "default page", "index of", "website disabled",
              "domain for sale", "page not found", "403 forbidden", "404 not found"}


def _name_stem(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_company(raw: str, domain: str = "") -> str:
    """The business's name, out from behind the page title it was scraped in.

    Page titles put the brand on either side of the separator depending on who
    built the site - "Best Vet Hospital In San Antonio, TX | Kirby Family Vet"
    hides it at the end, "Moore Chiropractic San Antonio - Sports Injury |
    Neck & Back Pain" leads with it. Picking a side by convention gets one of
    those two wrong, and the first version of this renamed Moore Chiropractic
    to "Neck & Back Pain".

    So when we know their domain, that decides it: the brand is the piece of
    the title that looks like the address they registered. Without a domain,
    fall back to leading-brand order, which is the commoner shape.
    """
    s = html.unescape(raw or "").strip()
    s = re.sub(r"\s+", " ", s)
    parts = [p.strip() for p in re.split(r"\s*\|\s*", s) if p.strip()]
    if not parts:
        return ""

    def before_tagline(t):
        for sep in (" \u2013 ", " \u2014 ", " - "):
            if sep in t:
                return t.split(sep)[0].strip()
        return t

    stem = _name_stem((domain or "").split(".")[0])
    if stem:
        best, score = "", 0
        for p in parts:
            for cand in (p, before_tagline(p)):
                cs = _name_stem(cand)
                if not cs:
                    continue
                hit = len(cs) if cs in stem else (len(stem) if stem in cs else 0)
                if hit > score:
                    best, score = cand, hit
        if best:
            return before_tagline(best).strip(" -\u2013\u2014|")
    return before_tagline(parts[0]).strip(" -\u2013\u2014|")


# When the scraped title is useless but the domain isn't. Greedy longest-match
# against words that actually appear in local business names, so
# "stampschiropractic.com" comes back as "Stamps Chiropractic" instead of
# being thrown away. Anything that doesn't split cleanly is left alone rather
# than guessed at - a half-parsed name in a subject line is worse than none.
_DOMAIN_WORDS = [
    "chiropractic", "chiropractor", "chiro", "orthodontics", "orthodontic",
    "physicaltherapy", "rehabilitation", "rehab", "wellness", "dentistry",
    "dentist", "dental", "medical", "clinic", "health", "spine", "joint",
    "family", "sports", "injury", "care", "center", "centre", "group",
    "barbershop", "barber", "salon", "spa", "fitness", "gym", "crossfit",
    "pilates", "yoga", "studio", "veterinary", "animal", "hospital", "vision",
    "eyecare", "optical", "restaurant", "cafe", "kitchen", "grill", "bbq",
    "pizza", "taco", "bakery", "brewing", "bar", "carwash", "wash", "auto",
    "detail", "detailing", "tire", "collision", "roofing", "plumbing", "hvac",
    "electric", "landscaping", "construction", "remodeling", "law", "realty",
    "dermatology", "physical", "therapy", "express", "heights", "orthopedic", "urgent", "urban",
    "city", "north", "south", "east", "west", "hill", "hills", "oaks", "park",
    "mission", "american", "texas", "alamo", "san", "antonio", "braunfels",
    "first", "premier", "advanced", "complete", "total", "pure", "elite",
    "the", "and", "of", "for", "my", "dr", "doctor",
]
_DOMAIN_WORDS.sort(key=len, reverse=True)
_KEEP_CAPS = {"bbq", "hvac"}


def _parse_known(stem: str):
    """Split a run of letters into known words, or give up."""
    words, i = [], 0
    while i < len(stem):
        for w in _DOMAIN_WORDS:
            if stem.startswith(w, i):
                words.append(w)
                i += len(w)
                break
        else:
            return None
    return words


def name_from_domain(domain: str) -> str:
    """"stampschiropractic.com" -> "Stamps Chiropractic".

    Most local domains are one proper noun followed by what the business does.
    The proper noun is never going to be in a word list - that is the whole
    point of a name - so one unknown chunk at the front is allowed as long as
    everything after it parses cleanly. Two unknowns and we stop: a
    half-guessed name in front of a prospect is worse than no name.
    """
    stem = re.sub(r"^www\.", "", (domain or "").strip().lower()).split("/")[0]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = re.sub(r"[^a-z0-9]", "", stem)
    if not (4 <= len(stem) <= 40):
        return ""

    def render(ws):
        out = []
        for w in ws:
            if w in _KEEP_CAPS or len(w) <= 2:
                out.append(w.upper())
            else:
                out.append(w.capitalize())
        return " ".join(out)

    whole = _parse_known(stem)
    if whole and len(whole) >= 2:
        return render(whole)
    for cut in range(2, min(15, len(stem) - 2)):
        rest = _parse_known(stem[cut:])
        if rest:
            return render([stem[:cut]] + rest)
    return ""


def usable_company(raw: str, domain: str = "") -> bool:
    """Would putting this in a subject line embarrass us?"""
    s = display_company(raw, domain)
    if len(s) < 3 or len(s) > 60:
        return False
    if s.lower() in JUNK_NAMES:
        return False
    if "/" in s and s.count("/") >= 2:      # "A / B / C group" - a list, not a name
        return False
    return True


def display_company(raw: str, domain: str = "") -> str:
    """The name to actually put in front of a prospect.

    The scraped title first; if that turns out to be "Home" or "Account
    Suspended", fall back to whatever the domain spells out.
    """
    s = clean_company(raw, domain)
    if len(s) < 3 or len(s) > 60 or s.lower() in JUNK_NAMES \
            or (s.count("/") >= 2):
        return name_from_domain(domain)
    return s


def _first_name(pid_contacts, prospect):
    """The first name of the best contact - only when it is clearly a
    person's name. "Hi Corporate," went out once; the gate is in research."""
    for c in pid_contacts:
        nm = rsrch.clean_person_name((c.get("name") or "").strip())
        if nm:
            return nm.split()[0]
    return ""


# What each kind of evidence is evidence OF. Order matters: the first match
# wins, so the most specific signals sit at the top.
EVIDENCE = [
    (r"no website|invisible on google|website is down|no site|directory (listing|only)", "Website"),
    (r"unanswered|voicemail|not answered|never call|missed call|doesn.t answer|no one answers",
     "AI Receptionist"),
    (r"doordash|ubereats|uber eats|grubhub|favor|commission|3rd-party|third-party|storefront",
     "Ordering App"),
    (r"understaff|check-?back|checks dropped|wait|slow|inattentive|ignored|rude|"
     r"never return|hard to find|damage|scratch|dust|scuff", "AI Vision"),
    (r"\brating\b|\bstars?\b|reputation|low-rated", "Reviews & Rewards"),
]


# The complaint column is research shorthand. Some of it is the customer's own
# words, and some of it is a note to self - "(confirm on visit)", "verify",
# "(assumed)". The first kind can be quoted back to an owner. The second kind
# absolutely cannot: "I was reading your reviews and they say '(confirm on
# visit) lobby wait'" tells a prospect they are a row in someone's list.
# Be surgical. The first version of this listed bare "check" as an internal
# marker, which killed "checks dropped without a check-back" - the single best
# customer sentence in the whole workbook. Match the shape of an annotation
# (parenthesised, or a leading hedge), never a common English word on its own.
_INTERNAL = re.compile(
    r"\((?:\s*)(confirm|verify|assumed|unconfirmed|tbd|guess|check)"     # (confirm on visit)
    r"|^(confirm|verify|assumed|unconfirmed|tbd)\b"                      # confirm: ...
    r"|\b(to be confirmed|not confirmed|unconfirmed|assumed|tbd)\b", re.I)


def quotable(complaint: str) -> bool:
    c = (complaint or "").strip()
    return bool(c) and len(c) > 12 and not _INTERNAL.search(c)


def clean_quote(complaint: str) -> str:
    """Their words, without our filing system around them."""
    c = re.sub(r"^(BBB|Angi|Yelp|Google|Reviews?|Isolated|Note)[^:]{0,26}:\s*",
               "", complaint.strip())
    c = c.replace("\u2018", "'").replace("\u2019", "'")
    # These summaries join several reviews with semicolons and wrap each in
    # quote marks. Nested inside our own quotation they read as a copy-paste
    # accident. Drop the delimiters, keep the contractions: an apostrophe
    # between two letters is part of a word, anywhere else it is punctuation
    # we inherited.
    c = re.sub(r"(?<![A-Za-z])['\"]|['\"](?![A-Za-z])", "", c)
    c = re.sub(r"\s+", " ", c).strip(" ;,").rstrip(". ")
    return c


def offer_from(text):
    for rx, offer in evidence_rules():
        if text and re.search(rx, text, re.I):
            return offer
    return ""


# What a WEBSITE SCAN finding is evidence of. Deliberately a separate table
# from EVIDENCE, which reads a customer's own words: "no way to book online"
# found by the scanner is a fact about their site, while the same sentence in
# a review is a customer complaining about their night. Keeping the two apart
# means tuning one never quietly moves the other - and EVIDENCE's step-1 path
# is the one that actually works today, so it does not get touched.
SITE_FINDING = [
    (r"book online|booking|catering|big party|reserve|reservation|order online", "Ordering App"),
    # "people can't call you from your website, and if they do call and you're
    # busy..." - Daniel. A number that won't dial is the same story as no
    # number at all, and the receptionist email's fix covers the site too.
    (r"no phone number|tap-?to-?call|tapping it does nothing|only way to reach you|"
     r"pick up the phone|after-?hours|request a quote",
     "AI Receptionist"),
    (r"google reviews (don'?t|do not) show", "Reviews & Rewards"),
]


def offer_from_site_finding(opener: str) -> str:
    """A site-scan finding is evidence about their website before it is
    evidence about anything else.

    Anything the table above doesn't point somewhere more specific is a
    Website pitch, and that is honest rather than a fallback: the thing we
    would actually sell someone whose homepage has no tappable phone number
    is a better homepage. Website's own fix line already says exactly that.
    """
    for rx, offer in SITE_FINDING:
        if opener and re.search(rx, opener, re.I):
            return offer
    return "Website"


# A qualification GATE writes here too, and "not verified" is its own status
# marker meaning nobody has checked afford/need yet - not a customer's words
# about anything. pick_pitch() used to treat that marker as "a real complaint
# we just can't quote," which meant an owner with a sharp, specific finding
# already sitting in `say`/`opener` (a dollar figure, a missing phone number)
# got the same generic filler sentence as someone we know nothing about at
# all. Caught on Auto Dude Mobile Detailing, where the real opener - a $2,000
# damage-claim line - was sitting one field over from "not verified" and
# never got used.
PLACEHOLDER_COMPLAINTS = {"not verified", "unverified", "n/a", "na", "none",
                          "tbd", "pending", "-"}


def pick_pitch(d):
    """Choose the offer and the proof line together, from the SAME evidence.

    They used to be chosen separately, and it produced an email that quoted
    Blanco Cafe's "inattentive servers; checks dropped without a check-back"
    and then pitched them on DoorDash commissions. An owner reads two halves
    that don't connect and correctly concludes nobody actually looked. So the
    sentence we quote decides the thing we sell - never one without the other.

    Returns (offer, proof_sentence).
    """
    # The CLEANED name, same as compose_email puts in the subject. The
    # company column is a raw scraped <title> for a good chunk of rows
    # ("Best Vet Hospital In San Antonio, TX | Kirby Family Vet"), and
    # usable_company() gates on the cleaned version - so those rows sailed
    # through with "Kirby Family Vet" in the subject and the whole title,
    # pipe and all, in the proof sentence. That's the form-letter tell.
    company = (display_company(d.get("company") or "", d.get("domain") or "")
               or d.get("domain") or "your business")
    listed = [o.strip() for o in (d.get("offers") or "").split(",") if o.strip()]
    complaint = (d.get("complaint") or "").strip()
    if complaint.lower() in PLACEHOLDER_COMPLAINTS:
        complaint = ""          # a status marker, not evidence - fall through

    # 1. Their customers' own words. Strongest evidence there is.
    if complaint and quotable(complaint):
        c = clean_quote(complaint)
        offer = offer_from(c)
        if offer == "AI Vision" and "Ordering App" in listed:
            # A generic service complaint ("slow", "rude", "inattentive") isn't
            # camera-specific evidence once we already know, from their own
            # site, that they have no ordering system of their own - that's
            # the more specific sell for a restaurant, and it's meant to
            # outrank Vision (Website > Ordering App > AI Vision when more
            # than one qualifies - see qualify_one's `order`). Offer and proof
            # stay paired, same rule as everywhere else in this function:
            # never quote a review about slow service and then pitch an
            # ordering page - that mismatch is the exact bug pick_pitch exists
            # to prevent.
            return "Ordering App", (
                "I had a look at how %s takes orders online and didn't find one of your own "
                "on the site - it's either running through the delivery apps or not happening "
                "online at all." % company)
        if offer:
            return offer, ('I was reading the reviews for %s and more than one of them says the '
                           'same thing - "%s."' % (company, c))
        # A complaint we can't classify still beats anything we could assert -
        # but it does NOT license picking whatever happens to be first in
        # `offers` and pitching that underneath it. No offer means DEFAULT_COPY,
        # which says something true and general instead of naming a product the
        # quote is no evidence for.
        return "", (
            'I was reading the reviews for %s and a few of them mention "%s."' % (company, c))

    # 1b. Real complaint, but it carries our own annotations. Use it to choose
    #     the offer; say something true and general instead of quoting it.
    if complaint:
        # Classify it or fall through to the site scan below. The old `or
        # listed[0]` here was the same guess as above wearing a different hat.
        offer = offer_from(complaint)
        if offer == "AI Vision" and "Ordering App" in listed:
            offer = "Ordering App"   # same priority call as step 1; nothing quoted here to mismatch
        if offer:
            return offer, ("I've been going through how the businesses around here in %s handle "
                           "the busy hours, and %s came up."
                           % (d.get("city") or d.get("market") or "San Antonio", company))

    # 2. A fact about their setup, from the workbook's own web/ordering columns.
    for key, sentence in (
            ("web_status", "I went looking for %s online and there's no website of your own to "
                           "find - just a directory listing." % company),
            ("ordering",   "I had a look at how %s takes orders - it all runs through the "
                           "delivery apps." % company)):
        val = ""
        m = re.search(r"%s: ([^|]+)" % ("Web" if key == "web_status" else "Ordering"),
                      d.get("note") or "")
        if m:
            val = m.group(1)
        offer = offer_from(val)
        if offer:
            return offer, sentence

    # 3. The site scan's `opener` - written specifically to be spliced into a
    #    sentence like this one. NOT `say` / `d["line"]`: that field is
    #    research notes for the "why them" card and can hold things that must
    #    never reach a prospect - "Ask for Bobby," "hottest SA target," a
    #    competitor comparison. Sending that verbatim isn't a weak pitch, it's
    #    a leak. The card still shows `say` to Daniel; the email never sees it.
    #    This branch is where the function's own promise used to break. It
    #    returned `listed[0]` as the offer - whatever qualify_one happened to
    #    rank first, often AI Vision off a middling star rating - and paired it
    #    with a proof sentence from the website scanner. Offer and proof from
    #    unrelated evidence, by construction, on every send that reached here:
    #    twelve of the first forty-five. Studio 1604, a Pilates studio, got a
    #    subject line about their reviews, a body about their phone number not
    #    being tap-to-call, and a pitch about pointing a security camera at it.
    #    The finding decides the offer now, the same as everywhere else.
    opener = (d.get("opener") or "").strip()
    if opener:
        return offer_from_site_finding(opener), (
            "I had a look at %s and noticed %s." % (d.get("domain") or company, opener))

    # 4. Nothing found. Say so plainly and let DEFAULT_COPY carry it - naming a
    #    product here would be asserting evidence we do not have.
    return "", (
        "I came across %s while looking at local businesses here." % company)


# ═══════════════ HomeRepair Tech — ProActive Home Care ═══════════════
# The first email's job is one thing: get a yes to a free Portfolio Health
# Score on three of their properties. That offer costs nothing, so none of the
# portfolio pricing has to be settled before this can go out.

# From the D2D playbook's "What You Cannot Say". These are not tone rules —
# an email promising a repair outcome or quoting a price is a compliance
# problem, and ProActive is a maintenance concierge, not a licensed inspector
# or contractor. compose_homerepair() is checked against this before it ships.
BANNED_CLAIMS = [
    (r"\bwe(?:'ll| will)\s+(?:fix|repair|replace|install)\b",
     "promises a repair outcome — concierge, not contractor"),
    (r"\b(?:cost|run|around|about)\s*\$\s*\d",
     "quotes a repair cost before a visit"),
    (r"\b(?:covered by|through your)\s+insurance\b",
     "implies insurance coverage"),
    (r"\bguarantee(?:d|s)?\s+(?:to\s+)?(?:fix|repair|save|score)\b",
     "guarantees an outcome"),
    (r"\bdiagnos(?:e|is|ed)\b", "claims a diagnosis"),
    (r"\bfree\s+(?:repairs?|maintenance|service)\b",
     "calls the service free — only the Score is free"),
]


def compliance_problems(text: str):
    """What in this draft would fail the playbook's own rules."""
    return [why for pat, why in BANNED_CLAIMS
            if re.search(pat, text, re.I)]


COMMERCIAL_CATS = ("commercial", "commercial_manager", "commercial_building", "office", "office_building",
                   "retail", "shopping_center", "medical_office", "warehouse", "industrial")


# What HomeRepair sells each kind of buyer, in the order to pitch it.
HR_OFFERS = {
    "property_manager": ["Snap It Send It", "Preventive Maintenance", "Move-In/Move-Out Reports",
                         "Quarterly Health & Safety Audit"],
    "hoa":              ["Common-Area Care", "Quarterly Health & Safety Audit"],
    "commercial":       ["Preventive Maintenance", "Snap It Send It"],
    "realtor":          ["Repair Estimate", "HomePassport Report"],
}

# "Say this" on a HomeRepair card. The website-scan line under it belongs to
# the Watson Factor pitch ("no retargeting pixel") and means nothing to a
# property manager.
HR_SAY = {
    "property_manager": "Ask who handles maintenance calls. \"When a tenant has a leak, how does it get to you? "
                        "With us the tenant snaps a photo and sends it, you tap approve, and we send a tech, "
                        "inside the dollar limit you set.\"",
    "hoa":              "Ask for the board president or the manager. \"Is next year's common-area budget set yet? "
                        "I'd like to walk the property and give the board a written scope and price.\"",
    "commercial":       "\"When a tenant calls about the AC or a leak, who do you send? We walk the building on a "
                        "schedule and fix the small stuff before it turns into a call.\"",
    "realtor":          "\"When the inspection report comes in, how do you get repair numbers inside three days? "
                        "We turn photos into a written estimate the same day.\"",
}


# The leave-behind's headline for HomeRepair: we didn't audit their website,
# so the Watson line "we looked at your site the way a customer would" is wrong.
HR_SHEET_INTRO = {
    "property_manager": lambda p: {"title": "Less maintenance work for %s" % (p.get("company") or "your team"),
                                   "sub": "What HomeRepair.tech does for property managers around %s, and what it "
                                          "looks like day to day." % ((p.get("city") or "San Antonio").split(",")[0].title())},
    "hoa":              lambda p: {"title": "Common-area care for %s" % (p.get("company") or "your community"),
                                   "sub": "What we do for HOA and condo boards, and what the board sees each month."},
    "commercial":       lambda p: {"title": "Fewer tenant calls for %s" % (p.get("company") or "your building"),
                                   "sub": "What we do for commercial buildings, and what it looks like."},
    "realtor":          lambda p: {"title": "Repair numbers inside the option period",
                                   "sub": "What HomeRepair.tech does for agents, and what your clients get."},
}


def homerepair_segment(d) -> str:
    """Which of the three sequences this prospect gets. An HOA BOARD is a
    different buyer from an HOA MANAGEMENT COMPANY: the board copy says
    "your board" and "budget season"; sending it to RealManage, who IS the
    vendor, reads as nonsense. Only boards get Sequence A. Commercial
    buildings and their managers get Sequence C."""
    cat = (d.get("category") or "").lower().strip()
    if cat in ("hoa_board", "hoa", "condo", "board"):
        return "hoa"
    if cat in COMMERCIAL_CATS:
        return "commercial"
    if cat in ("realtor", "real_estate", "brokerage", "real_estate_agent", "real estate agency", "real estate agent"):
        return "realtor"
    return "property_manager"


def compose_homerepair(d, contacts):
    """Step 1 of the ProActive sequence, from the approved copy in sequences.py.

    This used to be a one-off email written here. It is now the first touch of
    a real 3-step sequence, so the Outbox and the follow-ups say the same
    things - and the copy lives in one file that the test suite scans for
    signal leaks rather than being embedded in the composer.
    """
    company = d.get("company") or d.get("domain") or "there"
    city = (d.get("city") or "").split(",")[0].strip()
    if city and city.islower():
        city = city.title()          # imported lowercase; "around new braunfels" reads careless

    # Segment picks the sequence. Everything on this list today is a management
    # company; HOA boards get the budget-season copy when that list exists.
    # An HOA BOARD is a different buyer from an HOA MANAGEMENT COMPANY. The
    # board copy says "your board" and "budget season"; sending it to RealManage,
    # who IS the vendor, reads as nonsense. Only boards get Sequence A.
    seg = homerepair_segment(d)

    # The signal chooses the angle and never appears in the email. There is no
    # review classification stored yet, so this is the spec's own fallback -
    # and it is a real default, not a pretend one.
    signal = (d.get("signal") or "") or None

    ctx = {"first_name": _first_name(contacts, d) or "",
           "company": company, "community": company, "city": city,
           "realtor_link": setting("realtor_page_url", "").strip(),
           "pm_link": pm_page_url()}
    out = seq.render(seg, signal, ctx, setting,
                     unsubscribe_url=setting("unsubscribe_url", ""))
    first = out["emails"][0]
    return {"offer": "ProActive Home Care",
            "variant": "seq:%s:%s" % (seg, out["angle_used"] or "board"),
            "subject": first["subject_options"][0],
            "body": first["body_text"],
            "step": 1,
            "address_missing": any("mailing address" in p
                                   for p in out["compliance_problems"])}


PM_PAGE_DEFAULT = "https://homerepair.tech/pm/"


def pm_page_url() -> str:
    """The property-manager page the PM emails point to. Blank in Setup
    means no link; unset means the live page."""
    v = setting("pm_page_url", None)
    return PM_PAGE_DEFAULT if v is None else v.strip()


def compose_homerepair_followup(d, contacts, seg: str, signal, step: int):
    """Step 2, 3 or 4 of the same sequence compose_homerepair started.

    Same rendering path as step 1 - same copy file, same footer, same guard -
    just picking a later email out of the three `seq.render()` already
    produces instead of always taking the first.
    """
    company = d.get("company") or d.get("domain") or "there"
    city = (d.get("city") or "").split(",")[0].strip()
    if city and city.islower():
        city = city.title()
    ctx = {"first_name": _first_name(contacts, d) or "",
           "company": company, "community": company, "city": city,
           "realtor_link": setting("realtor_page_url", "").strip(),
           "pm_link": pm_page_url()}
    out = seq.render(seg, signal, ctx, setting,
                     unsubscribe_url=setting("unsubscribe_url", ""))
    idx = step - 1
    if idx < 0 or idx >= len(out["emails"]):
        return None
    email = out["emails"][idx]
    return {"offer": "ProActive Home Care",
            "variant": "seq:%s:%s" % (seg, out["angle_used"] or "board"),
            "subject": email["subject_options"][0],
            "body": email["body_text"],
            "step": step,
            "address_missing": any("mailing address" in p
                                   for p in out["compliance_problems"])}


def homerepair_advance(c, pid: int, seg: str, signal, completed_step: int):
    """Schedule the next step of a HomeRepair sequence, or close it out.

    Days are counted from the step before, not from day zero - the same rule
    Watson Factor's own SEQUENCE comment states, applied here so a step sent
    a little late doesn't throw off the gap to the one after it.
    """
    days = seq.step_days(seg, signal)
    steps = sorted(days)
    later = [s for s in steps if s > completed_step]
    if not later:
        # Four emails, no reply. The sequence said its piece.
        c.execute("UPDATE prospects SET status='cold', next_due=NULL, "
                  "next_action=NULL, touched_at=? WHERE id=?", (now(), pid))
        return
    nxt = min(later)
    gap = max(1, days[nxt] - days[completed_step])
    c.execute("UPDATE prospects SET status='working', step=?, "
              "next_action='email_followup', next_due=date('now', ?), "
              "seq_variant=?, touched_at=? WHERE id=?",
              (nxt, "+%d days" % gap, "%s:%s" % (seg, signal or ""), now(), pid))


def run_homerepair_followups(c, room: int):
    """Draft the next step for anyone due one, into the same Outbox as any
    other draft. This is the whole feature: a lead who went quiet after step
    1 doesn't just sit there until a human remembers to check - the copy
    that was already written and tested for exactly this gets queued for
    approval on its own.

    A real reply of any kind pulls a prospect out of this before it drafts
    anything - the automated cadence does not talk over a person who already
    answered. `next_action='email_followup'` is this mechanism's own marker,
    so clearing it here never touches Watson Factor's call/email cadence.
    """
    if room <= 0:
        return []
    made = []
    due = c.execute(
        """SELECT * FROM prospects
           WHERE next_action='email_followup' AND next_due IS NOT NULL
             AND date(next_due) <= date('now')
             AND status NOT IN ('booked','dead','cold','conversation','corporate')
             AND COALESCE(seq_variant,'') <> ''
           ORDER BY date(next_due) ASC
           LIMIT ?""", (room,)).fetchall()
    dead = dead_addresses(c)
    for prow in due:
        pid = prow["id"]
        if c.execute("SELECT 1 FROM outreach WHERE prospect_id=? "
                     "AND state IN ('draft','approved')", (pid,)).fetchone():
            continue    # already holding an undecided draft - never double it up
        replied = c.execute(
            "SELECT 1 FROM outreach WHERE prospect_id=? AND COALESCE(reply,'')<>'' "
            "LIMIT 1", (pid,)).fetchone()
        if replied:
            c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
                      "WHERE id=?", (pid,))
            continue
        raw = prow["seq_variant"] or ""
        seg, sig = raw.split(":", 1) if raw else (None, None)
        if not seg:
            continue
        step = int(prow["step"] or 2)
        d = row_to_dict(prow)
        contacts = contact_rows(c, pid)
        to, _to_name = best_recipient(contacts, d, dead)
        if not to:
            continue
        e = compose_homerepair_followup(d, contacts, seg, sig or None, step)
        if not e:
            continue
        e["body"] = voiced(e["body"])
        cur = c.execute(
            "INSERT INTO outreach (prospect_id, offer, to_addr, subject, body,"
            " subject_original, body_original, state, created_at, variant, step)"
            " VALUES (?,?,?,?,?,?,?, 'draft', ?, ?, ?)",
            (pid, e["offer"], to, e["subject"], e["body"],
             e["subject"], e["body"], now(), e["variant"], e["step"]))
        # Drafted, not yet sent - the schedule clears until a human decides.
        # homerepair_advance() re-arms it for the step after this one once
        # this draft actually goes out.
        c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
                  "WHERE id=?", (pid,))
        made.append(cur.lastrowid)
    return made


# ═══════════════ Watson Factor — follow-ups ═══════════════
# Forty-five cold emails went out of here and every one of them was step 1,
# because the draft query excluded anyone who had already been written to.
# One touch, ever, by construction - while the playbook this system was built
# from says in as many words: "Most people never reply to email #1. Send 2-3
# short follow-ups, 3-4 days apart, in the same thread," and calls that the
# place most replies come from. HomeRepair got a sequence engine; Watson never
# did. This is Watson's.
#
# Two things it deliberately does NOT do. It does not re-pitch - the second
# and third touch are short check-ins that stand on their own, so a follow-up
# still works when the first email landed badly (and by our own audit, a lot
# of them did). And it does not invent state: whether a follow-up is due is
# derived from the outreach table itself - last send, its step, whether
# anything came back - so there is no counter to drift out of sync with what
# was actually sent.
# How many prospects sharing one domain stops looking like a small business
# with a few locations and starts looking like a franchise chain.
CHAIN_DOMAIN_MIN = 3

WATSON_FOLLOWUP_GAPS = {2: 3, 3: 7}      # step -> days since the previous send
MAX_WATSON_EMAIL_STEP = 3                 # first email + two follow-ups, then stop


def _followup_subject(last_subject: str) -> str:
    """Same thread, as far as a subject line can carry one.

    The Outbox approves a draft by handing it to a `mailto:` link, and a
    mailto ALWAYS opens a brand-new compose window - there is no reply, no
    thread, and no original quoted underneath. This subject said "Re:" while
    the body said "did this land with you?", which only reads as a sentence
    if the first email is sitting below it. It never was.

    The "Re:" is kept, because there genuinely was a previous email and the
    body now quotes it. What changed is that the body no longer *depends* on
    a thread this system cannot produce.
    """
    s = (last_subject or "").strip()
    return s if s.lower().startswith("re:") else ("Re: " + s if s else "Following up")


def _strip_signature(body: str, sig: str) -> str:
    """The first email's text without its sign-off and legal footer.

    Quoting the whole thing would put the mailing address and the opt-out
    line in the message twice, which looks like a machine wrote it - which
    is the one impression the entire pitch is trying not to make.
    """
    b = (body or "").rstrip()
    head = (sig or "").split("\n")[0].strip()
    for cut in ([sig] if sig else []) + ([head] if head else []):
        i = b.find(cut)
        if i > 0:
            return b[:i].rstrip()
    return b


def _when_sent(last_sent: str) -> str:
    """"on August 23" - or nothing, rather than a guess at the date."""
    try:
        dt = datetime.fromisoformat((last_sent or "")[:19])
    except Exception:
        return ""
    return "on %s %d" % (dt.strftime("%B"), dt.day)


# Values in `complaint` that are notes to ourselves rather than customer
# words. Twenty-nine of the forty-five rows due a follow-up say literally
# "not verified"; others read "(confirm on visit) upscale table service" or
# "category-risk only (busy vet, front-desk phone)". Quoting any of those back
# to an owner as something their reviewers said would be a fabrication, and an
# embarrassing one. The substring test below would already catch them - none
# appears in a sent email - but a rule this important gets stated outright
# rather than left to hold by accident.
_NOT_A_COMPLAINT = re.compile(
    r"^\s*(not[ -]verified|none|n/?a|unknown|tbd|pending)\s*$"
    r"|^\s*\(?\s*confirm\b"
    r"|category[- ]risk", re.I)

# "Isolated: employee standing around" - the stored value carries an analyst's
# label that the sent email stripped before quoting. The words after the colon
# are the customer's; the label is ours.
_COMPLAINT_LABEL = re.compile(r"^[A-Za-z][A-Za-z /&-]{0,24}:\s*")


def followup_recap(d, last_body: str) -> str:
    """One clause naming what the first email actually said. Or nothing.

    The rule is the same one `pick_pitch` follows and for the same reason:
    what we claim has to come from the evidence, not from nearby. Here that
    means the complaint is only repeated if it can be shown to appear in the
    email we really sent. Reminding somebody of an argument they were never
    made is worse than reminding them of nothing.
    """
    complaint = (d.get("complaint") or "").strip().rstrip(".")
    if not complaint or _NOT_A_COMPLAINT.search(complaint):
        return ""
    body = last_body or ""
    if complaint not in body:
        stripped = _COMPLAINT_LABEL.sub("", complaint).strip()
        if stripped and stripped != complaint and stripped in body:
            complaint = stripped
        else:
            return ""
    # Quoted verbatim, never paraphrased into a sentence. Two real rows show
    # why. Clipping at the semicolon turned "Understaffed; inattentive
    # servers; checks dropped without a check-back" into the bare word
    # "understaffed", and the stored complaint is often already a sentence
    # ABOUT the reviews - "Multiple reviews flag bad/slow service" - so any
    # framing like "more than one of them mentions ..." wraps a summary in a
    # summary and reads as nonsense. The first email quotes this string
    # exactly; so does this one.
    if len(complaint) > 160:
        complaint = complaint[:157].rsplit(" ", 1)[0] + "..."
    return '"%s."' % complaint


def compose_watson_followup(d, contacts, step: int, last_subject: str,
                            last_body: str = "", last_sent: str = ""):
    """Touch 2 or 3. Short, specific, and readable cold.

    The version this replaces said only "Short one - did this land with you?"
    - and "this" referred to an email the reader had, by definition, not
    responded to and most likely never opened. There was no antecedent. The
    docstring even stated the rule it was breaking: every line has to be true
    if the first email was ignored or never arrived.

    The root cause was upstream: this function was handed a subject line and
    nothing else, so it could not have restated the point even in principle.
    It now gets the email that was actually sent, so touch 2 can carry the
    same observation in one clause - and quote the original underneath, since
    the mailto path means nothing else ever will.
    """
    first = _first_name(contacts, d) or "there"
    company = (display_company(d.get("company") or "", d.get("domain") or "")
               or d.get("domain") or "")
    sig, has_addr = _signature()
    when = _when_sent(last_sent)
    recap = followup_recap(d, last_body)
    whose = ("%s's reviews" % company) if company else "your reviews"

    # Built first, because the copy is only allowed to promise the quote when
    # there actually is one. "It's quoted below" over nothing is the same
    # class of mistake as "did this land with you" over no thread.
    quoted = _strip_signature(last_body, sig)
    if len(quoted) > 1400:
        quoted = quoted[:1400].rsplit("\n", 1)[0] + "\n..."
    tail = ""
    if quoted:
        tail = ("\n%s I wrote:\n\n" % (("On %s" % when[3:]) if when else "Earlier")
                + "\n".join("> " + ln if ln.strip() else ">"
                             for ln in quoted.split("\n")) + "\n")
    below = " The original's below, in case it never landed." if quoted else ""

    # Touch 2 used to be a nudge about the same thing. Daniel: "if there is
    # not a response from the first product email we try to follow and also
    # offer different products, maybe send a one-sheeter." So touch 2 opens
    # a DIFFERENT door - the next-best offer for this business, in that
    # offer's own general words (cost + fix), never a quote it has no
    # evidence for - and touch 3 is the last note with a link to one page
    # on everything we do. The first email stays quoted underneath.
    second = second_offer(d, last_body)
    if step == 2 and second:
        name2, (tail2, cost2, fix2) = second
        body = ("Hi %s,\n\n"
                "I wrote %s about %s. Different idea this time, in case that one wasn't "
                "the itch - %s:\n\n"
                "%s %s\n\n"
                "If neither is something you're chasing right now, just say so and I'll "
                "leave it there.%s\n\n"
                % (first, when or "recently", whose if recap else "something I'd noticed",
                   tail2, cost2, fix2, below))
        return {"offer": name2, "variant": "followup:%d" % step,
                "subject": _followup_subject(last_subject),
                "body": apply_bans(body + sig + tail), "step": step,
                "address_missing": not has_addr}
    if step == 2:
        if recap:
            body = ("Hi %s,\n\n"
                    "I wrote %s about something in %s - %s\n\n"
                    "If that's not something you're chasing right now, just say so and "
                    "I'll leave it there.%s\n\n"
                    % (first, when or "recently", whose, recap, below))
        else:
            body = ("Hi %s,\n\n"
                    "Following up on a note I sent %s.%s\n\n"
                    "If it's not something you're thinking about right now, just say so "
                    "and I'll leave it there.\n\n"
                    % (first, when or "a little while back", below))
    else:
        # Says "last note" and means it: MAX_WATSON_EMAIL_STEP makes it the last.
        # Carries the one-sheet: one public page on everything we do, so a
        # reader who wasn't moved by either pitch can see the whole menu.
        sheet = one_sheet_url()
        lead = ("This was about a line that keeps coming up in %s - %s\n\n"
                % (whose, recap)) if recap else ""
        menu = ("Everything I do for businesses here is on my site, if that's easier "
                "than replying: %s\n\n" % sheet) if sheet else ""
        body = ("Hi %s,\n\n"
                "Last note from me. %s%s"
                "If the timing's wrong I'd rather know than keep turning up in your "
                "inbox - one word is plenty. My number's below if it's ever worth "
                "a look.\n\n" % (first, lead, menu))

    return {"offer": "", "variant": "followup:%d" % step,
            "subject": _followup_subject(last_subject),
            "body": apply_bans(body + sig + tail), "step": step,
            "address_missing": not has_addr}


def second_offer(d, last_body: str):
    """The next-best thing to pitch this business, after the first email's
    offer got no answer. Their qualified list first (what the research said
    fits), then the catalog in order; never the one already pitched. Returns
    (name, (tail, cost, fix)) or None when there is only one thing to sell."""
    copy = offer_copy_map()
    if len(copy) < 2:
        return None
    first_offer = ""
    for name, (tail, cost, fix) in copy.items():
        if fix and fix[:60] in (last_body or ""):
            first_offer = name
            break
    if not first_offer:
        try:
            first_offer = (pick_pitch(d) or ("", ""))[0]
        except Exception:
            first_offer = ""
    listed = [o.strip() for o in (d.get("offers") or "").split(",") if o.strip() and o.strip() in copy]
    # When the first email's offer can't be told from its text, the safe
    # second idea is the one that fits nearly any business - a phone that
    # gets answered, reviews, a site - not a product that needs cameras or a
    # kitchen.
    general = [o for o in ("AI Receptionist", "Reviews & Rewards", "Website") if o in copy]
    rest = [o for o in copy if o not in general]
    catalog_order = (general + rest) if not first_offer else list(copy)
    order = [o for o in listed if o != first_offer] + \
            [o for o in catalog_order if o != first_offer and o not in listed]
    for name in order:
        # AI Vision reads wrong for a trade with no cameras on the floor;
        # skip it unless the research listed it for them.
        if name == "AI Vision" and name not in listed and (d.get("vertical") or "") not in ("restaurant", "auto"):
            continue
        return name, copy[name]
    return None


def one_sheet_url() -> str:
    """The link an email carries for 'everything I do'. A prospect should land
    on the sender's own website (the Setup 'site' field), never on the app's
    own hostname. The built-in /welcome/offers page is only the fallback for a
    workspace with no site saved."""
    site = (setting("sender_site", "") or "").strip()
    if site:
        if not re.match(r"^https?://", site, re.I):
            site = "https://" + site
        return site.rstrip("/")
    base = public_base()
    return (base.rstrip("/") + "/welcome/offers") if base else ""


def run_watson_followups(c, room: int):
    """Draft the next touch for anyone owed one, into the same Outbox.

    Who is owed one is asked of the data rather than of a flag: the most
    recent thing we sent them, how long ago, and whether anything at all has
    come back since. A prospect who answered - or asked to be removed - is
    not in this set, because `reply` being non-empty on ANY of their rows
    takes them out. The automated cadence never talks over a person who
    already replied.

    A bounce is the one label that ends an ADDRESS rather than the sequence:
    retarget_after_bounce moves the lead to the next person at that business
    and leaves the bounced row as the record of who we tried. Only the most
    recent send bouncing stops the cadence; an older one to a dead address
    must not, or the replacement gets exactly one email and never a second.
    """
    if room <= 0:
        return []
    made = []
    rows = c.execute(
        """SELECT p.*, o.id AS last_oid, o.subject AS last_subject,
                  o.body AS last_body,
                  COALESCE(o.step,1) AS last_step, o.sent_at AS last_sent
             FROM prospects p
             JOIN outreach o ON o.id = (SELECT id FROM outreach
                                         WHERE prospect_id = p.id AND state='sent'
                                         ORDER BY sent_at DESC, id DESC LIMIT 1)
            WHERE p.status NOT IN ('booked','dead','cold','conversation','corporate')
              AND COALESCE(o.step,1) < ?
              AND COALESCE(o.reply,'') <> 'bounce'
              -- Same carve-out queue_drafts makes: a bounced send is not a
              -- reply from a person, so an old bounce row (kept after the
              -- retarget) must not block the address we moved on to.
              AND NOT EXISTS (SELECT 1 FROM outreach r
                               WHERE r.prospect_id = p.id
                                 AND (COALESCE(r.reply,'') NOT IN ('', 'bounce')
                                      OR COALESCE(r.reply_text,'') <> ''))
              AND NOT EXISTS (SELECT 1 FROM outreach w
                               WHERE w.prospect_id = p.id
                                 AND w.state IN ('draft','approved'))
              -- a follow-up step Daniel skipped stays skipped
              AND NOT EXISTS (SELECT 1 FROM outreach k
                               WHERE k.prospect_id = p.id AND k.state='skipped'
                                 AND COALESCE(k.step,1) = COALESCE(o.step,1) + 1)
            ORDER BY o.sent_at ASC""", (max_email_step(),)).fetchall()
    dead = dead_addresses(c)
    for prow in rows:
        if len(made) >= room:
            break
        nxt = int(prow["last_step"] or 1) + 1
        gap = followup_gaps().get(nxt)
        if gap is None:
            continue
        due = c.execute("SELECT julianday('now') - julianday(?) >= ?",
                        (prow["last_sent"], gap)).fetchone()
        if not due or not due[0]:
            continue
        d = row_to_dict(prow)
        pid = d["id"]
        contacts = contact_rows(c, pid)
        to, _to_name = best_recipient(contacts, d, dead)
        if not to:
            continue
        # Same bar as a first touch: a follow-up threaded under "Re: Book
        # Online - ..." advertises how it was made just as loudly.
        if not usable_company(d.get("company") or "", d.get("domain") or ""):
            continue
        try:
            if is_suppressed(d.get("domain") or ""):
                continue
        except Exception:
            pass
        e = compose_watson_followup(d, contacts, nxt, prow["last_subject"],
                                    prow["last_body"], prow["last_sent"])
        e["body"] = voiced(e["body"])
        cur = c.execute(
            "INSERT INTO outreach (prospect_id, offer, to_addr, subject, body,"
            " subject_original, body_original, state, created_at, variant, step)"
            " VALUES (?,?,?,?,?,?,?, 'draft', ?, ?, ?)",
            (pid, e["offer"], to, e["subject"], e["body"],
             e["subject"], e["body"], now(), e["variant"], e["step"]))
        made.append(cur.lastrowid)
    return made


# ── A/B harness ──────────────────────────────────────────────────────
# Five openers, five different theories about why a stranger writes back.
# They are NOT five wordings of one email - that only measures adjectives.
# Each takes the same evidence (offer, proof, cost, fix) and frames it
# differently, so whatever wins says something about people.
#
# The variant is stamped on the outreach row at draft time and never
# recomputed, because the scoreboard has to measure what was actually sent -
# not what the composer would write today.

# One question per offer, for the variant that asks instead of pitching.
# Each is answerable in a sentence by someone who has never heard of us.
QUESTION_BY_OFFER = {
    "AI Receptionist":   "When somebody calls %s after five, where does that call end up?",
    "AI Vision":         "When something goes sideways on the floor at %s, how do you usually find out - while it's happening, or after?",
    "Ordering App":      "How much of %s's order volume comes through the delivery apps now, versus straight from you?",
    "Website":           "When somebody new is looking for what %s does, how do they normally find you?",
    "Reviews & Rewards": "Does anybody at %s actually ask the happy customers for a review, or does it just happen on its own?",
}
DEFAULT_QUESTION = "How does %s handle that side of things right now?"

# What changes for them, said without asserting anything about their setup.
# This is the correction Daniel caught: "your cameras already see it" states a
# fact about a stranger's premises that we have no way of knowing, and a cold
# email that gets a fact wrong is worse than a generic one - it proves nobody
# looked. Everything here is conditional or about the world, never about them.
IMPACT_BY_OFFER = {
    "AI Receptionist":   ("a call that doesn't go to voicemail",
                          "A customer with a problem right now rings three numbers and hires whoever picks up."),
    "AI Vision":         ("finding out now instead of tomorrow",
                          "Most security footage only ever gets watched after something has already gone wrong - somebody pulls the tape to confirm what they already know."),
    "Ordering App":      ("orders that don't pay a commission",
                          "The repeat customer is worth the most and is the one the apps charge you for every single time."),
    "Website":           ("showing up when somebody searches",
                          "The job goes to whoever comes up, not to whoever is better at it."),
    "Reviews & Rewards": ("the rating going up on its own",
                          "Happy customers almost never leave a review unless something asks them at the right moment."),
}
DEFAULT_IMPACT = ("one thing worth fixing",
                  "Small things like this quietly cost a local business customers.")


def sender_brand() -> str:
    """The company line under the name in every email. It was a string
    literal - "The Watson Factor Development" - which meant a customer's
    workspace signed its outreach as Daniel's company. Settings first, then
    the workspace's own name; the literal survives only for the original
    workspace, whose sent mail already carries it."""
    typed = setting("sender_company", "").strip()
    if typed:
        return typed
    w = ws.me()
    if w.get("slug") == ws.PRIMARY:
        return "The Watson Factor Development"
    return w.get("name") or "Your business"


# ── the owner's voice on every draft ─────────────────────────────────────
def voice_profile() -> dict:
    return voice.load(setting("voice_profile", ""))


def voiced(body: str, first: str = "") -> str:
    """The owner's greeting form and sign-off on a freshly composed draft.
    A no-op until a profile has been learned."""
    p = voice_profile()
    if not p:
        return body
    return voice.apply(body, p, first, setting("sender_name", "").strip())



def _signature():
    """The sign-off, and whether it's legal to send. Built once so every
    variant carries the same footer - CAN-SPAM doesn't care which test cell a
    message came from."""
    name  = setting("sender_name", "Daniel Watson")
    phone = setting("sender_phone", "")
    site  = setting("sender_site", "")
    addr  = setting("sender_address", "").strip()
    return (("%s\n%s\n%s%s\n\n"
             "—\n%s\n"
             'Reply "stop" and I\'ll remove you permanently.\n'
             ) % (name, sender_brand(), phone, (" · " + site) if site else "",
                  addr or "[ADD YOUR MAILING ADDRESS IN SETTINGS - required by law]"),
            bool(addr))


def _v_finding(g):
    """Control - the email that was already being sent. Lead with what we
    found, then what it costs, then the fix."""
    return ("%s - %s" % (g["company"], g["tail"]),
            "%s,\n\n%s\n\n%s\n\n%s\n\n%s"
            "Worth fifteen minutes? I'm in New Braunfels and happy to show you. "
            "If it's not for you, say so and I'll leave you alone.\n\n"
            % (_greet(g), g["proof"], g["cost"], g["fix"], _menu(g)))


def _menu(g):
    return (g.get("menu") + "\n\n") if g.get("menu") else ""


def _greet(g):
    """"Hi Ann" with a name; "Hi Blanco Cafe team" without one - the same
    rule HomeRepair's emails use, after 49 "Hi there" emails to info@
    inboxes got zero replies. "Hi there" only when we know nothing."""
    if g["first"] and g["first"] != "there":
        return "Hi %s" % g["first"]
    short = seq.short_name(g.get("company") or "")
    return ("Hi %s team" % short) if short and short.lower() != "there" else "Hi there"


def _v_artifact(g):
    """Asks permission to send a file. The smallest ask available - a one-word
    reply - on the theory that the thirty-minute ask is what kills the rest."""
    return ("two-page check on %s" % (g["domain"] or g["company"]),
            "%s -\n\nI build a tool that checks a local business the way a customer would, "
            "starting with the website and the Google listing. I ran %s through it this "
            "morning.\n\n%s\n\n%s\n\n"
            "I'm not asking for a meeting, just whether you want the write-up. "
            'Reply "send it" and it\'s in your inbox in a minute.\n\n'
            % (_greet(g), g["company"], g["proof"], g["cost"]))


def _v_proof(g):
    """Names things actually shipped instead of describing capability. Every
    product here is live and public - nothing in this one can be checked and
    found empty."""
    return ("built this for a company here in town",
            "%s,\n\nI'm in New Braunfels and I build software for local businesses - "
            "HomeRepair.tech for contractors and property managers, BossBuilt for builders.\n\n"
            "%s\n\n%s\n\n"
            "If that's where %s is right now, I'd like fifteen minutes. If it isn't, ignore "
            "me and I won't write again.\n\n"
            % (_greet(g), g["proof"], g["fix"], g.get("company_short") or g["company"]))


def _v_question(g):
    """No pitch at all. People answer a question about their own operation that
    they'd never answer a pitch with, and a reply of any kind is the thing this
    pipeline is actually short of."""
    q = question_map().get(g["offer"], DEFAULT_QUESTION) % g["company"]
    return ("quick question about %s" % g["company"],
            "%s - one question, then I'll leave you alone.\n\n%s\n\n"
            "I build software for local businesses around San Antonio and New Braunfels, and "
            "I'm trying to work out whether that's a real problem worth solving or something "
            "everybody has already handled.\n\nWhat do you do about it?\n\n"
            % (_greet(g), q))


def _v_impact(g):
    """Leads with what changes for them, and asserts nothing about their
    business."""
    line, setup = impact_map().get(g["offer"], DEFAULT_IMPACT)
    return (line,
            "%s,\n\n%s\n\n%s\n\n%s"
            "That's what I build, and that's the whole pitch. If it's worth a look for %s, "
            "say so and I'll show you. If not, no hard feelings.\n\n"
            % (_greet(g), setup, g["fix"], _menu(g), g.get("company_short") or g["company"]))


VARIANTS = {"finding": _v_finding, "artifact": _v_artifact, "proof": _v_proof,
            "question": _v_question, "impact": _v_impact}
VARIANT_ORDER = ["finding", "artifact", "proof", "question", "impact"]


BANNED_SETTING = "banned_lines"
VARIANT_MIN_SAMPLE = 15      # sends per opener before the scoreboard gets a vote


def banned_lines() -> list:
    try:
        v = json.loads(setting(BANNED_SETTING, "") or "[]")
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    except ValueError:
        return []


# The ask, when the usual one has been banned. An email with no ask is a
# newsletter.
FALLBACK_ASK = ("If it's worth a look, reply and I'll send one page on it - and if it's "
                "not, tell me and I'll leave you alone.")


def apply_bans(body: str) -> str:
    """Drop the lines the owner has told the app to stop using. "Lines you
    keep cutting" on the Feedback tab used to be a chart; each now has a
    'Stop using this line' button, and this is where it takes effect. If
    the banned line was the ask, a plainer ask goes in its place."""
    bans = [b for b in banned_lines() if len(b) > 20]
    if not bans or not body:
        return body
    head, sep, tail = body.partition("\n\u2014\n")     # never touch the legal footer
    out, dropped_ask = [], False
    for para in head.split("\n\n"):
        p = " ".join(para.split())
        if any(b in p or p in b for b in bans):
            if "?" in p or "worth" in p.lower():
                dropped_ask = True
            continue
        out.append(para)
    if dropped_ask and FALLBACK_ASK not in out:
        # the signature is the last paragraph before the footer; the ask goes before it
        out.insert(max(1, len(out) - 1), FALLBACK_ASK)
    return "\n\n".join(out) + (sep + tail if sep else "")


class BanBody(BaseModel):
    line: str = Field(..., min_length=20, max_length=600)
    undo: bool = False


@app.post("/api/feedback/ban_line")
def ban_line(request: Request, body: BanBody):
    require_auth(request)
    line = " ".join(body.line.split()).lstrip("> ").strip()
    bans = banned_lines()
    if body.undo:
        bans = [b for b in bans if b != line]
    elif line not in bans:
        bans.append(line)
    set_setting(BANNED_SETTING, json.dumps(bans[:60]))
    with closing(db()) as c:
        cleared = resolve_covered_edits(c)
        c.commit()
    return {"banned": bans, "cleared": cleared}


@app.post("/api/feedback/review_edits")
def review_edits(request: Request):
    """'I've seen these': every open "noticed" edit goes to Applied in one go.
    They still feed "Lines you keep cutting" - that reads every edit, whatever
    its state - so nothing the app learns from is lost."""
    require_auth(request)
    with closing(db()) as c:
        n = c.execute("UPDATE feedback SET state='applied' WHERE state='open' AND kind='edit'").rowcount
        c.commit()
    return {"cleared": n}


def next_variant(c):
    """Least-sent-first until every opener has been tried enough to judge;
    then the one that gets replies most, 60% of the time, so the test keeps
    learning without going blind. "The system should learn from what's
    working and what's not."

    Straight round-robin by position skews the moment a batch is interrupted or
    a prospect is skipped for having no address. Counting what actually went out
    and picking the thinnest cell self-corrects.
    """
    counts = {v: 0 for v in VARIANT_ORDER}
    try:
        # A send that was rewritten before it left is not a send of that
        # variant's copy - the cell is still owed one.
        for v, n in c.execute("SELECT variant, COUNT(*) FROM outreach "
                              "WHERE state IN ('sent','approved','draft') "
                              "AND COALESCE(copy_check,'') <> 'edited' GROUP BY variant"):
            if v in counts:
                counts[v] = n
    except sqlite3.OperationalError:
        return VARIANT_ORDER[0]
    thinnest = min(VARIANT_ORDER, key=lambda v: (counts[v], VARIANT_ORDER.index(v)))
    if min(counts.values()) < VARIANT_MIN_SAMPLE:
        return thinnest
    # Every cell has enough sends to be judged. Replies per send, human
    # replies only (a bounce is a list problem, not an opener problem).
    rates = {}
    for v, sent, replied in c.execute(
            "SELECT variant, COUNT(*), SUM(CASE WHEN reply IN ('positive','negative') OR "
            "COALESCE(reply_text,'')<>'' THEN 1 ELSE 0 END) FROM outreach WHERE state='sent' "
            "AND COALESCE(copy_check,'')<>'edited' GROUP BY variant"):
        if v in counts and sent:
            rates[v] = (replied or 0) / sent
    if not rates or max(rates.values()) == 0:
        return thinnest                     # nobody has replied to anything yet
    best = max(rates, key=lambda v: (rates[v], -VARIANT_ORDER.index(v)))
    import random
    return best if random.random() < 0.6 else thinnest


def compose_email(d, contacts, variant=None, to_addr=""):
    if ws.current() == "homerepair":
        return compose_homerepair(d, contacts)
    offer, proof = pick_pitch(d)
    tail, cost, fix = offer_copy_map().get(offer, DEFAULT_COPY)
    # The subject is a promise about the body. "something in your reviews" over
    # a body that never mentions a review is the first thing an owner notices,
    # and it reads as exactly what it is - a form letter. Four of the first
    # forty-five went out that way.
    if "review" in tail.lower() and "review" not in (proof or "").lower():
        tail = "something I noticed"
    # A site-scan finding means they have a site and it is letting them down -
    # a different sentence from "nobody can find you". Keyed on the opener
    # actually appearing in the proof, so it can only fire on the branch that
    # really did come from the scanner.
    _op = (d.get("opener") or "").strip()
    if offer == "Website" and _op and _op in (proof or ""):
        tail, cost, fix = WEBSITE_COPY_ONSITE
    if offer == "AI Vision":
        trade = AI_VISION_BY_TRADE.get(d.get("vertical") or "")
        if trade:
            cost, fix = trade
    menu_skip = ()
    if offer == "AI Receptionist" and ws.current() == ws.PRIMARY:
        story = receptionist_story(d)
        if story:
            tail, cost, fix, story_proof = story
            # only swap the opening line when it really came from the site scan
            if story_proof and _op and _op in (proof or ""):
                proof = story_proof
            menu_skip = ("Website",)        # the site fix is already in the email
    first = _first_name(contacts, d)
    company = (display_company(d.get("company") or "", d.get("domain") or "")
               or d.get("domain") or "there")


    render = VARIANTS.get(variant) or VARIANTS[VARIANT_ORDER[0]]
    sig, has_addr = _signature()
    g = {"first": first or "there", "company": company, "company_short": seq.short_name(company) or company,
         "domain": d.get("domain") or "", "offer": offer,
         "tail": tail, "proof": proof, "cost": cost, "fix": fix, "menu": ""}
    subject, opening = render(g)
    # The "I also do..." sentence earns its place only in a short email. A
    # first touch reads best under 130 words (the playbook's slop test), and
    # a long finding plus a long fix has already used them.
    if slop.word_count(opening) <= 95:
        subject, opening = render(dict(g, menu=help_menu(d, offer, menu_skip)))
    # Writing to a shared inbox as though it were the owner personally is how
    # this gets deleted by whoever opens the mail. If we don't know a name and
    # the address is a front desk, say so and ask to be passed along - that is
    # a smaller, easier favour than the meeting, and it is the actual next
    # step for that reader.
    if to_addr and is_role_inbox(to_addr) and not first:
        opening += "Not your department? Point me to whoever's it is - that's just as useful.\n\n"
    return {"offer": offer, "variant": variant or VARIANT_ORDER[0],
            "subject": subject, "body": apply_bans(opening + sig),
            "address_missing": not has_addr}


@app.get("/api/prospect/{pid}/email")
def draft_email(request: Request, pid: int):
    """Compose without saving - the preview behind the card's Email button."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        d = row_to_dict(r)
        contacts = contact_rows(c, pid)
        dead = dead_addresses(c)
    out = compose_email(d, contacts)
    out["to"] = best_recipient(contacts, d, dead)[0]
    return out


# A cold mailbox is a bank account you can only overdraw once. The cap is a
# hard stop, not a reminder: the outbox refuses to write more drafts than you
# have sends left today, so the limit can't be blown past in a hurry.
DAILY_EMAIL_CAP_DEFAULT = 10


def daily_email_cap() -> int:
    try:
        return max(0, int(setting("daily_email_cap", str(DAILY_EMAIL_CAP_DEFAULT))))
    except (TypeError, ValueError):
        return DAILY_EMAIL_CAP_DEFAULT


def sent_today(c) -> int:
    return c.execute("SELECT COUNT(*) FROM outreach WHERE state='sent' "
                     "AND date(sent_at) = date('now')").fetchone()[0]


def waiting_now(c) -> int:
    """Drafts already written count against the day - otherwise you'd write 10,
    send none, and write 10 more."""
    return c.execute("SELECT COUNT(*) FROM outreach "
                     "WHERE state IN ('draft','approved')").fetchone()[0]


# ── send health: is the volume you're already sending actually clean ────────
# Warmbly's own ramp (start 10/day, +5/day, ceiling 50/day) and their bulk-
# sender bar ("quarantine acts at 10% spam placement") are the reference point
# here - see Deliverability_and_Warmbly_Review. Bounce rate is the visible
# proxy for spam placement a solo sender actually has: nobody sees the spam
# folder, everybody sees a bounce.
CAP_RAMP_INCREMENT = 5
CAP_RAMP_CEILING = 50
CAP_MIN_SAMPLE = 20     # fewer sends than this and a rate is noise, not a signal


def send_health(c, days: int = 30) -> dict:
    """Bounce / unsubscribe / reply rates over a rolling window - the numbers
    a cap decision should actually be made on, instead of a gut feeling."""
    row = c.execute(
        """SELECT COUNT(*) AS sent,
                  SUM(CASE WHEN reply='bounce' THEN 1 ELSE 0 END) AS bounced,
                  SUM(CASE WHEN reply='unsubscribe' THEN 1 ELSE 0 END) AS unsubscribed,
                  SUM(CASE WHEN reply IN ('positive','negative') THEN 1 ELSE 0 END) AS replied
           FROM outreach
           WHERE state='sent' AND date(sent_at) >= date('now', ?)""",
        ("-%d days" % days,)).fetchone()
    sent = row["sent"] or 0
    bounced = row["bounced"] or 0
    unsubscribed = row["unsubscribed"] or 0
    replied = row["replied"] or 0

    def rate(n):
        return round(100.0 * n / sent, 1) if sent else None

    return {"window_days": days, "sent": sent, "bounced": bounced,
            "unsubscribed": unsubscribed, "replied": replied,
            "bounce_rate": rate(bounced), "unsubscribe_rate": rate(unsubscribed),
            "reply_rate": rate(replied)}


def cap_recommendation(auth_state: str, health: dict, current_cap: int) -> dict:
    """What the numbers support for the daily cap - advisory only, this never
    changes the cap itself. A human still decides; this just replaces the
    guess with the actual bounce/unsubscribe rate.

    Bounce rate overrides everything else, including domain auth passing:
    a high bounce rate on a authenticated domain is still burning reputation,
    just for a different reason (bad addresses, not a DNS problem).
    """
    sent = health["sent"]
    bounce_rate = health["bounce_rate"]
    unsub_rate = health["unsubscribe_rate"]

    if sent < CAP_MIN_SAMPLE:
        return {"action": "hold", "reason":
                "Only %d sent in the last %d days - not enough to trust a change "
                "either way yet." % (sent, health["window_days"])}

    if bounce_rate is not None and bounce_rate >= 5:
        return {"action": "lower", "reason":
                "Bounce rate is %.1f%% over the last %d sends - high enough to be "
                "hurting your sender reputation. Worth finding out why (bad "
                "addresses? a list that's gone stale?) before sending more, not "
                "after." % (bounce_rate, sent)}

    if auth_state != "passing":
        return {"action": "hold", "reason":
                "Bounce and unsubscribe rates look fine, but domain authentication "
                "isn't passing yet. Fix SPF/DKIM/DMARC before raising volume - "
                "otherwise the extra volume just reaches spam folders faster."}

    if (bounce_rate is not None and bounce_rate < 2
            and (unsub_rate is None or unsub_rate < 2)
            and current_cap < CAP_RAMP_CEILING):
        new_cap = min(CAP_RAMP_CEILING, current_cap + CAP_RAMP_INCREMENT)
        return {"action": "raise", "suggested_cap": new_cap, "reason":
                "Bounce rate %.1f%% and unsubscribe rate %s over %d sends - clean "
                "enough to raise the daily cap from %d to %d."
                % (bounce_rate,
                   ("%.1f%%" % unsub_rate) if unsub_rate is not None else "0%",
                   sent, current_cap, new_cap)}

    return {"action": "hold", "reason":
            "Numbers are steady - no reason to change the cap right now."}


# Cached: a DNS answer doesn't change minute to minute, and the Outbox asks
# on every load.
_AUTH_CACHE: dict = {}
_AUTH_TTL = 900


@app.get("/api/deliverability")
def deliverability(request: Request, domain: str = "", refresh: int = 0):
    """Will mail from this domain actually authenticate?

    A send that 'succeeds' into a spam folder looks identical to one that
    lands, so this is the only way the app can tell you the difference.
    """
    require_auth(request)
    dom = (domain or "").strip().lower()
    if not dom:
        # Default to whatever the sender identity implies.
        site = setting("sender_site", "").strip()
        dom = re.sub(r"^https?://", "", site).split("/")[0].replace("www.", "")
    if not dom:
        raise HTTPException(400, "No sending domain set. Add one under Settings.")

    key = dom
    hit = _AUTH_CACHE.get(key)
    if hit and not refresh and (time.time() - hit[0]) < _AUTH_TTL:
        res, cached = hit[1], True
    else:
        try:
            from deliverability import check_domain_auth
            res = check_domain_auth(dom)
        except Exception as e:
            raise HTTPException(502, "Could not run the DNS check (%s)." % type(e).__name__)
        _AUTH_CACHE[key] = (time.time(), res)
        cached = False

    # The DNS answer is cached for 15 minutes because it barely changes; send
    # volume changes every day, so it's recomputed on every call regardless of
    # whether the DNS check above was a cache hit.
    with closing(db()) as c:
        health = send_health(c)
        cap = daily_email_cap()
    return {**res, "cached": cached, "health": health, "cap": cap,
            "cap_recommendation": cap_recommendation(res["state"], health, cap)}


# ── the spam test: what a receiving server actually sees ────────────────────
# DNS says whether the records EXIST. Only a real message through the real
# mail path says whether the signature verifies, the IP is clean and the copy
# scores. mail-tester.com accepts any address of the form
# test-<slug>@srv1.mail-tester.com and shows the result at /test-<slug>, so
# the app can mint one, hand it to Mail the same way it hands over every
# outreach draft, and link the report. The first time this ran by hand it
# found the domain at 10/10 - which was the answer to "is everything going
# to spam?" and worth having as a button rather than a memory.
SPAMTEST_DOMAIN = "srv1.mail-tester.com"
SPAMTEST_RESULTS = "https://www.mail-tester.com/"


def spamtest_slug() -> str:
    import secrets
    return "jg" + secrets.token_hex(4)


def spamtest_message() -> tuple:
    """A message shaped like the real outreach - same signature, same length
    band, no links - so the score reflects what prospects receive, not a
    'hello world' that would pass anywhere."""
    sig, _ = _signature()
    name = setting("sender_name", "").strip() or "there"
    subject = "Quick test from %s" % name
    body = ("Hi there,\n\n"
            "This is a test of the email address I send from, so I can see how a "
            "receiving server scores it before it goes to anyone real. Nothing to "
            "reply to.\n\n"
            "Thanks for reading this far.\n\n" + sig)
    return subject, body


@app.post("/api/deliverability/spamtest")
def spamtest_start(request: Request):
    """Mint a one-time test address and the mailto: that fills Mail with the
    test. The slug is kept so the results link survives a reload."""
    require_auth(request)
    from urllib.parse import quote
    slug = spamtest_slug()
    addr = "test-%s@%s" % (slug, SPAMTEST_DOMAIN)
    subject, body = spamtest_message()
    mailto = "mailto:%s?subject=%s&body=%s" % (addr, quote(subject), quote(body))
    set_setting("spamtest_slug", slug)
    set_setting("spamtest_at", now())
    return {"address": addr, "mailto": mailto, "subject": subject,
            "results": SPAMTEST_RESULTS + "test-" + slug, "started_at": now(),
            "note": ("Send it from the address you do outreach from - the report "
                     "is about that mailbox, not this app. Give it a minute, then "
                     "open the results link.")}


@app.get("/api/deliverability/spamtest")
def spamtest_last(request: Request):
    require_auth(request)
    slug = setting("spamtest_slug", "").strip()
    if not slug:
        return {"address": "", "results": "", "started_at": ""}
    return {"address": "test-%s@%s" % (slug, SPAMTEST_DOMAIN),
            "results": SPAMTEST_RESULTS + "test-" + slug,
            "started_at": setting("spamtest_at", "")}


@app.get("/api/setup_health")
def setup_health(request: Request):
    """Setup problems that affect everything, so they get fixed once rather
    than reported over and over on individual leads."""
    require_auth(request)
    issues = []
    k, src = google_key_source()
    shadowed = key_on_file() if src == "Settings" else ""
    if not k:
        issues.append({"key": "google", "level": "warn",
                       "message": "No Google Maps key set anywhere. Find (on the Prospects "
                                  "tab) and the area sweeps can't run without one. This key "
                                  "is account-wide — it serves both workspaces."})
    elif not looks_like_google_key(k):
        if shadowed:
            # The old wording sent you looking for a new key when a good one was
            # already sitting on disk. Naming the wrong fix costs more than
            # saying nothing.
            issues.append({"key": "google", "level": "bad",
                           "fix": "clear_google_key",
                           "message": "Settings holds something that isn't a Google API key "
                                      "(%d characters, doesn't start with AIza) — and it's "
                                      "covering up a valid key already on this Mac, in %s. "
                                      "Nothing needs replacing; the bad value needs removing. "
                                      "Saving the field empty won't do it (that's guarded so a "
                                      "blank form can't wipe your key), so use the button."
                                      % (len(k), shadowed)})
        else:
            issues.append({"key": "google", "level": "bad",
                           "message": "The Google key in %s isn't shaped like a Google API key "
                                      "(%d characters, doesn't start with AIza). Google keys are "
                                      "39 characters starting AIza. Find on the Prospects tab, "
                                      "the area sweeps and Look-them-up are all dead until it's "
                                      "replaced. This key is account-wide — it serves both "
                                      "workspaces, which is why you see this on each."
                                      % (src, len(k))})
    frm = setting("sender_email", "").strip().lower()
    site = re.sub(r"^https?://", "", setting("sender_site", "").strip().lower()).split("/")[0]
    if not frm:
        issues.append({"key": "sender_email", "level": "bad",
                       "message": "No sending address set. Every draft signs off as "
                                  "The Watson Factor, but 'open in mail' hands the message "
                                  "to whichever account your mail app defaults to - which "
                                  "is how a Watson Factor pitch goes out from a homerepair "
                                  "address. Set it under Settings."})
    elif site and not frm.endswith("@" + site):
        issues.append({"key": "sender_email", "level": "bad",
                       "message": "Sending address (%s) doesn't match the site in the "
                                  "signature (%s). Recipients see a maintenance company "
                                  "pitching software." % (frm, site)})
    if not setting("sender_address", "").strip():
        issues.append({"key": "address", "level": "bad",
                       "message": "No mailing address in Settings. CAN-SPAM requires one in "
                                  "every commercial email."})
    return {"issues": issues, "ok": not issues}


@app.get("/api/outreach/allowance")
def outreach_allowance(request: Request):
    require_auth(request)
    with closing(db()) as c:
        cap, sent, waiting = daily_email_cap(), sent_today(c), waiting_now(c)
    return {"cap": cap, "sent_today": sent, "waiting": waiting,
            "remaining": max(0, cap - sent - waiting)}


class DraftBody(BaseModel):
    ids: Optional[list] = None       # specific prospects; default = today's queue
    limit: int = 25


@app.post("/api/outreach/draft")
def queue_drafts(request: Request, body: DraftBody):
    """Fill the outbox from the queue. Skips anyone with no address, anyone
    already holding an undecided draft, and anyone on do-not-contact."""
    require_auth(request)
    catalog_ready("draft outreach")
    with closing(db()) as c:
        return draft_batch(c, ids=body.ids, limit=body.limit)


def draft_batch(c, ids=None, limit: int = 25, followups_only: bool = False) -> dict:
    """The drafting pass: follow-ups owed first, then new prospects up to
    today's room. The Outbox button and autopilot both come through here,
    so 'what gets written' is one rule however it is triggered."""
    made, skipped = [], {"no_email": 0, "already_queued": 0, "suppressed": 0,
                         "over_daily_cap": 0, "no_real_name": 0, "corporate_chain": 0}
    followups_drafted = []
    if True:
        cap = daily_email_cap()
        room = max(0, cap - sent_today(c) - waiting_now(c))
        if room <= 0:
            return {"ok": True, "drafted": 0, "skipped": skipped, "cap": cap,
                    "remaining": 0,
                    "note": "You're at today's limit of %d. Send or skip what's "
                            "already waiting first." % cap}
        # A silent lead who's already three days into a real conversation with
        # us is worth more than a cold name who's never heard from us - so the
        # sequence's own step 2/3 fill the day's room before new prospects do.
        if ws.current() == "homerepair":
            followups_drafted = run_homerepair_followups(c, room)
        else:
            followups_drafted = run_watson_followups(c, room)
        c.commit()
        room = max(0, cap - sent_today(c) - waiting_now(c))
        if room <= 0 or followups_only:
            return {"ok": True, "drafted": 0,
                    "followups_drafted": len(followups_drafted),
                    "skipped": skipped, "cap": cap, "remaining": room,
                    "note": ("" if followups_only else
                             "Follow-ups filled today's limit of %d. Send or "
                             "skip what's already waiting first." % cap)}
        if ids:
            marks = ",".join("?" * len(ids))
            rows = c.execute("SELECT * FROM prospects WHERE id IN (%s)" % marks,
                             list(ids)).fetchall()
        else:
            # Email is its own channel. It used to draft only from what was due
            # in the CALL queue today - so with 41 emailable leads sitting in the
            # pipeline and none of today's four holding an address, the button
            # did nothing. Pull from anyone reachable who hasn't been written to.
            fsql, fargs = focus_sql("p")
            rows = c.execute(
                """SELECT p.* FROM prospects p
                   WHERE p.email <> ''
                     AND p.status NOT IN ('booked','dead','cold','conversation','corporate')"""
                + fsql + """
                     -- A send that bounced is not a send. Leaving it in this
                     -- exclusion meant one dead address permanently retired a
                     -- business that has three other people we could write to.
                     -- A skipped first email is a decision, not a gap to
                     -- refill: redrafting it on the next pass (every 90s
                     -- under autopilot) made Skip a button that did nothing.
                     AND NOT EXISTS (SELECT 1 FROM outreach o
                                     WHERE o.prospect_id = p.id
                                       AND (o.state IN ('approved','draft','skipped')
                                            OR (o.state = 'sent'
                                                AND COALESCE(o.reply,'') <> 'bounce')))
                   ORDER BY CASE WHEN COALESCE(p.offer_evidence,'') <> '' THEN 0 ELSE 1 END,
                            COALESCE(p.tier,1) DESC,
                            COALESCE(p.lead_score,0) DESC
                   LIMIT ?""", fargs + [limit]).fetchall()
        dead = dead_addresses(c)
        for r in rows:
            d = row_to_dict(r)
            pid = d["id"]
            contacts = contact_rows(c, pid)
            to, _to_name = best_recipient(contacts, d, dead)
            if not to:
                skipped["no_email"] += 1
                continue
            # A subject line reading "Home - something on your site is costing
            # you calls" is deleted on sight and tells the reader exactly how
            # it was made. Better to leave the lead for a human to name than to
            # spend it on a scraped page title.
            if not usable_company(d.get("company") or "", d.get("domain") or ""):
                skipped["no_real_name"] = skipped.get("no_real_name", 0) + 1
                continue
            # Thirteen prospects on this list are franchise locations of one
            # corporate chain sharing a single corporate domain. The GTM rules
            # exclude corporate, and writing to all of them writes to the same
            # company thirteen times.
            if (d.get("domain") or "").strip():
                same = c.execute("SELECT COUNT(*) FROM prospects WHERE domain=?",
                                 (d["domain"],)).fetchone()[0]
                if same >= CHAIN_DOMAIN_MIN:
                    skipped["corporate_chain"] = skipped.get("corporate_chain", 0) + 1
                    continue
            if d.get("domain"):
                try:
                    if is_suppressed(d["domain"]):
                        skipped["suppressed"] += 1
                        continue
                except Exception:
                    pass
            if c.execute("SELECT 1 FROM outreach WHERE prospect_id=? AND state IN ('draft','approved')",
                         (pid,)).fetchone():
                skipped["already_queued"] += 1
                continue
            if len(made) >= room:
                skipped["over_daily_cap"] += 1
                continue
            v = next_variant(c)
            e = compose_email(d, contacts, variant=v, to_addr=to)
            e["body"] = voiced(e["body"])
            cur = c.execute(
                "INSERT INTO outreach (prospect_id, offer, to_addr, subject, body,"
                " subject_original, body_original, state, created_at, variant, step)"
                " VALUES (?,?,?,?,?,?,?, 'draft', ?, ?, ?)",
                (pid, e["offer"], to, e["subject"], e["body"],
                 e["subject"], e["body"], now(), e.get("variant", v),
                 e.get("step", 1)))
            made.append(cur.lastrowid)
        c.commit()
        left = max(0, cap - sent_today(c) - waiting_now(c))
    return {"ok": True, "drafted": len(made),
            "followups_drafted": len(followups_drafted), "skipped": skipped,
            "cap": cap, "remaining": left, "focus": focus_trades()}


class ReplyBody(BaseModel):
    kind: str = "positive"          # see REPLY_KINDS
    note: str = ""


# Warmbly's taxonomy, kept because the distinctions are the ones that matter:
# an out-of-office is not interest, and a bounce is a list problem rather than
# a copy problem. Scoring them the same is how a test lies to you.
REPLY_KINDS = {"positive", "negative", "out_of_office", "auto_reply",
               "bounce", "unsubscribe"}
# What counts as "the email worked". A flat no is in here on purpose: at ten
# sends a day a negative reply is still proof the message was read and was
# clear, and treating it as a failure throws away most of the signal there is.
HUMAN_REPLIES = {"positive", "negative", "unsubscribe"}


class SocialCheckBody(BaseModel):
    networks: list = ["facebook", "instagram"]
    text: str = ""
    media: list = []
    title: str = ""


@app.post("/api/social/check")
def social_check(request: Request, body: SocialCheckBody):
    """Would every target network accept this? Answered before anything is sent.

    Each network rejects for its own reasons, hours later, in a webhook nobody
    is watching. This is the same check the publish routes run, exposed so the
    UI can run it while a person is still looking at the draft.
    """
    require_auth(request)
    return soc.check_post(body.networks, body.text, body.media or None, body.title)


class IgStageBody(BaseModel):
    caption: str = ""
    image_url: str = ""
    video_url: str = ""
    media_type: str = ""
    link: str = ""                   # tracked and put on the end of the caption
    campaign_id: Optional[int] = None


@app.get("/api/instagram/status")
def instagram_status(request: Request):
    require_auth(request)
    ig = setting("ig_user_id", "").strip()
    tok = setting("ig_token", "").strip() or setting("fb_page_token", "").strip()
    if not ig or not tok:
        return {"connected": False,
                "missing": [k for k, v in (("ig_user_id", ig), ("ig_token", tok)) if not v],
                "how": "Instagram publishing uses the linked Facebook Page's token, "
                       "so fb_page_token works if it has the Instagram scopes. The "
                       "account must be Business or Creator - a personal account "
                       "cannot use the API at all."}
    try:
        q = insta.quota(ig, tok)
    except insta.InstagramError as e:
        return {"connected": True, "ig_user_id": ig, "quota": None, "warning": str(e)}
    return {"connected": True, "ig_user_id": ig, "quota": q}


@app.post("/api/instagram/stage")
def instagram_stage(request: Request, body: IgStageBody):
    """Step 1. Builds the container and stops. Nothing is public yet."""
    require_auth(request)
    ig = setting("ig_user_id", "").strip()
    tok = setting("ig_token", "").strip() or setting("fb_page_token", "").strip()
    if not ig or not tok:
        raise HTTPException(400, "Instagram is not connected. See /api/instagram/status.")

    media = [u for u in (body.image_url, body.video_url) if u]
    if body.link.strip():
        body.caption = with_link(body.caption, ig_link_text(body.link))
    problems = soc.validate("instagram", body.caption, media)
    if problems:
        raise HTTPException(400, "; ".join(problems))

    base = public_base()
    if base and any(u.startswith(base) for u in media):
        chk = media_check(request)
        if not chk["ok"]:
            raise HTTPException(400, chk["problem"])
    try:
        out = insta.stage(ig, tok, image_url=body.image_url,
                          video_url=body.video_url, caption=body.caption,
                          media_type=body.media_type)
    except insta.InstagramError as e:
        raise HTTPException(400, str(e))
    out["next"] = ("POST /api/instagram/release with this container_id. That call "
                   "is the public one and cannot be undone.")
    return out


class IgReleaseBody(BaseModel):
    container_id: str
    wait: bool = True


@app.post("/api/instagram/release")
def instagram_release(request: Request, body: IgReleaseBody):
    """Step 2. This publishes. Instagram has no unpublished state - once this
    returns, it is live."""
    require_auth(request)
    ig = setting("ig_user_id", "").strip()
    tok = setting("ig_token", "").strip() or setting("fb_page_token", "").strip()
    if not ig or not tok:
        raise HTTPException(400, "Instagram is not connected.")
    try:
        if body.wait:
            insta.wait_ready(body.container_id, tok, tries=3, delay=20)
        out = insta.release(ig, tok, body.container_id)
    except insta.InstagramError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "published": True, "media": out}


# ── media ────────────────────────────────────────────────────────────
# This route is deliberately NOT behind require_auth. Meta's servers fetch the
# image and they have no session; an authenticated media URL cannot work by
# definition. What keeps it safe is that `resolve()` only accepts the exact
# name shape we generate (64 hex + known extension) and never joins request
# text onto a path.
# GET *and* HEAD. FastAPI does not add HEAD for a @app.get route, so a HEAD
# was answering 405 - and Meta's fetcher, like most caches and proxies, HEADs
# before it GETs. That failure surfaces as "Instagram could not process the
# media", which names neither the method nor this route.
@app.api_route("/media/{name}", methods=["GET", "HEAD"])
@app.api_route("/welcome/m/{name}", methods=["GET", "HEAD"])
def serve_media(name: str):
    """Two paths, one file. /welcome/m/ is the one handed to Meta: the
    /welcome prefix is already bypassed in Cloudflare Access for the landing
    page, and a bypass is a prefix match, so pictures under it are fetchable
    by a server with no session. /media/ stays for the app's own thumbnails
    and for anyone who saved the old URL."""
    try:
        path, ctype = mstore.resolve(name, DATA)
    except mstore.MediaError:
        raise HTTPException(404, "No such media.")
    return FileResponse(path, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/media")
async def upload_media(request: Request):
    """Raw bytes in the body - deliberately not a multipart form.

    A multipart upload needs python-multipart, and FastAPI raises at IMPORT
    time if it is missing, so a missing dependency stops the whole app from
    starting rather than failing this one route. Raw bytes need nothing, and
    `curl --data-binary @photo.jpg` is easier to drive than a form anyway.
    The content type is sniffed from the bytes regardless, so nothing is lost.
    """
    require_auth(request)
    raw = await request.body()
    try:
        out = mstore.save(raw, DATA)
    except mstore.MediaError as e:
        raise HTTPException(400, str(e))
    base = public_base()
    out["url"] = mstore.public_url(base, out["id"]) if base else ""
    if not base:
        out["warning"] = ("No public_base_url in Settings, so there is no URL to "
                          "hand Facebook. The file is stored either way.")
    return out


# ── image generation (bring your own provider) ───────────────────────
def imagegen_status() -> dict:
    """What the Social tab needs to draw itself: which provider, whether a
    key is saved, what this month has cost in pictures. The key itself never
    leaves the database."""
    prov = setting("imagegen_provider", "").strip()
    p = imagegen.PROVIDERS.get(prov)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    made = failed = 0
    try:
        with closing(db()) as c:
            for ok, n in c.execute("SELECT ok, COUNT(*) FROM imagegen_log "
                                   "WHERE substr(at,1,7)=? GROUP BY ok", (month,)):
                if ok:
                    made = n
                else:
                    failed = n
    except sqlite3.OperationalError:
        pass
    return {
        "provider": prov,
        "label": p["label"] if p else "",
        "has_key": bool(setting("imagegen_api_key", "").strip()),
        "base_url": setting("imagegen_base_url", ""),
        "model": setting("imagegen_model", "") or (p["model"] if p else ""),
        "style": setting("imagegen_style", ""),
        "refine": bool(p and p["refine"]),
        "cost": p["cost"] if p else "",
        "ready": bool(p and setting("imagegen_api_key", "").strip()),
        "made_this_month": made,
        "failed_this_month": failed,
    }


@app.get("/api/imagegen/providers")
def imagegen_providers(request: Request):
    """The catalogue the Social tab's setup box is drawn from - labels, cost,
    defaults and the step-by-step for each. Lives in imagegen.py so the
    instructions and the code that calls the provider cannot drift apart."""
    require_auth(request)
    return {"providers": [dict(key=k, **{kk: vv for kk, vv in v.items()})
                          for k, v in imagegen.PROVIDERS.items()]}


class ImageGenBody(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=imagegen.MAX_PROMPT)
    previous_id: str = ""          # Meta only: refine the picture this id made
    use_style: bool = True         # prepend the workspace's brand notes
    use_logo: bool = True          # stamp the workspace's logo, when one is saved and switched on


def brand_logo_bytes() -> bytes:
    """The workspace's saved logo, or b'' - never raises."""
    mid = setting("brand_logo", "").strip()
    if not mid:
        return b""
    try:
        path, _ = mstore.resolve(mid, DATA)
        return path.read_bytes()
    except Exception:
        return b""


def brand_tag() -> str:
    """The line stamped on every picture (a phone number, a web address),
    or '' when it's switched off or blank."""
    if setting("brand_tag_on", "1") != "1":
        return ""
    return " ".join(setting("brand_tag", "").split())[:60]


def brand_tag_pos(has_logo: bool) -> str:
    """Bottom corner opposite the logo; bottom-left with no logo."""
    if has_logo:
        return imagegen.opposite_corner(setting("brand_logo_pos", "") or "br")
    return "bl"


@app.get("/api/brand")
def brand_get(request: Request):
    require_auth(request)
    mid = setting("brand_logo", "").strip()
    return {"logo": ("/media/" + mid) if mid else "", "on": setting("brand_logo_on", "") == "1",
            "position": setting("brand_logo_pos", "") or "br", "positions": list(imagegen.LOGO_POSITIONS),
            "tag": setting("brand_tag", ""), "tag_on": setting("brand_tag_on", "1") == "1"}


@app.post("/api/brand/logo")
async def brand_logo_upload(request: Request):
    """Raw bytes, like the picture library. PNG with a transparent
    background looks best; JPEG works and gets a rounded badge."""
    require_auth(request)
    raw = await request.body()
    try:
        saved = mstore.save(raw, DATA)
    except mstore.MediaError as e:
        raise HTTPException(400, str(e))
    if not saved["kind"].startswith("image/"):
        raise HTTPException(400, "A logo has to be a picture - PNG (best) or JPEG.")
    set_setting("brand_logo", saved["id"])
    set_setting("brand_logo_on", "1")
    return brand_get(request)


class BrandBody(BaseModel):
    on: Optional[bool] = None
    position: Optional[str] = None
    tag: Optional[str] = Field(None, max_length=60)      # the line on every picture
    tag_on: Optional[bool] = None


@app.post("/api/brand/settings")
def brand_settings(request: Request, body: BrandBody):
    require_auth(request)
    if body.on is not None:
        set_setting("brand_logo_on", "1" if body.on else "")
    if body.tag is not None:
        set_setting("brand_tag", " ".join(body.tag.split()))
    if body.tag_on is not None:
        set_setting("brand_tag_on", "1" if body.tag_on else "")
    if body.position is not None:
        if body.position not in imagegen.LOGO_POSITIONS:
            raise HTTPException(400, "position is one of " + ", ".join(imagegen.LOGO_POSITIONS))
        set_setting("brand_logo_pos", body.position)
    return brand_get(request)


@app.post("/api/brand/logo/remove")
def brand_logo_remove(request: Request):
    require_auth(request)
    set_setting("brand_logo", "")
    set_setting("brand_logo_on", "")
    return brand_get(request)


class PictureError(RuntimeError):
    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status


def make_picture(prompt: str, previous_id: str = "", use_style: bool = True,
                 use_logo: bool = True) -> dict:
    """Make one picture with the workspace's own provider, convert it to the
    JPEG Instagram insists on, stamp the logo, store it, log it. Shared by
    the Make a picture button and the week autopilot. Every attempt -
    success or failure - is logged, because the log is the bill."""
    try:
        cfg = imagegen.config(setting("imagegen_provider", ""), setting("imagegen_api_key", ""),
                              setting("imagegen_base_url", ""), setting("imagegen_model", ""))
    except imagegen.ImageGenError as e:
        raise PictureError(str(e), 400)
    prompt = (prompt or "").strip()
    style = setting("imagegen_style", "").strip()
    if use_style and style and not previous_id:
        # Brand anchoring, the cheap way: the same style notes in front of
        # every first prompt keeps a series looking like one business. Not
        # on refinements - the picture already carries the style.
        prompt = style + "\n\n" + prompt
    t0 = time.time()
    logo = b""
    with closing(db()) as c:
        try:
            out = imagegen.generate(cfg, prompt, previous_id=(previous_id or "").strip() or None)
            jpeg = imagegen.to_jpeg(out["bytes"], out["mime"])
            logo = brand_logo_bytes() if (use_logo and setting("brand_logo_on", "") == "1") else b""
            if logo:
                try:
                    jpeg = imagegen.stamp_logo(jpeg, logo, setting("brand_logo_pos", "") or "br")
                except Exception as e:           # a broken logo file never costs the picture
                    print("logo stamp skipped: %s: %s" % (type(e).__name__, e))
                    logo = b""
            tag = brand_tag() if use_logo else ""
            if tag:
                try:
                    jpeg = imagegen.stamp_text(jpeg, tag, brand_tag_pos(bool(logo)))
                except Exception as e:
                    print("tag stamp skipped: %s: %s" % (type(e).__name__, e))
            saved = mstore.save(jpeg, DATA)
        except (imagegen.ImageGenError, mstore.MediaError) as e:
            c.execute("INSERT INTO imagegen_log (provider, model, prompt, ok, error, ms, at)"
                      " VALUES (?,?,?,0,?,?,?)",
                      (cfg["provider"], cfg["model"], prompt[:2000], str(e)[:600],
                       int((time.time() - t0) * 1000), now()))
            c.commit()
            raise PictureError(str(e), 502)
        c.execute("INSERT INTO imagegen_log (provider, model, prompt, media_id, response_id, "
                  "ok, ms, at) VALUES (?,?,?,?,?,1,?,?)",
                  (cfg["provider"], cfg["model"], prompt[:2000], saved["id"],
                   out.get("response_id") or "", int((time.time() - t0) * 1000), now()))
        c.commit()
    base = public_base()
    saved["url"] = mstore.public_url(base, saved["id"]) if base else ""
    saved["response_id"] = out.get("response_id") or ""
    saved["provider"] = cfg["provider"]
    saved["model"] = cfg["model"]
    saved["ms"] = int((time.time() - t0) * 1000)
    saved["logo"] = bool(logo)
    if not base:
        saved["warning"] = ("Made and stored, but there is no public_base_url in Setup, so "
                            "there is no URL to hand Facebook or Instagram yet.")
    return saved


@app.post("/api/imagegen")
def imagegen_make(request: Request, body: ImageGenBody):
    """The Make a picture button. See make_picture."""
    require_auth(request)
    try:
        return make_picture(body.prompt, body.previous_id, body.use_style, body.use_logo)
    except PictureError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/imagegen/recent")
def imagegen_recent(request: Request, limit: int = 12):
    """The last few pictures, for the strip under the generator - and the
    failures, because a failing prompt should be visible, not silent."""
    require_auth(request)
    base = public_base()
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, provider, model, prompt, media_id, response_id, ok, error, ms, at "
            "FROM imagegen_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),))]
    for r in rows:
        r["url"] = mstore.public_url(base, r["media_id"]) if (base and r["media_id"]) else ""
        r["local"] = "/media/" + r["media_id"] if r["media_id"] else ""
    return {"rows": rows}


@app.get("/api/media/check")
def media_check(request: Request):
    """Can an anonymous stranger fetch a media URL? The thing that silently
    breaks everything downstream if the answer is no."""
    require_auth(request)
    base = public_base()

    def fetch(url):
        # Ask the way Meta's fetcher asks. Cloudflare's bot rules answer 403
        # to Python's default user agent while serving facebookexternalhit
        # fine - checking as Python reported "blocked" for a host that works.
        # And don't follow redirects: Access answers a stranger with a 302 to
        # its sign-in page, which is exactly the thing to catch.
        import urllib.request, urllib.error
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(url, headers={"User-Agent": mstore.FETCHER_UA})
        try:
            with opener.open(req, timeout=15) as r:
                return r.status, r.read(4000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            where = e.headers.get("Location", "") if e.headers else ""
            return e.code, ("Location: %s\n" % where) + e.read(4000).decode("utf-8", "replace")

    problem = mstore.hosting_problem(base, fetch)
    return {"ok": not problem, "base_url": base, "problem": problem}


# ── the picture library: the customer's own material ────────────────
# Originals stay wherever the customer keeps them (laptop, external drive,
# Drive, Dropbox). The app copies what it is handed into the content-
# addressed media store so Meta has a public URL to fetch, and keeps a
# library row so the picture shows up as a thumbnail to pick from.
def _library_row(c, saved: dict, name: str, source: str, ref: str) -> dict:
    """Insert-or-touch. The store is content-addressed, so the same photo
    added twice (once from a laptop, once from Drive) is one row."""
    existing = c.execute("SELECT id FROM media_library WHERE media_id=?",
                         (saved["id"],)).fetchone()
    if existing:
        c.execute("UPDATE media_library SET removed_at=NULL, name=COALESCE(NULLIF(name,''),?) "
                  "WHERE id=?", (name, existing["id"]))
        lid = existing["id"]
    else:
        c.execute("INSERT INTO media_library (media_id, name, source, source_ref, width, "
                  "height, bytes, added_at) VALUES (?,?,?,?,?,?,?,?)",
                  (saved["id"], name, source, ref, saved.get("width") or 0,
                   saved.get("height") or 0, saved.get("bytes") or 0, now()))
        lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": lid, "media_id": saved["id"], "name": name, "source": source,
            "width": saved.get("width") or 0, "height": saved.get("height") or 0}


def _library_public(row: dict) -> dict:
    base = public_base()
    row = dict(row)
    row["url"] = mstore.public_url(base, row["media_id"]) if base else ""
    row["local"] = "/media/" + row["media_id"]
    return row


def _http3(url: str, timeout: int = 30):
    """(status, headers, bytes) - the shape library.py wants."""
    req = urllib.request.Request(url, headers={"User-Agent": "JustGrit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read(mstore.MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read(4000)


@app.get("/api/social/library")
def library_list(request: Request, limit: int = 200):
    require_auth(request)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, media_id, name, source, source_ref, width, height, bytes, added_at "
            "FROM media_library WHERE removed_at IS NULL ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),))]
    return {"rows": [_library_public(r) for r in rows], "public": bool(public_base())}


@app.post("/api/social/library/upload")
async def library_upload(request: Request, name: str = ""):
    """Raw bytes in the body (see /api/media for why not multipart). The
    browser reads the file the customer picked and posts it; `name` is the
    filename, for the caption under the thumbnail."""
    require_auth(request)
    raw = await request.body()
    try:
        saved = mstore.save(raw, DATA)
    except mstore.MediaError as e:
        raise HTTPException(400, str(e))
    if not saved["kind"].startswith("image/"):
        raise HTTPException(400, "The library takes pictures (JPEG or PNG). Video isn't "
                                 "supported for posting yet.")
    with closing(db()) as c:
        row = _library_row(c, saved, library.clean_name(name), "upload", "")
        c.commit()
    return _library_public(row)


class LibraryLinkBody(BaseModel):
    link: str = Field(..., min_length=8, max_length=2000)


@app.post("/api/social/library/link")
def library_from_link(request: Request, body: LibraryLinkBody):
    """A Google Drive folder or file, a Dropbox link, or a picture's own
    address. Lists first, then downloads each picture; a folder is capped
    (library.MAX_FILES) and every failure is reported by name rather than
    stopping the batch."""
    require_auth(request)
    try:
        listing = library.list_link(body.link, _http3, google_key() or "")
    except library.LibraryError as e:
        raise HTTPException(400, str(e))
    added, failed = [], []
    with closing(db()) as c:
        for it in listing["items"]:
            try:
                raw = library.fetch_image(it["download"], _http3)
                saved = mstore.save(raw, DATA)
                if not saved["kind"].startswith("image/"):
                    raise library.LibraryError("not a picture")
                added.append(_library_public(_library_row(
                    c, saved, library.clean_name(it["name"]), listing["kind"], it["ref"])))
            except (library.LibraryError, mstore.MediaError) as e:
                failed.append({"name": it["name"], "why": str(e)})
        c.commit()
    if not added and not failed:
        raise HTTPException(400, listing.get("note") or
                            "No pictures found at that link. For a Drive folder, share it "
                            "as 'Anyone with the link' and make sure it holds JPEG or PNG files.")
    return {"kind": listing["kind"], "added": added, "failed": failed,
            "note": listing.get("note", "")}


@app.post("/api/social/library/{lid}/remove")
def library_remove(request: Request, lid: int):
    """Takes it out of the library. The file itself stays - a scheduled post
    may still point at its URL, and content-addressed files are cheap."""
    require_auth(request)
    with closing(db()) as c:
        c.execute("UPDATE media_library SET removed_at=? WHERE id=?", (now(), lid))
        c.commit()
    return {"ok": True}


# ── competitor watch ─────────────────────────────────────────────────
# "Check Google for the same products the end user is selling, pull up the
# competitors, check their website, their SEO and the keywords they're
# using ... so it's easier to sell against, and we can find what's working
# for them and apply it." See competitors.py for what is and isn't claimed.
COMPETITOR_RESCAN_HOURS = 6


def own_site_domain() -> str:
    site = setting("sender_site", "").strip()
    return re.sub(r"^https?://", "", site).split("/")[0].replace("www.", "").lower()


def read_site(domain: str) -> tuple:
    """(seo, analysis) for one domain. Two fetches; either may fail alone."""
    from seo_synopsis import read_seo
    seo, analysis = {}, {}
    try:
        seo = read_seo(domain)
    except Exception as e:
        seo = {"ok": False, "error": type(e).__name__}
    try:
        analysis = analyze("https://" + domain, deep=False)
    except Exception as e:
        analysis = {"ok": False, "error": "%s" % type(e).__name__}
    return seo, analysis


OWN_PLACE_DAYS = 7


def own_place(dom: str) -> dict:
    """Your own Google listing - rating and review count - found by name and
    matched on your website. Looked up at most weekly (one Places request).
    {} when there's no key or no listing links to your site: then the
    comparison says what THEY have and never invents "your 0"."""
    try:
        cached = json.loads(setting("own_place", "") or "{}")
    except ValueError:
        cached = {}
    fresh_after = (datetime.now(timezone.utc) - timedelta(days=OWN_PLACE_DAYS)).isoformat()
    if cached.get("domain") == dom and (cached.get("looked_at") or "") > fresh_after:
        return cached
    key = google_key()
    if not key:
        return cached if cached.get("domain") == dom else {}
    name = setting("sender_company", "").strip() or sender_brand()
    cities = [setting("default_city", "").strip() or "San Antonio, TX"]
    home = town_of(setting("sender_address", "").replace(" TX ", ", TX "))     # where the business is
    m = re.search(r",\s*([A-Za-z .]+),?\s+TX\b", setting("sender_address", ""))
    home = home or ((m.group(1).strip() + ", TX") if m else "")
    if home and home.split(",")[0].lower() not in cities[0].lower():
        cities.append(home)
    found = {}
    try:
        for city in cities:
            for p in places_search("%s %s" % (name, city), key, pages=1):
                if domain_of(p.get("websiteUri")) == dom:
                    found = {"place_id": p.get("id") or "", "rating": p.get("rating"),
                             "reviews": p.get("userRatingCount") or 0,
                             "name": (p.get("displayName") or {}).get("text", "")}
                    break
            if found:
                break
    except Exception:
        return cached if cached.get("domain") == dom else {}
    found.update({"domain": dom, "looked_at": now()})
    set_setting("own_place", json.dumps(found))
    return found


def own_profile(refresh: bool = False) -> dict:
    """Your own site, read the same way, so the comparison is apples to
    apples. Cached in Settings; refreshed with the competitors."""
    dom = own_site_domain()
    if not dom:
        return {}
    raw = setting("own_site_profile", "")
    if raw and not refresh:
        try:
            cached = json.loads(raw)
            if cached.get("domain") == dom:
                return cached
        except ValueError:
            pass
    seo, analysis = read_site(dom)
    place = own_place(dom)
    prof = compt.profile({"rating": place.get("rating"), "reviews": place.get("reviews")}, seo, analysis,
                         name=setting("sender_company", "") or ws.me()["name"], domain=dom)
    prof["reviews"] = place.get("reviews")           # None = we couldn't find your listing
    prof["google_listing"] = bool(place.get("place_id"))
    prof["read_at"] = now()
    set_setting("own_site_profile", json.dumps(prof))
    return prof


def competitor_scan(c, row) -> dict:
    """Read one tracked competitor and store the snapshot, keeping the
    previous one so the card can say what changed."""
    dom = (row["domain"] or "").strip()
    seo, analysis = read_site(dom) if dom else ({}, {"ok": False, "error": "no website"})
    place = {"rating": row["rating"], "reviews": row["reviews"]}
    prof = compt.profile(place, seo, analysis, name=row["name"], domain=dom)
    prof["read_at"] = now()
    c.execute("UPDATE competitors SET prev=snapshot, snapshot=?, last_scan_at=? WHERE id=?",
              (json.dumps(prof), now(), row["id"]))
    return prof


def _competitor_card(row, own: dict) -> dict:
    snap, prev = {}, {}
    try:
        snap = json.loads(row["snapshot"] or "{}")
        prev = json.loads(row["prev"] or "{}")
    except ValueError:
        pass
    return {"id": row["id"], "name": display_company(row["name"] or "", row["domain"] or "") or row["name"],
            "full_name": row["name"], "domain": row["domain"], "phone": row["phone"],
            "address": row["address"], "rating": row["rating"], "reviews": row["reviews"],
            "added_at": row["added_at"], "last_scan_at": row["last_scan_at"],
            "profile": snap, "changes": compt.changes(prev, snap),
            "steal": compt.steal_list(own, snap) if snap else []}


@app.get("/api/competitors")
def competitors_list(request: Request):
    require_auth(request)
    own = own_profile()
    w = ws.me()
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM competitors WHERE removed_at IS NULL ORDER BY id").fetchall()
        cards = [_competitor_card(r, own) for r in rows]
    offers = list((catalog().get("offers") or {}).keys())
    return {"competitors": cards, "own": own, "own_domain": own_site_domain(),
            "sells": setting("sells", "").strip() or w.get("sells") or "",
            "city": setting("default_city", "").strip() or "San Antonio, TX",
            "queries": compt.search_queries(setting("sells", "").strip() or w.get("sells") or "",
                                            offers, setting("default_city", "")),
            "max": compt.MAX_TRACKED, "rescan_hours": COMPETITOR_RESCAN_HOURS,
            "has_google": bool(google_key())}


class CompetitorFindBody(BaseModel):
    query: str = ""


@app.post("/api/competitors/find")
def competitors_find(request: Request, body: CompetitorFindBody):
    """Who shows up on Google for what you sell, where you are. Nothing is
    saved - pick the ones worth watching."""
    require_auth(request)
    key = google_key()
    if not key:
        raise HTTPException(400, "A Google Maps key (Setup → Lead finder) is what finds competitors.")
    w = ws.me()
    offers = list((catalog().get("offers") or {}).keys())
    queries = [body.query.strip()] if body.query.strip() else \
        compt.search_queries(setting("sells", "").strip() or w.get("sells") or "",
                             offers, setting("default_city", ""))
    own = own_site_domain()
    with closing(db()) as c:
        tracked = {r[0] for r in c.execute("SELECT place_id FROM competitors WHERE removed_at IS NULL")}
        tracked_dom = {r[0] for r in c.execute("SELECT domain FROM competitors WHERE removed_at IS NULL") if r[0]}
    seen, out = set(), []
    for q in queries[:4]:
        for p in places_search(q, key, pages=1):
            pid = p.get("id") or ""
            dom = domain_of(p.get("websiteUri"))
            if not pid or pid in seen or p.get("businessStatus") not in (None, "OPERATIONAL"):
                continue
            if own and dom == own:
                continue
            seen.add(pid)
            raw = (p.get("displayName") or {}).get("text", "")
            addr = p.get("formattedAddress") or ""
            out.append({"place_id": pid, "name": display_company(raw, dom) or raw, "full_name": raw,
                        "domain": dom, "phone": p.get("nationalPhoneNumber") or "",
                        "address": addr, "town": town_of(addr), "local": is_local_address(addr),
                        "rating": p.get("rating"),
                        "reviews": p.get("userRatingCount") or 0, "query": q,
                        "tracked": bool(pid in tracked or (dom and dom in tracked_dom))})
    # Google's text search happily returns a Dallas shop for "app development
    # San Antonio". Local first; the rest are kept but marked, not mixed in.
    out.sort(key=lambda x: (not x["local"], not x["tracked"], -(x["reviews"] or 0)))
    return {"candidates": out[:40], "queries": queries[:4],
            "outside": sum(1 for x in out[:40] if not x["local"])}


def town_of(address: str) -> str:
    """"2544 MacArthur View, San Antonio, TX 78217, USA" -> "San Antonio, TX"."""
    parts = [x.strip() for x in (address or "").split(",")]
    for i, part in enumerate(parts):
        m = re.match(r"^([A-Z]{2})\s+\d{5}", part)
        if m and i > 0:
            return "%s, %s" % (parts[i - 1], m.group(1))
    return ""


def is_local_address(address: str) -> bool:
    """Inside the area you work: a ZIP in the same 3-digit region as your
    metros (782xx San Antonio, 781xx New Braunfels/Seguin/Cibolo...), or one
    of your towns by name. An address we can't read counts as local."""
    if not (address or "").strip():
        return True
    prefixes = set()
    for m in load_metros().values():
        prefixes |= {z[:3] for z in (m.get("zips") or []) if len(z) >= 3}
    zips = re.findall(r"\b(\d{5})(?:-\d{4})?\b", address)
    towns = [c.split(",")[0].strip().lower() for c in work_cities()]
    if any(t and t in address.lower() for t in towns):
        return True
    if zips and prefixes:
        return zips[-1][:3] in prefixes
    return True                 # no ZIP to go on - don't hide them


class CompetitorTrackBody(BaseModel):
    place_id: str = ""
    name: str = Field(..., min_length=1, max_length=200)
    domain: str = ""
    phone: str = ""
    address: str = ""
    rating: Optional[float] = None
    reviews: int = 0


@app.post("/api/competitors/track")
def competitors_track(request: Request, body: CompetitorTrackBody):
    """Watch one. Reads it right away so the card is never empty."""
    require_auth(request)
    dom = domain_of(body.domain) if body.domain else ""
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) FROM competitors WHERE removed_at IS NULL").fetchone()[0]
        if n >= compt.MAX_TRACKED:
            raise HTTPException(400, "You're watching %d already - drop one first. More than that "
                                     "and the list stops being the ones that matter." % n)
        dup = c.execute("SELECT id FROM competitors WHERE removed_at IS NULL AND "
                        "((place_id<>'' AND place_id=?) OR (domain<>'' AND domain=?))",
                        (body.place_id, dom)).fetchone()
        if dup:
            raise HTTPException(400, "Already watching them.")
        c.execute("INSERT INTO competitors (place_id, name, domain, phone, address, rating, reviews, added_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (body.place_id, body.name.strip(), dom, body.phone,
                                               body.address, body.rating, body.reviews, now()))
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = c.execute("SELECT * FROM competitors WHERE id=?", (cid,)).fetchone()
        competitor_scan(c, row)
        c.commit()
        row = c.execute("SELECT * FROM competitors WHERE id=?", (cid,)).fetchone()
        return _competitor_card(row, own_profile())


@app.post("/api/competitors/{cid}/scan")
def competitors_rescan(request: Request, cid: int):
    require_auth(request)
    with closing(db()) as c:
        row = c.execute("SELECT * FROM competitors WHERE id=? AND removed_at IS NULL", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        competitor_scan(c, row)
        c.commit()
        row = c.execute("SELECT * FROM competitors WHERE id=?", (cid,)).fetchone()
        return _competitor_card(row, own_profile())


@app.post("/api/competitors/{cid}/remove")
def competitors_remove(request: Request, cid: int):
    require_auth(request)
    with closing(db()) as c:
        c.execute("UPDATE competitors SET removed_at=? WHERE id=?", (now(), cid))
        c.commit()
    return {"ok": True}


@app.post("/api/competitors/scan_all")
def competitors_scan_all(request: Request):
    """Everyone, plus your own site, right now."""
    require_auth(request)
    own_profile(refresh=True)
    return {"scanned": competitors_due(ws.current(), force=True)}


def competitors_due(slug: str, force: bool = False) -> int:
    """Re-read every tracked competitor older than the rescan window.
    Called by the loop with the workspace set."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=COMPETITOR_RESCAN_HOURS)).isoformat()
    n = 0
    with closing(db(slug)) as c:
        rows = c.execute("SELECT * FROM competitors WHERE removed_at IS NULL AND "
                         "(? OR last_scan_at IS NULL OR last_scan_at < ?)", (1 if force else 0, cutoff)).fetchall()
        for row in rows:
            try:
                competitor_scan(c, row)
                c.commit()
                n += 1
            except Exception as e:
                print("competitor scan [%s] %s: %s: %s" % (slug, row["name"], type(e).__name__, e))
    return n


HEARTBEAT = {}
LOOP_EVERY = {"social-queue": 30, "social-autopilot": 600, "email-autopilot": 90, "outbox-refill": 120, "competitor-watch": 1800}


def beat(name: str):
    HEARTBEAT[name] = time.time()


def _competitor_loop(every: int = 1800):
    while True:
        beat("competitor-watch")
        time.sleep(every)
        for slug in list(ws.WORKSPACES):
            tok = ws.CURRENT.set(slug)
            try:
                competitors_due(slug)
            except Exception as e:
                print("competitor loop [%s]: %s: %s" % (slug, type(e).__name__, e))
            try:
                reputation_due()
            except Exception as e:
                print("reputation loop [%s]: %s: %s" % (slug, type(e).__name__, e))
            finally:
                ws.CURRENT.reset(tok)


# ── facebook ─────────────────────────────────────────────────────────
class FbPublishBody(BaseModel):
    message: str = ""
    image_url: str = ""
    link: str = ""
    publish_now: bool = False        # opt IN; default leaves it unpublished
    campaign_id: Optional[int] = None


@app.get("/api/facebook/status")
def facebook_status(request: Request):
    require_auth(request)
    page = setting("fb_page_id", "").strip()
    tok = setting("fb_page_token", "").strip()
    if not page or not tok:
        return {"connected": False,
                "missing": [k for k, v in (("fb_page_id", page),
                                           ("fb_page_token", tok)) if not v],
                "how": "The token must be a PAGE access token, not the user token "
                       "from Graph API Explorer. POST /api/facebook/exchange with a "
                       "user token and a page id will derive it."}
    exp = setting("fb_page_token_expires", "").strip()
    expires_at = int(exp) if exp.isdigit() else None
    expired = bool(expires_at) and expires_at < time.time()
    # Meta doesn't always tell us when a token dies; a publish that failed on
    # the token in the last week does. That is what the owner needs to see.
    broken = ""
    with closing(db()) as c:
        r = c.execute("SELECT error, when_at FROM social_queue WHERE state='failed' AND error LIKE '%access token%' "
                      "AND when_at >= ? ORDER BY when_at DESC LIMIT 1",
                      ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"),)).fetchone()
        if r:
            ok_after = c.execute("SELECT 1 FROM social_queue WHERE state='published' AND published_at > ?", (r["when_at"],)).fetchone()
            if not ok_after:
                broken = "A post failed on %s because Facebook no longer accepts the token. Reconnect the Page under Setup → Facebook & Instagram; the failed post is kept and can be swapped back in." % r["when_at"][:10]
                expired = True
    return {"connected": True, "page_id": page, "expires_at": expires_at, "expired": expired, "broken": broken}


class FbExchangeBody(BaseModel):
    user_token: str
    page_id: str


@app.post("/api/facebook/exchange")
def facebook_exchange(request: Request, body: FbExchangeBody):
    """Derive the page token from a user token. The step people skip."""
    require_auth(request)
    user_tok = body.user_token.strip()
    # A Page key made from a short-lived user token dies with it (about an
    # hour). With the app's id and secret on file, stretch the user token to
    # ~60 days first; a Page key made from THAT one doesn't expire.
    app_id, app_secret = setting("fb_app_id", "").strip(), setting("fb_app_secret", "").strip()
    if app_id and app_secret:
        try:
            user_tok = fb.long_lived_token(app_id, app_secret, user_tok).get("access_token") or user_tok
        except fb.FacebookError:
            pass
    try:
        tok = fb.page_token(user_tok, body.page_id)
    except fb.FacebookError as e:
        raise HTTPException(400, str(e))
    set_setting("fb_page_id", body.page_id)
    set_setting("fb_page_token", tok)
    exp = fb.token_expiry(tok)
    set_setting("fb_page_token_expires", str(exp.get("expires_at", "")))

    # While we hold a page token, grab the linked Instagram account id. It only
    # exists on the Page, Instagram's API needs the number rather than the
    # @handle, and hand-looking-it-up in Graph Explorer is where this stalls.
    ig = {}
    try:
        ig = fb.instagram_account(body.page_id, tok)
    except fb.FacebookError:
        ig = {}
    if ig.get("id"):
        set_setting("ig_user_id", ig["id"])

    lasting = exp.get("expires_at", 1) == 0
    return {"ok": True, "page_id": body.page_id,
            "instagram": ig or None, "expires_at": exp.get("expires_at"),
            "note": ("Page token stored - not returned here on purpose. "
                     + ("It doesn't expire. " if lasting else
                        "WARNING: it expires with the token you pasted (about an hour). "
                        "Extend the token in Meta's Access Token Tool and connect again. ")
                     + ("Instagram @%s (%s) linked and stored."
                        % (ig.get("username") or "?", ig["id"]) if ig.get("id") else
                        "No Instagram Business account is linked to this Page, so "
                        "Instagram publishing stays off. Link it in the Page's "
                        "settings - a personal Instagram account cannot be linked "
                        "and cannot use the API."))}


@app.post("/api/facebook/publish")
def facebook_publish(request: Request, body: FbPublishBody):
    """Post to the Page. Unpublished unless publish_now is explicitly true."""
    require_auth(request)
    page = setting("fb_page_id", "").strip()
    tok = setting("fb_page_token", "").strip()
    if not page or not tok:
        raise HTTPException(400, "Facebook is not connected. See /api/facebook/status.")

    if body.link.strip():
        # Same as a scheduled post: tracked, and with a picture the link goes
        # in the words (a photo post has no link card to hang it on).
        body.link = socialkit.utm(body.link, "facebook",
                                  campaign_for(body.campaign_id)[1] or "post")
        if body.image_url:
            body.message, body.link = with_link(body.message, body.link), ""

    problems = soc.validate("facebook", body.message,
                            [body.image_url] if body.image_url else [])
    if problems:
        raise HTTPException(400, "; ".join(problems))

    # If we are handing Facebook one of our own URLs, make sure Facebook can
    # actually reach it before asking them to fetch it - the error they return
    # otherwise says nothing about Cloudflare Access.
    base = public_base()
    if body.image_url and base and body.image_url.startswith(base):
        chk = media_check(request)
        if not chk["ok"]:
            raise HTTPException(400, chk["problem"])

    try:
        out = fb.publish(page, tok, message=body.message, image_url=body.image_url,
                         link=body.link, published=body.publish_now)
    except fb.FacebookError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "published": body.publish_now, "post": out,
            "note": "" if body.publish_now else
                    "Created UNPUBLISHED. With a picture that is a hidden photo, not a "
                    "draft post - it won't appear anywhere on the Page to release. "
                    "Re-send with publish_now=true to post it."}


def metricool_client():
    """Build a client from Settings, or explain exactly what's missing."""
    return mc.Metricool(setting("metricool_token", ""),
                        setting("metricool_user_id", ""),
                        setting("metricool_blog_id", ""),
                        setting("metricool_timezone", "America/Chicago"))


@app.get("/api/social/status")
def social_status(request: Request):
    """Is Metricool wired up, and what can it do for this workspace."""
    require_auth(request)
    have = {k: bool(setting("metricool_" + k, "").strip())
            for k in ("token", "user_id", "blog_id")}
    if not all(have.values()):
        missing = [k for k, v in have.items() if not v]
        return {"connected": False, "missing": missing,
                "how": "Metricool > Settings > API gives you all three. The token "
                       "only exists on Advanced and Custom plans - on a lower plan "
                       "there is nothing to paste and this stays off."}
    return {"connected": True, "missing": [],
            "competitor_networks": sorted(mc.COMPETITOR_NETWORKS),
            "besttime_providers": sorted(mc.BESTTIME_PROVIDERS)}


@app.get("/api/social/scheduled")
def social_scheduled(request: Request, days: int = 14):
    require_auth(request)
    start = datetime.now().replace(microsecond=0)
    end = start + timedelta(days=max(1, min(days, 90)))
    try:
        return {"ok": True, "posts": metricool_client().scheduled_posts(
            start.isoformat(), end.isoformat())}
    except mc.MetricoolError as e:
        raise HTTPException(400, str(e))


class SchedulePostBody(BaseModel):
    text: str
    when: str                       # ISO local datetime
    networks: list                  # ["instagram","facebook","youtube"]
    media: list = []
    first_comment: str = ""
    auto_publish: bool = False      # opt IN, never the default


@app.post("/api/social/schedule")
def social_schedule(request: Request, body: SchedulePostBody):
    """Queue a post. Draft by default - a person still has to release it.

    Same rule as the Outbox and the ad campaigns: the machine prepares, a
    human decides. Publishing to an audience is not reversible by editing a
    row, so it does not happen without somebody saying so.
    """
    require_auth(request)
    if not (body.text or "").strip():
        raise HTTPException(400, "A post needs text.")
    try:
        out = metricool_client().schedule_post(
            body.text, body.when, body.networks, media=body.media or None,
            auto_publish=body.auto_publish, draft=not body.auto_publish,
            first_comment=body.first_comment)
    except mc.MetricoolError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "draft": not body.auto_publish, "post": out}


@app.get("/api/social/besttimes")
def social_besttimes(request: Request, provider: str = "instagram", days: int = 7):
    require_auth(request)
    start = datetime.now().replace(microsecond=0)
    end = start + timedelta(days=max(1, min(days, 30)))
    try:
        client = metricool_client()
        raw = client.best_times(provider, start.isoformat(), end.isoformat())
    except mc.MetricoolError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "provider": provider, **mc.Metricool.top_slots(raw)}


@app.get("/api/social/competitors")
def social_competitors(request: Request, network: str = "instagram", days: int = 30):
    require_auth(request)
    end = datetime.now().replace(microsecond=0)
    start = end - timedelta(days=max(1, min(days, 180)))
    try:
        rows = metricool_client().competitors(network, start.isoformat(),
                                              end.isoformat())
    except mc.MetricoolError as e:
        raise HTTPException(400, str(e))
    listed = rows.get("data") if isinstance(rows, dict) else rows
    if not listed:
        return {"ok": True, "network": network, "competitors": [],
                "note": "No competitors are being tracked on %s yet. Add them and "
                        "Metricool starts collecting from that day forward - it "
                        "cannot backfill history it never watched." % network}
    return {"ok": True, "network": network, "competitors": listed}


class CompetitorBody(BaseModel):
    network: str
    handle: str


@app.post("/api/social/competitors")
def social_add_competitor(request: Request, body: CompetitorBody):
    require_auth(request)
    try:
        out = metricool_client().add_competitor(body.network, body.handle)
    except mc.MetricoolError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "added": body.handle, "network": body.network,
            "note": "Tracking starts now. Metricool does not backfill.", "raw": out}


# ── the Social tab's own tools: plan, review card, queue ─────────────
def social_business() -> dict:
    """What the plan and the card know about THIS business. The workspace
    record first, Settings on top where a person typed something better."""
    w = ws.me()
    # The business's name, not the person's: posts go out as "HomeRepair
    # Tech", never "Daniel Watson" (sender_name is who signs the emails).
    return {"name": setting("social_name", "").strip() or w["name"],
            "city": setting("default_city", "").strip() or "San Antonio, TX",
            "sells": w.get("sells") or "", "buyer": w.get("buyer") or "",
            "accent": w.get("accent") or "#e8a33d"}


@app.get("/api/social/plan")
def social_plan(request: Request, week: str = ""):
    """Five posts for the week, one per pillar, with captions to edit and
    image prompts that feed the picture box. Deterministic per ISO week."""
    require_auth(request)
    biz = social_business()
    posts = socialkit.plan_week(biz, week or None)
    return {"week": week or datetime.now().strftime("%G-W%V"), "business": biz,
            "posts": posts,
            "note": ("Square brackets are the bits only you can fill in. The image "
                     "prompts go straight into 'Make a picture' - or use your own photo, "
                     "which beats a generated one for the behind-the-scenes post.")}


class ReviewCardBody(BaseModel):
    quote: str = Field(..., min_length=10, max_length=1200)
    reviewer: str = ""
    stars: int = 5


@app.post("/api/social/review_card")
def social_review_card(request: Request, body: ReviewCardBody):
    """Turn a customer review into a branded 1080x1080 card plus a caption.
    Drawn, not generated - the customer's words are never re-typed by a
    model. Stored in the media store like any other picture."""
    require_auth(request)
    biz = social_business()
    stars = max(1, min(5, int(body.stars or 5)))
    try:
        jpeg = socialkit.review_card(body.quote, body.reviewer, stars, biz["name"],
                                     accent=biz["accent"])
        saved = mstore.save(jpeg, DATA)
    except (RuntimeError, mstore.MediaError) as e:
        raise HTTPException(500, str(e))
    base = public_base()
    saved["url"] = mstore.public_url(base, saved["id"]) if base else ""
    saved["caption"] = socialkit.review_caption(body.quote, body.reviewer, stars,
                                               biz["name"], biz["city"])
    if not base:
        saved["warning"] = ("Made and stored, but there is no public_base_url in Setup, "
                            "so there is no URL to hand Facebook or Instagram yet.")
    return saved


# ── campaigns + the caption writer ──────────────────────────────────
# Daniel: "auto gen the description for the post, and make an area where we
# can select a campaign and then it makes the post in regards to it."
# Campaigns live per workspace; the writer is captions.py.

MEDIA_ID_RX = re.compile(r"([0-9a-f]{64}\.(?:jpg|png))(?:$|[?#])")


def local_today() -> str:
    return datetime.now(workspace_tz()).strftime("%Y-%m-%d")


def _insert_campaign(c, st: dict) -> None:
    d = captions.clean_campaign(st)
    c.execute("INSERT INTO social_campaigns (name, offer, details, audience, cta, link, "
              "hashtags, picture_idea, starts, ends, created_at, updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              tuple(d[k] for k in captions.FIELDS) + (now(), now()))


def campaigns_seed(c, today: str = "") -> None:
    """Give a workspace its starter campaigns once. Deleting them all later
    leaves the list empty - the flag, not the row count, says it was done.
    Then, every call, the cheap check for the October catalog switch."""
    today = today or local_today()
    if setting("campaigns_seeded", "") != "1":
        if not c.execute("SELECT 1 FROM social_campaigns LIMIT 1").fetchone():
            for st in captions.starters_for(ws.current(), today):
                _insert_campaign(c, st)
            c.commit()
        set_setting("campaigns_seeded", "1")
        # seeded straight onto whichever catalog is in force
        set_setting("catalog_version", captions.CATALOG_VERSION if captions.catalog_live(today) else "2026-09")
    catalog_switch(c, today)


OLD_PRICE_RX = re.compile(r"\$(?:99|199)\b|\$100 in credits|\$300 in credits|\$50 in credits|up to 60 minutes|60 minutes of labor")


def catalog_switch(c, today: str = "") -> dict:
    """Daniel, 2026-09-25: "I'm moving to the new pricing structure as of
    October 1." On the first call on or after that day, per workspace:
    starter campaigns still word-for-word as we seeded them are replaced by
    the October versions; ones Daniel edited are kept (and reported); the
    two new one-time products are added; the caption rules move over
    unless he rewrote them; and any post already scheduled for October
    that still quotes a September price is pulled back into Saved posts
    rather than going out wrong. Runs once - the setting remembers."""
    today = today or local_today()
    slug = ws.current()
    if slug not in captions.STARTERS_2026_09 or not captions.catalog_live(today):
        return {}
    if setting("catalog_version", "") == captions.CATALOG_VERSION:
        return {}
    old = {s["name"]: captions.clean_campaign(s) for s in captions.STARTERS_2026_09[slug]}
    new = {s["name"]: captions.clean_campaign(s) for s in captions.STARTERS[slug]}
    rep = {"replaced": [], "kept": [], "added": [], "pulled": 0, "rules": False}
    rows = [dict(r) for r in c.execute("SELECT * FROM social_campaigns ORDER BY id")]
    have = set()
    for r in rows:
        o = old.get(r["name"])
        if not o:
            have.add(r["name"]); continue
        n = new.get(captions.RENAMED.get(r["name"], r["name"]))
        untouched = all((r.get(k) or "") == (o.get(k) or "") for k in captions.FIELDS)
        if untouched and n:
            c.execute("UPDATE social_campaigns SET %s, updated_at=? WHERE id=?"
                      % ", ".join("%s=?" % k for k in captions.FIELDS),
                      tuple(n[k] for k in captions.FIELDS) + (now(), r["id"]))
            rep["replaced"].append(n["name"]); have.add(n["name"])
        else:
            rep["kept"].append(r["name"]); have.add(captions.RENAMED.get(r["name"], r["name"]))
    if rows:                                      # a list he emptied on purpose stays empty
        for name, st in new.items():
            if name not in have and name not in old and name not in captions.RENAMED.values():
                _insert_campaign(c, st); rep["added"].append(name)
    saved_rules = setting("caption_rules", None)
    if saved_rules is None or saved_rules.strip() == captions.RULES_2026_09[slug].strip():
        c.execute("DELETE FROM settings WHERE k='caption_rules'")     # back to the (new) default
        rep["rules"] = True
    for r in c.execute("SELECT id, text FROM social_queue WHERE state='scheduled' AND when_at >= ? "
                       "AND COALESCE(text,'') != ''", (captions.CATALOG_EFFECTIVE,)).fetchall():
        if OLD_PRICE_RX.search(r["text"]):
            c.execute("UPDATE social_queue SET state='draft', error='Pulled back on Oct 1: it quoted "
                      "a September price. Rewrite it from the campaign and schedule it again.' WHERE id=?",
                      (r["id"],))
            rep["pulled"] += 1
    c.commit()
    set_setting("catalog_version", captions.CATALOG_VERSION)
    print("catalog [%s]: October pricing on - %d campaign(s) updated, %d kept as edited, %d added, "
          "%d scheduled post(s) pulled back%s" % (slug, len(rep["replaced"]), len(rep["kept"]),
                                                  len(rep["added"]), rep["pulled"],
                                                  ", caption rules updated" if rep["rules"] else ""))
    return rep


def caption_rules(today: str = "") -> tuple:
    """(rules text, banned words) for this workspace - saved, or the starter
    for whichever catalog is in force today."""
    slug = ws.current()
    rules = setting("caption_rules", None)
    banned = setting("caption_banned", None)
    banned = captions.BANNED.get(slug, "") if banned is None else banned
    never = slop.never_list(setting("never_say", ""))          # his never-say list covers posts too
    if never:
        banned = ", ".join([b for b in [banned.strip()] if b] + never)
    return ((captions.rules_for(slug, today or local_today()) if rules is None else rules), banned)


def _campaign_row(c, cid: int) -> dict:
    r = c.execute("SELECT * FROM social_campaigns WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "That campaign doesn't exist any more.")
    return dict(r)


@app.get("/api/social/campaigns")
def social_campaigns(request: Request):
    require_auth(request)
    today = datetime.now().strftime("%Y-%m-%d")
    with closing(db()) as c:
        campaigns_seed(c)
        rows = [dict(r) for r in c.execute("SELECT * FROM social_campaigns ORDER BY id")]
        used = {r[0]: r[1] for r in c.execute(
            "SELECT campaign_id, COUNT(*) FROM social_queue WHERE campaign_id IS NOT NULL "
            "AND state IN ('scheduled','publishing','published','used') GROUP BY campaign_id")}
        saved = {r[0]: r[1] for r in c.execute(
            "SELECT campaign_id, COUNT(*) FROM social_queue WHERE campaign_id IS NOT NULL "
            "AND state='draft' GROUP BY campaign_id")}
    for r in rows:
        r["posts"] = used.get(r["id"], 0)
        r["saved"] = saved.get(r["id"], 0)
        r["live"] = not ((r["starts"] and today < r["starts"]) or (r["ends"] and today > r["ends"]))
    rules, banned = caption_rules()
    return {"rows": rows, "rules": rules, "banned": banned, "writer": caption_writer_status(),
            "ai_note": ai_note_setting()}


class CampaignBody(BaseModel):
    id: Optional[int] = None
    name: str = Field(..., min_length=2, max_length=80)
    offer: str = ""
    details: str = ""
    audience: str = ""
    cta: str = ""
    link: str = ""
    hashtags: str = ""
    picture_idea: str = ""
    starts: str = ""
    ends: str = ""


@app.post("/api/social/campaigns")
def social_campaign_save(request: Request, body: CampaignBody):
    require_auth(request)
    d = captions.clean_campaign(body.dict())
    if not d["name"]:
        raise HTTPException(400, "Give the campaign a name.")
    if d["starts"] and d["ends"] and d["ends"] < d["starts"]:
        raise HTTPException(400, "The end date is before the start date.")
    with closing(db()) as c:
        campaigns_seed(c)
        if body.id:
            _campaign_row(c, body.id)
            c.execute("UPDATE social_campaigns SET name=?, offer=?, details=?, audience=?, cta=?, "
                      "link=?, hashtags=?, picture_idea=?, starts=?, ends=?, updated_at=? WHERE id=?",
                      tuple(d[k] for k in captions.FIELDS) + (now(), body.id))
            cid = body.id
        else:
            c.execute("INSERT INTO social_campaigns (name, offer, details, audience, cta, link, "
                      "hashtags, picture_idea, starts, ends, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      tuple(d[k] for k in captions.FIELDS) + (now(), now()))
            cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        row = _campaign_row(c, cid)
    return {"ok": True, "campaign": row}


@app.post("/api/social/campaigns/{cid}/remove")
def social_campaign_remove(request: Request, cid: int):
    """Scheduled posts keep their words; they just stop counting toward it."""
    require_auth(request)
    with closing(db()) as c:
        _campaign_row(c, cid)
        c.execute("DELETE FROM social_campaigns WHERE id=?", (cid,))
        c.commit()
    return {"ok": True}


def campaign_for(cid) -> tuple:
    """(id, utm_campaign) for a post, or (None, "") - a campaign deleted
    between writing and posting is not worth failing a post over."""
    if not cid:
        return None, ""
    with closing(db()) as c:
        r = c.execute("SELECT id, name FROM social_campaigns WHERE id=?", (cid,)).fetchone()
    return (r["id"], captions.slug(r["name"])) if r else (None, "")


def with_link(text: str, link: str) -> str:
    """The tracked link on the end of the words, once."""
    if not link or link in (text or ""):
        return text
    return (text or "").rstrip() + "\n\n" + link


def ig_link_text(link: str) -> str:
    """Instagram never makes a caption link clickable, so tracking tags on it
    only make it long and ugly. Show the plain address people can type:
    homerepair.tech/checkyourhomescore - no https, no utm_*."""
    u = urllib.parse.urlsplit((link or "").strip())
    if not u.netloc:
        return (link or "").strip()
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if not k.lower().startswith("utm_")]
    out = u.netloc.lower().removeprefix("www.") + u.path.rstrip("/")
    return out + ("?" + urllib.parse.urlencode(q) if q else "")


class CaptionRulesBody(BaseModel):
    rules: str = Field("", max_length=3000)
    banned: str = Field("", max_length=1000)


@app.post("/api/social/caption_rules")
def social_caption_rules(request: Request, body: CaptionRulesBody):
    require_auth(request)
    set_setting("caption_rules", body.rules.strip())
    set_setting("caption_banned", body.banned.strip())
    rules, banned = caption_rules()
    return {"ok": True, "rules": rules, "banned": banned}


class CaptionBody(BaseModel):
    campaign_id: Optional[int] = None
    image_url: str = ""
    picture: str = Field("", max_length=2000)   # what the picture shows, if known
    angle: int = 0                              # "write another" steps this


def caption_picture(image_url: str) -> tuple:
    """(jpeg bytes or None, what we know it shows). Only pictures in our own
    media store are read - never an arbitrary URL off the internet. A
    generated picture's prompt and a library picture's name come along."""
    m = MEDIA_ID_RX.search((image_url or "").strip())
    if not m:
        return None, ""
    mid = m.group(1)
    try:
        path, _ = mstore.resolve(mid, DATA)
        raw = path.read_bytes()
    except mstore.MediaError:
        return None, ""
    hint = ""
    with closing(db()) as c:
        r = c.execute("SELECT prompt FROM imagegen_log WHERE media_id=? AND ok=1 "
                      "ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
        if r and r["prompt"]:
            hint = r["prompt"]
        else:
            r = c.execute("SELECT name FROM media_library WHERE media_id=?", (mid,)).fetchone()
            if r and r["name"]:
                hint = "a photo named %s" % r["name"]
    try:                                   # small is plenty to read a picture, and cheap
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((768, 768))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        raw = buf.getvalue()
    except Exception:
        pass
    return raw, hint


@app.post("/api/social/caption")
def social_caption(request: Request, body: CaptionBody):
    """Write the post text. AI with the workspace's own key when it can,
    the template when it can't - never an error the post box has to show."""
    require_auth(request)
    camp_row = None
    if body.campaign_id:
        with closing(db()) as c:
            camp_row = _campaign_row(c, body.campaign_id)
    image, hint = caption_picture(body.image_url)
    picture = body.picture.strip() or hint
    cfg = caption_cfg()
    rules, banned = caption_rules()
    out = captions.write(social_business(), camp_row, cfg=cfg, image=image, picture=picture,
                         rules=rules, banned=banned, angle=max(0, int(body.angle or 0)),
                         model_override=setting("caption_model", "").strip(),
                         month=datetime.now().month)
    if camp_row:
        out["campaign"] = {"id": camp_row["id"], "name": camp_row["name"],
                           "link": camp_row["link"], "picture_idea": camp_row["picture_idea"]}
    return out


# ── saved posts: make one, save it, start the next ───────────────────
# Daniel: "after the image is created need to be able to save it and go to
# the next post." A saved post is a social_queue row in state 'draft': no
# time, never published by the loop. Scheduling it turns that same row into
# a scheduled one; posting it by hand marks it 'used'.

class PostDraftBody(BaseModel):
    id: Optional[int] = None
    text: str = Field("", max_length=2200)
    image_url: str = Field("", max_length=2000)
    link: str = Field("", max_length=500)
    campaign_id: Optional[int] = None


def library_keep(image_url: str, name: str = "") -> None:
    """A picture used in a saved post is kept in Your pictures, so it can be
    found again after it scrolls out of 'Recent'. Only our own media."""
    m = MEDIA_ID_RX.search((image_url or "").strip())
    if not m:
        return
    try:
        path, _ = mstore.resolve(m.group(1), DATA)
    except mstore.MediaError:
        return
    with closing(db()) as c:
        if not name:
            r = c.execute("SELECT prompt FROM imagegen_log WHERE media_id=? AND ok=1 ORDER BY id DESC LIMIT 1",
                          (m.group(1),)).fetchone()
            name = ("Made: " + " ".join((r["prompt"] or "").split()[:6])) if r else "Saved picture"
        raw = path.read_bytes()
        saved = {"id": m.group(1), "bytes": len(raw)}
        saved.update(mstore.dimensions(raw, "image/jpeg"))
        _library_row(c, saved, library.clean_name(name)[:80], "made", "")
        c.commit()


@app.get("/api/social/drafts")
def social_drafts(request: Request):
    require_auth(request)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT q.id, q.text, q.image_url, q.link, q.campaign_id, q.created_at, "
            "c.name AS campaign FROM social_queue q LEFT JOIN social_campaigns c ON c.id=q.campaign_id "
            "WHERE q.state='draft' ORDER BY q.id DESC LIMIT 100")]
    for r in rows:
        m = MEDIA_ID_RX.search(r["image_url"] or "")
        r["thumb"] = ("/media/" + m.group(1)) if m else (r["image_url"] or "")
    return {"rows": rows}


@app.post("/api/social/drafts")
def social_draft_save(request: Request, body: PostDraftBody):
    require_auth(request)
    text, img = body.text.strip(), body.image_url.strip()
    if not (text or img):
        raise HTTPException(400, "Nothing to save yet - add a picture or some words.")
    cid = campaign_for(body.campaign_id)[0]
    with closing(db()) as c:
        row = (c.execute("SELECT id FROM social_queue WHERE id=? AND state='draft'", (body.id,)).fetchone()
               if body.id else None)
        if row:
            c.execute("UPDATE social_queue SET text=?, image_url=?, link=?, campaign_id=? WHERE id=?",
                      (text, img, body.link.strip(), cid, row["id"]))
            did = row["id"]
        else:
            c.execute("INSERT INTO social_queue (networks, text, image_url, link, state, created_at, "
                      "campaign_id) VALUES ('[]',?,?,?,'draft',?,?)",
                      (text, img, body.link.strip(), now(), cid))
            did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        n = c.execute("SELECT COUNT(*) FROM social_queue WHERE state='draft'").fetchone()[0]
    try:
        library_keep(img)
    except Exception as e:                      # keeping the picture never costs the save
        print("library_keep skipped: %s: %s" % (type(e).__name__, e))
    return {"ok": True, "id": did, "saved": n, "updated": bool(row)}


@app.post("/api/social/drafts/{did}/remove")
def social_draft_remove(request: Request, did: int):
    require_auth(request)
    with closing(db()) as c:
        c.execute("DELETE FROM social_queue WHERE id=? AND state='draft'", (did,))
        c.commit()
    return {"ok": True}


@app.post("/api/social/drafts/{did}/used")
def social_draft_used(request: Request, did: int):
    """Posted by hand from the post box - off the saved list, kept for the record."""
    require_auth(request)
    with closing(db()) as c:
        c.execute("UPDATE social_queue SET state='used', published_at=? WHERE id=? AND state='draft'",
                  (now(), did))
        c.commit()
    return {"ok": True}


class LibraryKeepBody(BaseModel):
    image_url: str = Field(..., min_length=10, max_length=2000)
    name: str = ""


@app.post("/api/social/library/keep")
def library_keep_route(request: Request, body: LibraryKeepBody):
    """'Save to my pictures' under a made picture."""
    require_auth(request)
    if not MEDIA_ID_RX.search(body.image_url):
        raise HTTPException(400, "Only pictures made or added here can be kept.")
    library_keep(body.image_url, body.name.strip())
    return {"ok": True}


# ── a separate key just for writing ──────────────────────────────────
# Pictures and words don't have to come from the same place. Daniel's Meta
# account makes pictures but its text models answer 402 until billing is
# verified; a Gemini key (Google AI Pro's Cloud credit) can write meanwhile.
class CaptionWriterBody(BaseModel):
    provider: str = ""
    api_key: str = ""


@app.post("/api/social/caption_writer")
def social_caption_writer(request: Request, body: CaptionWriterBody):
    require_auth(request)
    prov = body.provider.strip()
    if not prov:                                # clear it: back to the picture key
        set_setting("caption_provider", ""); set_setting("caption_api_key", "")
        return {"ok": True, "writer": caption_writer_status()}
    if not captions.can_write(prov):
        raise HTTPException(400, "Pick Gemini, Meta or OpenAI for writing.")
    if not body.api_key.strip() and not setting("caption_api_key", "").strip():
        raise HTTPException(400, "Paste the API key.")
    set_setting("caption_provider", prov)
    if body.api_key.strip():
        set_setting("caption_api_key", body.api_key.strip())
    return {"ok": True, "writer": caption_writer_status()}


def caption_cfg():
    """The config the writer uses: its own key if one is saved, otherwise the
    picture key. None when neither can write."""
    prov, key = setting("caption_provider", "").strip(), setting("caption_api_key", "").strip()
    try:
        if prov and key:
            return imagegen.config(prov, key, "", "")
        cfg = imagegen.config(setting("imagegen_provider", ""), setting("imagegen_api_key", ""),
                              setting("imagegen_base_url", ""), setting("imagegen_model", ""))
        if cfg["provider"] == "gemini" and "generativelanguage" not in cfg["base_url"]:
            cfg["base_url"] = imagegen.PROVIDERS["gemini"]["base_url"]
        return cfg
    except imagegen.ImageGenError:
        return None


# ── "Picture created with AI." - a line you can switch on and off ─────
# Daniel, after a DM offered to "bypass the AI flag": no hiding it - but a
# plain disclosure line on AI pictures, on by default, switchable per
# business and per post. The line lives in the post text, so what the
# preview shows is exactly what goes out.
AI_NOTE_DEFAULT = "Picture created with AI."


def ai_note_setting() -> dict:
    return {"on": setting("ai_note_on", "1") != "0",
            "text": setting("ai_note_text", "").strip() or AI_NOTE_DEFAULT}


class AiNoteBody(BaseModel):
    on: Optional[bool] = None
    text: Optional[str] = Field(None, max_length=200)


@app.post("/api/social/ai_note")
def social_ai_note(request: Request, body: AiNoteBody):
    require_auth(request)
    if body.on is not None:
        set_setting("ai_note_on", "1" if body.on else "0")
    if body.text is not None:
        set_setting("ai_note_text", body.text.strip())
    return ai_note_setting()


def picture_is_ai(image_url: str) -> bool:
    """Was this picture made by the picture box? Only our own media can be
    known; a picture pasted from elsewhere is treated as not ours to judge."""
    m = MEDIA_ID_RX.search((image_url or "").strip())
    if not m:
        return False
    with closing(db()) as c:
        return bool(c.execute("SELECT 1 FROM imagegen_log WHERE media_id=? AND ok=1 LIMIT 1",
                              (m.group(1),)).fetchone())


@app.get("/api/social/picture_info")
def social_picture_info(request: Request, image_url: str = ""):
    require_auth(request)
    return {"ai": picture_is_ai(image_url), "ai_note": ai_note_setting()}


def caption_writer_status() -> dict:
    own = bool(setting("caption_provider", "").strip() and setting("caption_api_key", "").strip())
    cfg = caption_cfg()
    prov = cfg["provider"] if cfg else ""
    ok = bool(cfg and captions.can_write(prov))
    label = imagegen.PROVIDERS[prov]["label"] if prov in imagegen.PROVIDERS else ""
    return {"ai": ok, "provider": prov, "label": label, "own_key": own,
            "own_provider": setting("caption_provider", "").strip() if own else "",
            "why_not": "" if ok else (
                "No AI key for writing yet - captions come from the template." if not cfg else
                "%s doesn't write text - captions come from the template." % label)}


class QueueBody(BaseModel):
    networks: list
    text: str = ""
    image_url: str = ""
    link: str = ""
    first_comment: str = ""
    when: str                       # ISO 8601 with offset, or UTC 'Z'
    campaign_id: Optional[int] = None
    draft_id: Optional[int] = None  # a saved post being scheduled - it becomes this row


def _parse_when(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "That is not a date and time I can read.")
    if dt.tzinfo is None:
        raise HTTPException(400, "The time needs a timezone offset - the browser sends one.")
    return dt.astimezone(timezone.utc)


def social_connected() -> dict:
    return {"facebook": bool(setting("fb_page_id", "").strip() and
                             setting("fb_page_token", "").strip()),
            "instagram": bool(setting("ig_user_id", "").strip() and
                              (setting("ig_token", "").strip() or
                               setting("fb_page_token", "").strip()))}


@app.post("/api/social/queue")
def social_queue_add(request: Request, body: QueueBody):
    """Schedule a post. This is the approval - a person chose the words, the
    picture and the time, and pressed the button. The loop publishes it then
    and never anything else."""
    require_auth(request)
    nets = [n.strip().lower() for n in (body.networks or []) if n and n.strip()]
    nets = [n for n in nets if n in ("facebook", "instagram")]
    if not nets:
        raise HTTPException(400, "Pick at least one of Facebook or Instagram.")
    conn = social_connected()
    off = [n for n in nets if not conn[n]]
    if off:
        raise HTTPException(400, "%s %s not connected yet - see the status at the top of "
                                 "the Social tab." % (" and ".join(off).title(),
                                                      "is" if len(off) == 1 else "are"))
    when = _parse_when(body.when)
    if when < datetime.now(timezone.utc) + timedelta(minutes=2):
        raise HTTPException(400, "Pick a time at least a couple of minutes from now - "
                                 "for right now, use the Post buttons.")
    if when > datetime.now(timezone.utc) + timedelta(days=90):
        raise HTTPException(400, "That is more than 90 days out.")
    media = [body.image_url] if body.image_url else []
    chk = soc.check_post(nets, body.text, media)
    if not chk["ok"]:
        probs = "; ".join(p for n in chk["blocked"] for p in chk["by_network"][n]["problems"])
        raise HTTPException(400, probs)
    base = public_base()
    if body.image_url and base and body.image_url.startswith(base):
        mc_ = media_check(request)
        if not mc_["ok"]:
            raise HTTPException(400, mc_["problem"])
    cid = campaign_for(body.campaign_id)
    link = socialkit.utm(body.link, nets[0], cid[1] or "post") if body.link else ""
    with closing(db()) as c:
        draft = (c.execute("SELECT id FROM social_queue WHERE id=? AND state='draft'",
                           (body.draft_id,)).fetchone() if body.draft_id else None)
        if draft:
            # the saved post becomes the scheduled one - no leftover copy in "Saved posts"
            c.execute("UPDATE social_queue SET networks=?, text=?, image_url=?, link=?, first_comment=?, "
                      "when_at=?, state='scheduled', campaign_id=? WHERE id=?",
                      (json.dumps(nets), body.text.strip(), body.image_url.strip(), link,
                       body.first_comment.strip(), when.isoformat(timespec="seconds"), cid[0], draft["id"]))
            qid = draft["id"]
        else:
            c.execute("INSERT INTO social_queue (networks, text, image_url, link, first_comment, "
                      "when_at, state, created_at, campaign_id) VALUES (?,?,?,?,?,?, 'scheduled', ?, ?)",
                      (json.dumps(nets), body.text.strip(), body.image_url.strip(), link,
                       body.first_comment.strip(), when.isoformat(timespec="seconds"), now(), cid[0]))
            qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    return {"ok": True, "id": qid, "when": when.isoformat(timespec="seconds"),
            "networks": nets, "link": link}



# ── the social planner list ──────────────────────────────────────────────
# Every post in one table - caption, picture, network, status, when - with
# what it did once it went out (reach, likes, comments, shares) pulled from
# Meta at most once an hour per post and kept on the row.
STATS_TTL_MIN = 60


def _social_stats_for(row: dict) -> dict:
    """Ask Meta about one published row. {'facebook': {...}, 'instagram': {...}}."""
    try:
        res = json.loads(row.get("result") or "{}")
    except ValueError:
        res = {}
    out = {}
    fbr = (res.get("facebook") or {})
    if fbr.get("ok"):
        pid = ((fbr.get("post") or {}).get("post_id") or (fbr.get("post") or {}).get("id") or "")
        tok = setting("fb_page_token", "").strip()
        if pid and tok:
            try:
                out["facebook"] = fb.post_stats(pid, tok)
            except Exception as e:
                out["facebook"] = {"error": str(e)[:160]}
    igr = (res.get("instagram") or {})
    if igr.get("ok"):
        mid = (igr.get("media") or {}).get("id") or ""
        tok = setting("ig_token", "").strip() or setting("fb_page_token", "").strip()
        if mid and tok:
            try:
                out["instagram"] = insta.media_stats(mid, tok)
            except Exception as e:
                out["instagram"] = {"error": str(e)[:160]}
    return out


@app.get("/api/social/posts")
def social_posts(request: Request, state: str = "", q: str = "", days: int = 90, page: int = 1, per: int = 50,
                 refresh: int = 0):
    require_auth(request)
    conds, args = ["COALESCE(when_at, created_at) >= ?"], [(datetime.now(timezone.utc) - timedelta(days=max(7, min(int(days or 90), 730)))).isoformat(timespec="seconds")]
    if state:
        conds.append("state=?"); args.append(state)
    if q:
        conds.append("(text LIKE ? OR first_comment LIKE ?)"); args += ["%%%s%%" % q] * 2
    where = " WHERE " + " AND ".join(conds)
    per = max(10, min(int(per or 50), 200)); page = max(1, int(page or 1))
    with closing(db()) as c:
        total = c.execute("SELECT COUNT(*) FROM social_queue" + where, args).fetchone()[0]
        rows = [dict(r) for r in c.execute("SELECT * FROM social_queue" + where + " ORDER BY COALESCE(when_at, created_at) DESC LIMIT ? OFFSET ?",
                                           args + [per, (page - 1) * per])]
        counts = {r[0]: r[1] for r in c.execute("SELECT state, COUNT(*) FROM social_queue GROUP BY state")}
        stale_cut = (datetime.now(timezone.utc) - timedelta(minutes=STATS_TTL_MIN)).isoformat(timespec="seconds")
        for r in rows:
            if r["state"] == "published" and (refresh or not r.get("stats_at") or r["stats_at"] < stale_cut):
                st = _social_stats_for(r)
                if st:
                    r["stats"] = json.dumps(st); r["stats_at"] = now()
                    c.execute("UPDATE social_queue SET stats=?, stats_at=? WHERE id=?", (r["stats"], r["stats_at"], r["id"]))
        c.commit()
    out = []
    for r in rows:
        try:
            nets = json.loads(r.get("networks") or "[]")
        except ValueError:
            nets = []
        try:
            stats = json.loads(r.get("stats") or "{}")
        except ValueError:
            stats = {}
        tot = {"reach": 0, "likes": 0, "comments": 0, "shares": 0}
        for v in stats.values():
            for k in tot:
                if isinstance(v.get(k), int):
                    tot[k] += v[k]
        out.append({"id": r["id"], "text": r["text"], "image_url": r["image_url"], "link": r["link"], "networks": nets,
                    "state": r["state"], "when_at": r["when_at"], "published_at": r["published_at"], "error": r["error"],
                    "source": r.get("source") or "", "kind": r.get("kind") or "", "stats": stats, "totals": tot,
                    "stats_at": r.get("stats_at") or ""})
    return {"rows": out, "total": total, "page": page, "per": per, "pages": max(1, -(-total // per)), "counts": counts,
            "summary": {k: sum(x["totals"][k] for x in out) for k in ("reach", "likes", "comments", "shares")}}



# ── reputation ───────────────────────────────────────────────────────────
REP_SNAPSHOT_DAYS = 7


def reputation_refresh(c, key: str = "", force: bool = False) -> dict:
    """Pull the listing, store new reviews with a reply drafted, and add a
    snapshot row if the last one is older than a week (or force)."""
    pid = setting("own_place_id", "").strip()
    key = key or google_key()
    if not pid:
        raise HTTPException(400, "Pick your Google listing first (Reputation → Find my listing).")
    if not key:
        raise HTTPException(400, "Lead finding needs a Google Maps key. Add one under Setup → Lead finder.")
    if not reserve_request():
        raise HTTPException(429, "This month's free Google requests are used up. Raise the cap under Find leads.")
    try:
        L = rep.fetch_listing(pid, key)
    except rep.ListingError as e:
        raise HTTPException(502, str(e))
    business = setting("sender_company", "").strip() or ws.info()["name"]
    sender = setting("sender_name", "").strip()
    phone = setting("sender_phone", "").strip()
    new = 0
    for r in L["reviews"]:
        r["sentiment"] = rep.sentiment(r["rating"], r["text"])
        draft = rep.reply_draft(r, business, sender, phone)
        cur = c.execute("SELECT id FROM reviews WHERE review_id=?", (r["review_id"],)).fetchone()
        if cur:
            c.execute("UPDATE reviews SET when_text=? WHERE id=?", (r["when"], cur["id"]))
        else:
            c.execute("INSERT INTO reviews (review_id, author, rating, text, at, when_text, sentiment, reply_draft, seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (r["review_id"], r["author"], r["rating"], r["text"], r["at"], r["when"], r["sentiment"], draft, now())); new += 1
    last = c.execute("SELECT at FROM reputation_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    cut = (datetime.now(timezone.utc) - timedelta(days=REP_SNAPSHOT_DAYS)).isoformat(timespec="seconds")
    if force or not last or last["at"] < cut:
        c.execute("INSERT INTO reputation_snapshots (at, rating, count) VALUES (?,?,?)", (now(), L["rating"], L["count"]))
    for k, v in (("own_listing_name", L["name"]), ("own_rating", str(L["rating"])), ("own_review_count", str(L["count"])), ("own_listing_at", now())):
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit()
    return {"listing": L, "new_reviews": new}


def reputation_due():
    """Weekly, from the competitor loop: a snapshot for any workspace that
    has picked its listing. Quiet on failure; the page shows the last date."""
    if not setting("own_place_id", "").strip() or not google_key():
        return
    with closing(db()) as c:
        last = c.execute("SELECT at FROM reputation_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        cut = (datetime.now(timezone.utc) - timedelta(days=REP_SNAPSHOT_DAYS)).isoformat(timespec="seconds")
        if last and last["at"] >= cut:
            return
        try:
            reputation_refresh(c)
        except HTTPException as e:
            print("reputation [%s]: %s" % (ws.current(), e.detail))


@app.get("/api/reputation")
def reputation_get(request: Request):
    require_auth(request)
    pid = setting("own_place_id", "").strip()
    with closing(db()) as c:
        snaps = [dict(r) for r in c.execute("SELECT at, rating, count FROM reputation_snapshots ORDER BY id")]
        reviews = [dict(r) for r in c.execute("SELECT * FROM reviews ORDER BY at DESC, id DESC LIMIT 50")]
        reqs = [dict(r) for r in c.execute("SELECT * FROM review_requests ORDER BY id DESC LIMIT 50")]
        n_req = c.execute("SELECT COUNT(*) FROM review_requests WHERE state='sent'").fetchone()[0]
    return {"place_id": pid, "name": setting("own_listing_name", ""), "rating": float(setting("own_rating", "") or 0),
            "count": int(setting("own_review_count", "") or 0), "checked_at": setting("own_listing_at", ""),
            "review_url": rep.review_link(pid), "snapshots": snaps, "trend": rep.trend(snaps),
            "reviews": reviews, "split": rep.split(reviews), "requests": reqs, "requests_sent": n_req,
            "can_text": bool(setting("telnyx_api_key", "").strip() and setting("telnyx_from_number", "").strip()),
            "can_email": bool(mailbox_creds()[0]), "has_key": bool(google_key())}


class ListingBody(BaseModel):
    place_id: str = ""
    query: str = ""


@app.post("/api/reputation/listing")
def reputation_listing(request: Request, body: ListingBody):
    """Set the business's own listing by place id, or search for it by
    name + city and return candidates to pick from."""
    require_auth(request)
    key = google_key()
    if not key:
        raise HTTPException(400, "Lead finding needs a Google Maps key. Add one under Setup → Lead finder.")
    if body.place_id.strip():
        set_setting("own_place_id", body.place_id.strip())
        with closing(db()) as c:
            c.execute("DELETE FROM reviews"); c.execute("DELETE FROM reputation_snapshots"); c.commit()   # a different listing, a fresh history
            out = reputation_refresh(c, key, force=True)
        return {"ok": True, "listing": out["listing"]}
    q = body.query.strip() or " ".join(x for x in (setting("sender_company", "").strip() or ws.info()["name"], setting("default_city", "").strip()) if x)
    found = places_search(q, key, pages=1)
    return {"ok": True, "candidates": [{"place_id": p.get("id"), "name": (p.get("displayName") or {}).get("text", ""),
                                        "address": p.get("formattedAddress", ""), "rating": p.get("rating"), "count": p.get("userRatingCount")}
                                       for p in found[:8]]}


@app.post("/api/reputation/refresh")
def reputation_refresh_route(request: Request):
    require_auth(request)
    with closing(db()) as c:
        out = reputation_refresh(c, force=True)
    return {"ok": True, "new_reviews": out["new_reviews"], "rating": out["listing"]["rating"], "count": out["listing"]["count"]}


class ReviewMarkBody(BaseModel):
    replied: bool = True


@app.post("/api/reputation/review/{rid}/replied")
def reputation_review_replied(request: Request, rid: int, body: ReviewMarkBody):
    require_auth(request)
    with closing(db()) as c:
        c.execute("UPDATE reviews SET replied_at=? WHERE id=?", (now() if body.replied else "", rid)); c.commit()
    return {"ok": True}


class ReviewRequestBody(BaseModel):
    customer: str = ""
    phone: str = ""
    email: str = ""
    channel: str = "text"      # text | email
    message: str = ""          # optional edit of the default


@app.post("/api/reputation/request")
def reputation_request(request: Request, body: ReviewRequestBody):
    """Ask a customer for a Google review, by text or email, with the
    listing's write-a-review link. One press, one message."""
    require_auth(request)
    pid = setting("own_place_id", "").strip()
    if not pid:
        raise HTTPException(400, "Pick your Google listing first, so the request has a link to send.")
    link = rep.review_link(pid)
    business = setting("sender_company", "").strip() or ws.info()["name"]
    sender = setting("sender_name", "").strip()
    ch = (body.channel or "text").lower()
    with closing(db()) as c:
        if ch == "text":
            to = telnyx.to_e164(body.phone)
            if not to:
                raise HTTPException(400, "That phone number doesn't look right.")
            if c.execute("SELECT 1 FROM sms_optouts WHERE number=?", (to,)).fetchone():
                raise HTTPException(400, "That number asked us to stop. Nothing goes to it.")
            key, frm = setting("telnyx_api_key", "").strip(), setting("telnyx_from_number", "").strip()
            if not (key and frm):
                raise HTTPException(400, "Texting isn't set up. Check Setup → Phone & text-back.")
            msg = body.message.strip() or rep.request_text(body.customer, business, sender, link)
            try:
                tb._send(api_key=key, from_number=frm, to_number=to, text=msg, messaging_profile_id=setting("telnyx_messaging_profile_id", ""))
            except Exception as e:
                c.execute("INSERT INTO review_requests (customer, phone, channel, message, state, error, at) VALUES (?,?,?,?,?,?,?)",
                          (body.customer, to, "text", msg, "failed", str(e)[:300], now())); c.commit()
                raise HTTPException(502, "The text didn't go out. %s" % str(e)[:160])
            c.execute("INSERT INTO review_requests (customer, phone, channel, message, at) VALUES (?,?,?,?,?)", (body.customer, to, "text", msg, now()))
        else:
            addr = (body.email or "").strip().lower()
            if not auth.looks_like_email(addr):
                raise HTTPException(400, "That email address doesn't look right.")
            host, user, pw = mailbox_creds()
            if not host:
                raise HTTPException(400, "No mailbox is connected, so nothing can send. Connect one under Setup → Your mailbox.")
            subject, dflt = rep.request_email(body.customer, business, sender, link)
            msg = body.message.strip() or dflt
            try:
                m = mailer.build(setting("sender_email", "").strip() or user, sender or business, addr, subject, msg)
                mailer.send(setting("smtp_host", "").strip() or mailer.smtp_host_for(host), int(setting("smtp_port", "") or mailer.DEFAULT_SMTP_PORT),
                            user, pw, m, smtp_factory=_login_smtp_factory)
            except mailer.MailError as e:
                c.execute("INSERT INTO review_requests (customer, email, channel, message, state, error, at) VALUES (?,?,?,?,?,?,?)",
                          (body.customer, addr, "email", msg, "failed", str(e)[:300], now())); c.commit()
                raise HTTPException(502, "The email didn't go out. %s" % str(e)[:160])
            c.execute("INSERT INTO review_requests (customer, email, channel, message, at) VALUES (?,?,?,?,?)", (body.customer, addr, "email", msg, now()))
        c.commit()
    return {"ok": True, "message": msg}



@app.post("/api/social/posts/{qid}/retry")
def social_post_retry(request: Request, qid: int):
    """A failed post back in the queue, to go out on the next tick. Meant
    for after a reconnect: the words and picture are kept as they were."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT state FROM social_queue WHERE id=?", (qid,)).fetchone()
        if not r:
            raise HTTPException(404, "That post isn't here any more.")
        if r["state"] != "failed":
            raise HTTPException(400, "Only a failed post can be retried.")
        c.execute("UPDATE social_queue SET state='scheduled', when_at=?, error='', result='' WHERE id=?", (now(), qid)); c.commit()
    return {"ok": True}

@app.get("/api/social/queue")
def social_queue_list(request: Request, limit: int = 30):
    require_auth(request)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM social_queue WHERE state NOT IN ('draft','used') "
            "ORDER BY CASE state WHEN 'scheduled' THEN 0 ELSE 1 END, "
            "when_at DESC LIMIT ?", (max(1, min(limit, 100)),))]
    for r in rows:
        try:
            r["networks"] = json.loads(r["networks"] or "[]")
        except Exception:
            r["networks"] = []
    return {"rows": rows, "connected": social_connected()}


@app.post("/api/social/queue/{qid}/cancel")
def social_queue_cancel(request: Request, qid: int):
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT state FROM social_queue WHERE id=?", (qid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if r["state"] != "scheduled":
            raise HTTPException(400, "That post is already %s." % r["state"])
        c.execute("UPDATE social_queue SET state='cancelled' WHERE id=?", (qid,))
        c.commit()
    return {"ok": True}


def publish_queued(c, row: dict) -> dict:
    """One row, every network it names. Facebook goes out PUBLISHED here -
    the human approval already happened when the row was scheduled.
    Returns {network: {ok, ...}}; raises nothing, the row records it."""
    nets = json.loads(row["networks"] or "[]")
    out = {}
    for n in nets:
        try:
            if n == "facebook":
                page, tok = setting("fb_page_id", "").strip(), setting("fb_page_token", "").strip()
                if not (page and tok):
                    raise fb.FacebookError("Facebook is no longer connected.")
                msg = row["text"] + ("\n\n" + row["link"] if row["link"] and row["image_url"] else "")
                res = fb.publish(page, tok, message=msg, image_url=row["image_url"],
                                 link=row["link"] if not row["image_url"] else "", published=True)
                out[n] = {"ok": True, "post": res}
            elif n == "instagram":
                ig = setting("ig_user_id", "").strip()
                tok = setting("ig_token", "").strip() or setting("fb_page_token", "").strip()
                if not (ig and tok):
                    raise insta.InstagramError("Instagram is no longer connected.")
                if not row["image_url"]:
                    raise insta.InstagramError("Instagram needs a picture.")
                cap = with_link(row["text"], ig_link_text(row["link"])) if row["link"] else row["text"]
                st = insta.stage(ig, tok, image_url=row["image_url"], caption=cap)
                cid = st.get("container_id") or st.get("id")
                insta.wait_ready(cid, tok, tries=6, delay=10)
                res = insta.release(ig, tok, cid)
                out[n] = {"ok": True, "media": res}
        except Exception as e:
            out[n] = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}
    return out


def social_publish_due(slug: str) -> int:
    """Publish every due row in one workspace. Called by the loop with the
    workspace set; callable directly from a test the same way."""
    n = 0
    with closing(db(slug)) as c:
        due = [dict(r) for r in c.execute(
            "SELECT * FROM social_queue WHERE state='scheduled' AND when_at <= ? "
            "ORDER BY when_at", (now(),))]
        for row in due:
            # claim it first: a second tick during a slow Instagram publish
            # must not send the same post twice
            c.execute("UPDATE social_queue SET state='publishing' WHERE id=? AND state='scheduled'",
                      (row["id"],))
            c.commit()
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                continue
            res = publish_queued(c, row)
            ok = all(v.get("ok") for v in res.values()) and bool(res)
            c.execute("UPDATE social_queue SET state=?, result=?, error=?, published_at=? WHERE id=?",
                      ("published" if ok else "failed", json.dumps(res),
                       "" if ok else "; ".join("%s: %s" % (k, v.get("error")) for k, v in res.items()
                                              if not v.get("ok")),
                       now() if ok else "", row["id"]))
            c.commit()
            n += 1
    return n


def _social_loop(every: int = 30):
    """Daemon: every `every` seconds, every workspace, publish what is due.
    A workspace that throws does not stop the others."""
    while True:
        beat("social-queue")
        time.sleep(every)
        for slug in list(ws.WORKSPACES):
            tok = ws.CURRENT.set(slug)
            try:
                social_publish_due(slug)
            except Exception as e:
                print("social loop [%s]: %s: %s" % (slug, type(e).__name__, e))
            finally:
                ws.CURRENT.reset(tok)


# 53 leads came in (the ChiroCare list, some hand-added ones) with a broad
# group but no trade, so "pick a trade" on the Pipeline couldn't find them -
# 37 chiropractors, none under "Chiropractor". Name words are enough for the
# obvious ones; anything unclear stays blank and shows as "No trade set".
CATEGORY_FROM_NAME = [
    (r"chiropract", "Chiropractor"),
    (r"urgent care|medclinic|immediate care|emergency room|\bER\b", "Urgent Care"),
    (r"pizz", "Pizza / Italian"),
    (r"car ?wash", "Car Wash / Detail"),
    (r"bakery|panader", "Bakery Cafe"),
    (r"brew|taproom|\bpub\b|monk", "Bar / Gastropub"),
    (r"taquer", "Taqueria"),
    (r"grill|restaurant|kitchen|shrimp|seafood|cafe|café|diner|bbq|barbecue|cantina|bistro", "Full-Service Restaurant"),
]
VERTICAL_CATEGORY = {"chiro": "Chiropractor"}


def infer_category(company: str, vertical: str = "") -> str:
    for rx, cat in CATEGORY_FROM_NAME:
        if re.search(rx, company or "", re.I):
            return cat
    return VERTICAL_CATEGORY.get(vertical or "", "")


def backfill_categories(c) -> int:
    n = 0
    for pid, company, vertical in c.execute(
            "SELECT id, company, vertical FROM prospects WHERE COALESCE(category,'')=''").fetchall():
        cat = infer_category(company, vertical)
        if cat:
            c.execute("UPDATE prospects SET category=? WHERE id=?", (cat, pid))
            n += 1
    return n


def drop_placeholder_addresses(c) -> int:
    """Clear made-up addresses (email@email.com) off leads and contacts and
    retire any draft written to one. Returns drafts retired."""
    for tbl in ("prospects", "contacts"):
        for rid, em in c.execute("SELECT id, email FROM %s WHERE COALESCE(email,'')<>''" % tbl).fetchall():
            if rsrch.is_placeholder_email(em):
                c.execute("UPDATE %s SET email='' WHERE id=?" % tbl, (rid,))
    n = 0
    for oid, to in c.execute("SELECT id, to_addr FROM outreach WHERE state IN ('draft','approved')").fetchall():
        if to and rsrch.is_placeholder_email(to):
            c.execute("UPDATE outreach SET state='skipped', decided_at=?, note=? WHERE id=?",
                      (now(), "Retired: %s is a placeholder address from their website, not a real inbox" % to, oid))
            n += 1
    return n


def redraft_untouched(c, only_offer: str = "") -> int:
    """Rewrite first-touch drafts nobody has edited, with today's copy - so a
    copy change shows up in the Outbox instead of only on tomorrow's drafts.
    A draft whose body differs from what was composed is his, and is left
    alone. Returns how many were rewritten."""
    q = ("SELECT o.id, o.prospect_id, o.variant, o.to_addr, o.body, o.body_original, o.offer, "
         "COALESCE(o.step,1) AS step FROM outreach o WHERE o.state='draft'")
    n = 0
    for r in c.execute(q).fetchall():
        if (r["body_original"] or "") and (r["body"] or "") != (r["body_original"] or ""):
            continue                                    # edited - his words now
        p = c.execute("SELECT * FROM prospects WHERE id=?", (r["prospect_id"],)).fetchone()
        if not p:
            continue
        d = row_to_dict(p)
        if r["step"] > 1:
            # a follow-up: same sequence, same angle, the later email
            seg_, sig_ = seq.parse_variant(r["variant"])
            if not seg_:
                continue
            e = compose_homerepair_followup(d, contact_rows(c, r["prospect_id"]), seg_,
                                            None if sig_ in ("board", "none", "") else sig_, r["step"])
            if not e:
                continue
        else:
            e = compose_email(d, contact_rows(c, r["prospect_id"]), variant=r["variant"], to_addr=r["to_addr"] or "")
        if only_offer and e["offer"] != only_offer:
            continue
        if e["body"] == r["body"]:
            continue
        c.execute("UPDATE outreach SET subject=?, body=?, subject_original=?, body_original=?, offer=? WHERE id=?",
                  (e["subject"], e["body"], e["subject"], e["body"], e["offer"], r["id"]))
        n += 1
    return n


COPY_VERSION = "2026-09-28-plain-voice"   # HomeRepair sequences rewritten; Watson artifact opener      # bump when the copy changes and waiting drafts should follow


# ── the week on autopilot ────────────────────────────────────────────
# Daniel chose "full autopilot": build the week, make the pictures, schedule
# it to go out, and let him cancel or change anything before it does.
# Turning autopilot on IS the approval for what it schedules - the same way
# pressing Schedule is - and every post sits in the list with Edit, Swap
# and Cancel until its minute comes. socialweek.py writes the posts.
AUTO_LOCK = threading.Lock()
AUTO_RUNNING: set = set()


def _fetch_as_meta(url):
    """(status, text) the way Meta's fetcher asks: its user agent, no redirects."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": mstore.FETCHER_UA})
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        where = e.headers.get("Location", "") if e.headers else ""
        return e.code, ("Location: %s\n" % where) + e.read(4000).decode("utf-8", "replace")


def hosting_problem_now() -> str:
    return mstore.hosting_problem(public_base(), _fetch_as_meta)


def auto_status() -> dict:
    try:
        return json.loads(setting("social_auto_status", "") or "{}")
    except ValueError:
        return {}


def _auto_status_set(**kw) -> None:
    st = auto_status()
    st.update(kw)
    set_setting("social_auto_status", json.dumps(st))


def auto_built_weeks() -> list:
    try:
        return json.loads(setting("social_auto_weeks", "") or "[]")
    except ValueError:
        return []


def _local_when(date_iso: str, hhmm: str) -> datetime:
    tz = workspace_tz()
    y, m, d = (int(x) for x in date_iso.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(timezone.utc)


def live_campaigns() -> list:
    today = datetime.now(workspace_tz()).strftime("%Y-%m-%d")
    with closing(db()) as c:
        campaigns_seed(c)
        rows = [dict(r) for r in c.execute("SELECT * FROM social_campaigns ORDER BY id")]
    return [r for r in rows if not ((r["starts"] and today < r["starts"]) or (r["ends"] and today > r["ends"]))]


def build_week(week: str, only_slot: str = "", variant: int = 0, mark_built: bool = True) -> dict:
    """Build (the rest of) one week: picture, words, schedule - for every slot
    still in the future that doesn't already have a post. Call with the
    workspace set. Never raises; what went wrong is in the result."""
    biz = social_business()
    posts = socialweek.plan(week, ws.current(), biz, live_campaigns(), variant=variant)
    if only_slot:
        posts = [p for p in posts if p["slot"] == only_slot]
    conn = social_connected()
    hosting = hosting_problem_now() if (conn["facebook"] or conn["instagram"]) else "not connected"
    pictures_ok = bool(setting("imagegen_provider", "").strip() and setting("imagegen_api_key", "").strip())
    cfg = caption_cfg()
    ai_writer = bool(cfg and captions.can_write(cfg["provider"]))
    rules, banned = caption_rules()
    note = ai_note_setting()
    soon = datetime.now(timezone.utc) + timedelta(minutes=20)
    made, drafts, skipped, problems = 0, 0, 0, []
    todo = []
    with closing(db()) as c:
        for p in posts:
            when = _local_when(p["date"], p["time"])
            taken = c.execute("SELECT 1 FROM social_queue WHERE slot=? AND state IN "
                              "('scheduled','publishing','published','draft','used') LIMIT 1",
                              (p["slot"],)).fetchone()
            if when < soon or taken:
                skipped += 1
                continue
            todo.append((p, when))
    _auto_status_set(running=True, week=week, total=len(todo), done=0, started=now(), problems=[])
    camps = {r["id"]: r for r in live_campaigns()}
    for n, (p, when) in enumerate(todo, 1):
        camp = camps.get(p["campaign_id"])
        pic, pic_err = None, ""
        if pictures_ok:
            try:
                pic = make_picture(p["picture_prompt"])
            except PictureError as e:
                pic_err = str(e)
        text = p["caption"]
        if ai_writer:
            try:
                raw = None
                if pic:
                    raw = caption_picture("/media/" + pic["id"])[0]
                res = captions.write(biz, camp, cfg=cfg, image=raw, picture=p["picture_prompt"],
                                     rules=rules, banned=banned, angle=n, brief=p["brief"],
                                     model_override=setting("caption_model", "").strip(),
                                     month=datetime.now().month)
                if res.get("source") == "ai":
                    text = res["text"]
            except Exception as e:                    # the library caption is always there
                print("autopilot caption [%s]: %s" % (p["slot"], e))
        if pic and note["on"] and note["text"] not in text:
            text = text.rstrip() + "\n\n" + note["text"]
        image_url = (pic or {}).get("url") or ""
        link = (camp or {}).get("link", "") if p["kind"] in ("tip", "offer", "checklist") else ""
        nets = [k for k in ("facebook", "instagram") if conn[k]]
        if not image_url:
            nets = [k for k in nets if k != "instagram"]            # Instagram needs a picture
        why_draft = ""
        if not nets:
            why_draft = ("Facebook and Instagram aren't connected" if not (conn["facebook"] or conn["instagram"])
                         else "no picture, and Instagram needs one")
        elif hosting:
            why_draft = "Facebook and Instagram can't fetch your pictures yet (%s)" % hosting[:120]
        else:
            chk = soc.check_post(nets, text, [image_url] if image_url else [])
            if not chk["ok"]:
                why_draft = "; ".join(q for k in chk["blocked"] for q in chk["by_network"][k]["problems"])[:200]
        state = "draft" if why_draft else "scheduled"
        stored_link = (socialkit.utm(link, nets[0], captions.slug(camp["name"]) if camp else "autopilot")
                       if (link and state == "scheduled") else link)
        with closing(db()) as c:
            c.execute("INSERT INTO social_queue (networks, text, image_url, link, when_at, state, created_at, "
                      "campaign_id, source, plan_week, slot, kind, error) "
                      "VALUES (?,?,?,?,?,?,?,?, 'autopilot', ?,?,?,?)",
                      (json.dumps(nets), text, image_url, stored_link, when.isoformat(timespec="seconds"),
                       state, now(), p["campaign_id"], week, p["slot"], p["kind"],
                       ("Saved, not scheduled: " + why_draft) if why_draft else ""))
            c.commit()
        if state == "draft":
            drafts += 1
            problems.append("%s %s: %s" % (p["day"], p["label"], why_draft))
        else:
            made += 1
        if pic_err:
            problems.append("%s %s: no picture (%s)" % (p["day"], p["label"], pic_err[:140]))
        _auto_status_set(done=n, problems=problems[-6:])
    if mark_built and not only_slot:
        weeks = auto_built_weeks()
        if week not in weeks:
            set_setting("social_auto_weeks", json.dumps((weeks + [week])[-20:]))
    out = {"week": week, "scheduled": made, "saved": drafts, "skipped": skipped, "problems": problems,
           "ai_writer": ai_writer, "pictures": pictures_ok}
    _auto_status_set(running=False, finished=now(), last=out)
    return out


def _auto_run(slug: str, week: str, only_slot: str = "", variant: int = 0) -> bool:
    """Start a build in the background, one per workspace at a time."""
    with AUTO_LOCK:
        if slug in AUTO_RUNNING:
            return False
        AUTO_RUNNING.add(slug)

    def go():
        tok = ws.CURRENT.set(slug)
        try:
            build_week(week, only_slot=only_slot, variant=variant)
        except Exception as e:
            print("social autopilot [%s]: %s: %s" % (slug, type(e).__name__, e))
            _auto_status_set(running=False, error="%s: %s" % (type(e).__name__, str(e)[:200]))
        finally:
            ws.CURRENT.reset(tok)
            with AUTO_LOCK:
                AUTO_RUNNING.discard(slug)
    threading.Thread(target=go, daemon=True, name="social-autopilot-" + slug).start()
    return True


def auto_due_weeks(today: datetime) -> list:
    """Which weeks autopilot should build now: this week if it hasn't been,
    and next week from Friday on."""
    this = socialweek.week_of(today.date())
    out = [] if this in auto_built_weeks() else [this]
    if today.weekday() >= 4:
        nxt = socialweek.week_of((today + timedelta(days=7)).date())
        if nxt not in auto_built_weeks():
            out.append(nxt)
    return out


def _social_auto_loop(every: int = 600):
    while True:
        beat("social-autopilot")
        time.sleep(every)
        for slug in list(ws.WORKSPACES):
            tok = ws.CURRENT.set(slug)
            claimed = False
            try:
                with closing(db()) as c:
                    campaigns_seed(c)                 # also flips the catalog on Oct 1
                if setting("social_auto_on", "") == "1":
                    due = auto_due_weeks(datetime.now(workspace_tz()))
                    if due:
                        with AUTO_LOCK:
                            claimed = slug not in AUTO_RUNNING
                            if claimed:
                                AUTO_RUNNING.add(slug)
                        if claimed:
                            for wk in due:
                                build_week(wk)             # this loop is already in the background
            except Exception as e:
                print("social autopilot loop [%s]: %s: %s" % (slug, type(e).__name__, e))
            finally:
                if claimed:
                    with AUTO_LOCK:
                        AUTO_RUNNING.discard(slug)
                ws.CURRENT.reset(tok)


def _auto_rows(weeks: list) -> list:
    if not weeks:
        return []
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT q.id, q.state, q.text, q.image_url, q.link, q.when_at, q.networks, q.error, q.kind, q.campaign_id, "
            "q.slot, q.plan_week, q.result, c.name AS campaign FROM social_queue q "
            "LEFT JOIN social_campaigns c ON c.id=q.campaign_id "
            "WHERE q.source='autopilot' AND q.plan_week IN (%s) AND q.state NOT IN ('cancelled') "
            "ORDER BY q.when_at" % ",".join("?" * len(weeks)), weeks)]
    for r in rows:
        m = MEDIA_ID_RX.search(r["image_url"] or "")
        r["thumb"] = ("/media/" + m.group(1)) if m else ""
        try:
            r["networks"] = json.loads(r["networks"] or "[]")
        except ValueError:
            r["networks"] = []
        r["label"] = socialweek.LABEL.get(r["kind"] or "", "")
        r.pop("result", None)
    return rows


@app.get("/api/social/autopilot")
def social_autopilot(request: Request):
    require_auth(request)
    today = datetime.now(workspace_tz())
    this = socialweek.week_of(today.date())
    nxt = socialweek.week_of((today + timedelta(days=7)).date())
    conn = social_connected()
    st = auto_status()
    if st.get("running") and ws.current() not in AUTO_RUNNING:
        st["running"] = False                                  # a restart mid-build
    cfg = caption_cfg()
    return {
        "on": setting("social_auto_on", "") == "1",
        "status": st,
        "this_week": this, "next_week": nxt,
        "built": auto_built_weeks(),
        "posts": _auto_rows([this, nxt]),
        "ready": {"pictures": bool(setting("imagegen_provider", "").strip() and setting("imagegen_api_key", "").strip()),
                  "facebook": conn["facebook"], "instagram": conn["instagram"],
                  "writer": bool(cfg and captions.can_write(cfg["provider"])),
                  "campaigns": len(live_campaigns())},
        "slots": [{"kind": k, "day": socialweek.DAY[d], "time": t, "label": socialweek.LABEL[k]}
                  for k, d, t in socialweek.SLOTS],
    }


class AutoBody(BaseModel):
    on: bool


@app.post("/api/social/autopilot")
def social_autopilot_set(request: Request, body: AutoBody):
    """On = build the rest of this week now (and next week from Friday),
    then keep every week filled. Off = nothing new gets built; posts
    already scheduled stay scheduled until you cancel them."""
    require_auth(request)
    set_setting("social_auto_on", "1" if body.on else "")
    started = False
    if body.on:
        due = auto_due_weeks(datetime.now(workspace_tz()))
        if due:
            started = _auto_run(ws.current(), due[0])
    return {"ok": True, "on": body.on, "building": started}


class AutoBuildBody(BaseModel):
    week: str = "this"          # this | next


@app.post("/api/social/autopilot/build")
def social_autopilot_build(request: Request, body: AutoBuildBody):
    require_auth(request)
    today = datetime.now(workspace_tz())
    wk = socialweek.week_of((today + timedelta(days=7 if body.week == "next" else 0)).date())
    if not _auto_run(ws.current(), wk):
        raise HTTPException(409, "Already building - give it a minute.")
    return {"ok": True, "week": wk}


@app.post("/api/social/autopilot/{qid}/swap")
def social_autopilot_swap(request: Request, qid: int):
    """Don't like one? Replace it: a different idea, a new picture, same slot."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT id, slot, plan_week, state FROM social_queue WHERE id=? AND source='autopilot'",
                      (qid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if r["state"] not in ("scheduled", "draft", "failed"):
            raise HTTPException(400, "That post is already %s." % r["state"])
        variant = c.execute("SELECT COUNT(*) FROM social_queue WHERE slot=? AND source='autopilot'",
                            (r["slot"],)).fetchone()[0]
        c.execute("UPDATE social_queue SET state='cancelled' WHERE id=?", (qid,))
        c.commit()
    if not _auto_run(ws.current(), r["plan_week"], only_slot=r["slot"], variant=variant):
        raise HTTPException(409, "Already building - try again in a minute.")
    return {"ok": True}


@app.post("/api/social/queue/{qid}/to_draft")
def social_queue_to_draft(request: Request, qid: int):
    """Edit a scheduled post: it comes off the schedule and into the post box
    as a saved post. Schedule it again when it's right."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT state FROM social_queue WHERE id=?", (qid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if r["state"] not in ("scheduled", "failed"):
            raise HTTPException(400, "That post is already %s." % r["state"])
        c.execute("UPDATE social_queue SET state='draft', error='' WHERE id=?", (qid,))
        c.commit()
    return {"ok": True}


@app.on_event("startup")
def _start_social_loop():
    """Only when the ASGI server actually starts. The tests import this
    module and call functions directly, so they never start a publisher
    against their temp databases."""
    if os.environ.get("JUST_GRIT_NO_LOOP"):
        return
    # Repair queue steps left behind by follow-ups sent before record_sent()
    # learned to move them. Cheap, idempotent, once per start per workspace.
    for slug in list(ws.WORKSPACES):
        tok = ws.CURRENT.set(slug)
        try:
            with closing(db(slug)) as c:
                n = reconcile_email_steps(c)
                c.execute("UPDATE outreach SET state='skipped', decided_at=?, note='Retired: lead was "
                          "already closed' WHERE state IN ('draft','approved') AND prospect_id IN "
                          "(SELECT id FROM prospects WHERE status IN ('booked','dead','corporate'))",
                          (now(),))
                k = c.execute("SELECT changes()").fetchone()[0]
                k += drop_placeholder_addresses(c)
                backfill_categories(c)
                campaigns_seed(c)                     # the Oct 1 catalog switch, if it's time
                if setting("copy_version", "") != COPY_VERSION:
                    r = redraft_untouched(c)
                    c.commit()                  # before set_setting opens its own connection
                    set_setting("copy_version", COPY_VERSION)
                    print("copy [%s]: %d waiting draft(s) rewritten with the new copy" % (slug, r))
                c.commit()
            if n or k:
                print("queue [%s]: %d lead(s) moved past an email step already sent; %d draft(s) "
                      "retired for closed leads" % (slug, n, k))
        except Exception as e:
            print("queue reconcile [%s]: %s: %s" % (slug, type(e).__name__, e))
        finally:
            ws.CURRENT.reset(tok)
    threading.Thread(target=_social_loop, daemon=True, name="social-queue").start()
    threading.Thread(target=_social_auto_loop, daemon=True, name="social-autopilot").start()
    threading.Thread(target=_autopilot_loop, daemon=True, name="email-autopilot").start()
    threading.Thread(target=_competitor_loop, daemon=True, name="competitor-watch").start()
    threading.Thread(target=_refill_loop, daemon=True, name="outbox-refill").start()


# ── keeping the Outbox full ──────────────────────────────────────────
# "It doesn't populate the emails. It should load the leads from Today,
# automatically, after the ones in there are sent out - 50 at a time."
#
# What was actually wrong: 275 leads on file, 48 with an email address, and
# all 48 already written to. Google never returns an email, the background
# scan only scores the homepage, and the button that reads contact pages
# for addresses (/api/find_emails) was never wired to the Outbox. So
# "Refresh leads to email" qualified ten leads that had no address and
# wrote nothing.
#
# The refill does the whole chain: find addresses for leads that have a
# site but no email (contact page included), qualify them, draft - until
# the Outbox holds OUTBOX_TARGET. It runs when the button is pressed and,
# on its own, whenever the Outbox is empty.
OUTBOX_TARGET = 50
REFILL_SITES_PER_PASS = 60          # site reads per run; each is ~2-5s
REFILL_RECHECK_DAYS = 14            # a site with no address is re-read after this
REFILL_AUTO_EVERY = 30 * 60         # seconds between automatic refills
_REFILL = {}                        # slug -> {"running","step","checked","found","qualified","drafted","started","finished","error","auto_at"}


def refill_state(slug: str) -> dict:
    return _REFILL.setdefault(slug, {"running": False, "step": "", "checked": 0, "found": 0,
                                     "qualified": 0, "drafted": 0, "started": "", "finished": "",
                                     "error": "", "auto_at": 0.0, "waiting": 0})


def email_candidates(c, limit: int) -> list:
    """Leads with a site and no address that haven't been read lately -
    in the trades being worked, best leads first."""
    fsql, fargs = focus_sql("p")
    return [dict(r) for r in c.execute(
        """SELECT p.id, p.company, p.domain FROM prospects p
            WHERE COALESCE(p.domain,'') <> '' AND COALESCE(p.email,'') = ''
              AND p.status NOT IN ('booked','dead','cold','corporate','conversation')
              AND (COALESCE(p.email_checked_at,'') = '' OR p.email_checked_at < ?)
              AND NOT EXISTS (SELECT 1 FROM contacts k WHERE k.prospect_id=p.id AND COALESCE(k.email,'')<>'')"""
        + fsql + """
            ORDER BY COALESCE(p.tier,1) DESC, COALESCE(p.lead_score,0) DESC, p.id
            LIMIT ?""",
        [(datetime.now(timezone.utc) - timedelta(days=REFILL_RECHECK_DAYS)).isoformat()] + fargs + [limit])]


def find_email_for(c, pid: int, domain: str, fetch=None) -> str:
    """Read their homepage and contact page for an address on their own
    domain (or one a human linked with mailto:). Records the attempt either
    way. Returns the address, or ''."""
    fetch = fetch or fetch_page
    email = ""
    try:
        d = rsrch.research(domain, fetch, max_pages=3)
        email = (d.get("emails") or [""])[0]
        for i, person in enumerate((d.get("people") or [])[:3]):
            if person.get("name"):
                dup = c.execute("SELECT 1 FROM contacts WHERE prospect_id=? AND lower(name)=?",
                                (pid, person["name"].lower())).fetchone()
                if not dup:
                    c.execute("INSERT INTO contacts (prospect_id, name, role, email, phone, is_primary, note, added_at) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (pid, person["name"], person.get("role", ""), person.get("email", ""), "",
                               1 if i == 0 else 0, "found on their website", now()))
    except Exception:
        email = ""
    c.execute("UPDATE prospects SET email_checked_at=? WHERE id=?", (now(), pid))
    if email:
        c.execute("UPDATE prospects SET email=? WHERE id=? AND COALESCE(email,'')=''", (email, pid))
    return email


# ── find the person ──────────────────────────────────────────────────
# Daniel, 2026-09-25, on 49 sends and no replies: the copy was fine, the
# greeting was "Hi there" to info@ inboxes. This pass reads each drafted
# prospect's site - about, team, staff, leadership pages - for a real
# person and a direct address, keeps only names that pass the gate, and
# rewrites every waiting draft nobody has edited with the new greeting.
_PEOPLE = {}


def people_state(slug: str) -> dict:
    return _PEOPLE.setdefault(slug, {"running": False, "checked": 0, "named": 0, "emails": 0,
                                     "redrafted": 0, "purged": 0, "step": "", "started": "",
                                     "finished": "", "error": ""})


def purge_junk_contacts(c) -> int:
    """Contacts whose 'name' is not a person - the regex's old mistakes. Only
    the ones we found ourselves; a name Daniel typed is his."""
    n = 0
    for r in c.execute("SELECT id, name FROM contacts WHERE note='found on their website'").fetchall():
        if not rsrch.clean_person_name(r["name"] or ""):
            c.execute("DELETE FROM contacts WHERE id=?", (r["id"],))
            n += 1
    return n


def people_candidates(c, limit: int = 200) -> list:
    """Drafted prospects with a site and no clean named contact."""
    rows = [dict(r) for r in c.execute(
        """SELECT DISTINCT p.id, p.company, p.domain FROM prospects p
             JOIN outreach o ON o.prospect_id=p.id AND o.state IN ('draft','approved')
            WHERE COALESCE(p.domain,'') <> ''
            ORDER BY p.id LIMIT ?""", (limit,))]
    out = []
    for r in rows:
        named = any(rsrch.clean_person_name(k.get("name") or "") for k in contact_rows(c, r["id"]))
        if not named:
            out.append(r)
    return out


def find_person_for(c, pid: int, domain: str, fetch=None) -> dict:
    """Read the site for people. Returns {"people": n, "email": bool}."""
    fetch = fetch or fetch_page
    got = {"people": 0, "email": False}
    try:
        d = rsrch.research(domain, fetch, max_pages=5)
    except Exception:
        return got
    root = (domain or "").lower().replace("www.", "").strip("/")
    for i, person in enumerate((d.get("people") or [])[:3]):
        nm = rsrch.clean_person_name(person.get("name") or "")
        if not nm:
            continue
        if rsrch.someone_elses(person.get("email") or "", root):
            # Hidden Forest HOA's site lists a city council office: a real
            # person, a real address, and not the board. Not our contact.
            continue
        dup = c.execute("SELECT id FROM contacts WHERE prospect_id=? AND lower(name)=?",
                        (pid, nm.lower())).fetchone()
        if dup:
            if person.get("email"):
                c.execute("UPDATE contacts SET email=COALESCE(NULLIF(email,''),?) WHERE id=?",
                          (person["email"], dup["id"]))
            continue
        c.execute("INSERT INTO contacts (prospect_id, name, role, email, phone, is_primary, note, added_at) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (pid, nm, person.get("role", ""), person.get("email", ""), "",
                   1 if (i == 0 and not got["people"]) else 0, "found on their website", now()))
        got["people"] += 1
        if person.get("email"):
            got["email"] = True
    c.execute("UPDATE prospects SET email_checked_at=? WHERE id=?", (now(), pid))
    return got


def find_people(slug: str, fetch=None, max_sites: int = 200) -> dict:
    """The pass. Safe in a thread; sets the workspace itself."""
    st = people_state(slug)
    if st["running"]:
        return st
    st.update({"running": True, "checked": 0, "named": 0, "emails": 0, "redrafted": 0, "purged": 0,
               "step": "starting", "started": now(), "finished": "", "error": ""})
    tok = ws.CURRENT.set(slug)
    try:
        with closing(db(slug)) as c:
            st["purged"] = purge_junk_contacts(c)
            c.commit()
            todo = people_candidates(c, max_sites)
            for r in todo:
                st["step"] = "reading " + (r["domain"] or r["company"] or "")
                got = find_person_for(c, r["id"], r["domain"], fetch)
                c.commit()
                st["checked"] += 1
                st["named"] += 1 if got["people"] else 0
                st["emails"] += 1 if got["email"] else 0
            st["step"] = "rewriting the greetings"
            st["redrafted"] = redraft_untouched(c)
            c.commit()
        st["step"] = "done"
    except Exception as e:
        st["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        st["step"] = "stopped"
    finally:
        ws.CURRENT.reset(tok)
        st["running"] = False
        st["finished"] = now()
    return st


def people_summary(c) -> dict:
    """For the Outbox: how many waiting emails still open 'Hi there' or
    'Hi <company> team' - i.e. have no person behind them."""
    rows = c.execute("SELECT body FROM outreach WHERE state IN ('draft','approved')").fetchall()
    there = sum(1 for r in rows if (r["body"] or "").startswith("Hi there"))
    team = sum(1 for r in rows if re.match(r"Hi .+ (team|board),", (r["body"] or "").split("\n")[0] or ""))
    return {"waiting": len(rows), "hi_there": there, "hi_team": team,
            "with_name": len(rows) - there - team}


@app.get("/api/outreach/people")
def outreach_people_status(request: Request):
    require_auth(request)
    st = dict(people_state(ws.current()))
    with closing(db()) as c:
        st.update(people_summary(c))
        st["candidates"] = len(people_candidates(c, 500)) if not st["running"] else None
    return st


@app.post("/api/outreach/people")
def outreach_people_run(request: Request):
    require_auth(request)
    slug = ws.current()
    if people_state(slug)["running"]:
        return people_state(slug)
    threading.Thread(target=find_people, args=(slug,), daemon=True, name="find-people-" + slug).start()
    time.sleep(0.2)
    return people_state(slug)


def refill_outbox(slug: str, want: int = OUTBOX_TARGET, fetch=None, max_sites: int = REFILL_SITES_PER_PASS) -> dict:
    """Top the Outbox up to `want` waiting emails. Returns the state dict.
    Safe to call from a thread; sets ws.CURRENT itself."""
    st = refill_state(slug)
    if st["running"]:
        return st
    st.update({"running": True, "step": "starting", "checked": 0, "found": 0, "qualified": 0,
               "drafted": 0, "started": now(), "finished": "", "error": "", "need_leads": False})
    tok = ws.CURRENT.set(slug)
    try:
        with closing(db(slug)) as c:
            need = max(0, want - waiting_now(c))
            st["waiting"] = waiting_now(c)
            if need <= 0:
                st["step"] = "full"
                return st
            # 1. anyone already holding an address but never qualified/drafted
            #    gets written first - no site reads needed.
            st["step"] = "writing"
            d = draft_batch(c, limit=need)
            c.commit()
            st["drafted"] += d.get("drafted", 0) + d.get("followups_drafted", 0)
            need = max(0, want - waiting_now(c))
            # 2. go find addresses, a few at a time, drafting as they land
            while need > 0 and st["checked"] < max_sites:
                batch = email_candidates(c, min(10, max_sites - st["checked"]))
                if not batch:
                    break
                for cand in batch:
                    st["step"] = "reading %s" % (cand["company"] or cand["domain"])
                    got = find_email_for(c, cand["id"], cand["domain"], fetch)
                    st["checked"] += 1
                    if got:
                        st["found"] += 1
                        row = c.execute("SELECT * FROM prospects WHERE id=?", (cand["id"],)).fetchone()
                        try:
                            offers, ev = qualify_one(row_to_dict(row))
                            c.execute("UPDATE prospects SET offers=?, offer_evidence=?, qualified_at=? WHERE id=?",
                                      (", ".join(offers), json.dumps(ev), now(), cand["id"]))
                            st["qualified"] += 1
                        except Exception:
                            pass
                    c.commit()
                st["step"] = "writing"
                d = draft_batch(c, limit=need)
                c.commit()
                st["drafted"] += d.get("drafted", 0) + d.get("followups_drafted", 0)
                need = max(0, want - waiting_now(c))
            st["waiting"] = waiting_now(c)
            st["need_leads"] = False
            if need <= 0:
                st["step"] = "done"
            elif not email_candidates(c, 1):
                # Out of leads. Find more on our own if he said to, within a
                # daily budget; otherwise ask.
                today = datetime.now(timezone.utc).date().isoformat()
                used = st.get("auto_found_day") == today and st.get("auto_found", 0) or 0
                plan = find_more_plan(c, min(FIND_MORE_PER_RUN, FIND_MORE_AUTO_PER_DAY - used))
                if auto_find_on() and plan["searches"] and plan["has_key"] and used < FIND_MORE_AUTO_PER_DAY:
                    st["step"] = "out of leads - finding more"
                    res = find_more_leads(slug, plan["searches"])
                    st["auto_found_day"] = today
                    st["auto_found"] = used + res["searched"]
                    st["found_leads"] = st.get("found_leads", 0) + res["added"]
                    st["step"] = (("found %d new businesses%s - reading them next pass"
                                   % (res["added"], " in nearby towns" if plan.get("widened") else ""))
                                  if res["added"] else "out of leads - " + (res["stopped"] or "the searches found nobody new"))
                    if not res["added"]:
                        st["need_leads"] = True
                else:
                    st["step"] = "out of leads"
                    st["need_leads"] = True
            else:
                st["step"] = "paused - more next pass"
    except Exception as e:
        st["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        st["step"] = "failed"
    finally:
        ws.CURRENT.reset(tok)
        st["running"] = False
        st["finished"] = now()
    return st


# ── out of leads: find more ──────────────────────────────────────────
# "If it runs out of leads or leads with emails, it should prompt you to
# search for more leads, or do it automatically, or ask 'you're out of
# leads - would you like us to populate more?'"
#
# It asks by default: a Google search costs a request against the monthly
# budget, so spending it is the owner's call until he says "do this
# automatically". Either way the searches are planned for him - the trades
# he's working (or the ones that have yielded addresses before), in the
# towns he works, skipping any trade+town searched in the last 30 days.
FIND_MORE_PER_RUN = 6                # Google searches per "find more" (≈20 businesses each)
FIND_MORE_AUTO_PER_DAY = 10
LEAD_SEARCH_SETTING = "lead_searches"
DEFAULT_TRADES = {
    "watson": ["Dentist", "Chiropractor", "Med Spa", "HVAC", "Roofing Contractor", "Auto Repair",
               "Restaurant", "Plumber", "Veterinarian", "Hair Salon", "Gym", "Car Wash"],
    "homerepair": ["Property Management", "Real Estate Agency", "Home Builder",
                   "HOA Management", "Apartment Complex"],
}
# Towns around San Antonio / New Braunfels, nearest first, searched once the
# home towns are used up (find_more_plan). Same trades, new businesses.
WIDEN_TOWNS = ["Schertz, TX", "Cibolo, TX", "Selma, TX", "Universal City, TX", "Live Oak, TX",
               "Converse, TX", "Helotes, TX", "Boerne, TX", "Leon Valley, TX", "Alamo Heights, TX",
               "Bulverde, TX", "Garden Ridge, TX", "Canyon Lake, TX", "Kyle, TX", "San Marcos, TX",
               "Marion, TX", "Floresville, TX", "Lakehills, TX"]
GENERIC_TRADES = ["Dentist", "Chiropractor", "HVAC", "Plumber", "Auto Repair", "Restaurant",
                  "Hair Salon", "Roofing Contractor"]
# The "add a trade" dropdown. Local businesses that buy what a small
# software/marketing shop sells; typing anything else still works.
TRADE_MENU = sorted([
    "Accountant", "Apartment Complex", "Auto Body Shop", "Auto Repair", "Bakery", "Barbershop",
    "Car Dealership", "Car Wash", "Carpet Cleaning", "Catering", "Chiropractor", "Cleaning Service",
    "Coffee Shop", "Concrete Contractor", "Daycare", "Dentist", "Dermatologist", "Electrician",
    "Event Venue", "Fast Casual Restaurant", "Fencing Contractor", "Florist", "Food Truck",
    "Garage Door Repair", "General Contractor", "Gym", "Hair Salon", "Handyman", "Home Builder",
    "HVAC", "Insurance Agency", "Landscaping", "Law Firm", "Martial Arts", "Massage Therapist",
    "Med Spa", "Mortgage Broker", "Moving Company", "Nail Salon", "Optometrist", "Orthodontist",
    "Painter", "Pediatrician", "Pest Control", "Pet Groomer", "Physical Therapy", "Pizza",
    "Plumber", "Pool Service", "Property Management", "Real Estate Agency", "Restaurant",
    "Roofing Contractor", "Solar Installer", "Tattoo Shop", "Tire Shop", "Towing", "Tree Service",
    "Urgent Care", "Veterinarian", "Wedding Venue", "Welding", "Window Tinting", "Yoga Studio"])
_TRADE_VERTICAL = [
    (r"restaurant|taqueria|cafe|grill|bistro|pizza|bar\b|bakery|catering|food", "restaurant"),
    (r"auto|car wash|tire|detail|collision|mechanic|dealership", "auto"),
    (r"chiro", "chiro"),
    (r"dent|ortho|med ?spa|spa\b|salon|barber|clinic|doctor|vet|optical|eye|urgent care|physical therapy|gym|fitness|yoga|pilates", "appointment"),
    (r"hvac|roof|plumb|electric|contractor|fenc|concrete|mason|landscap|tree|pool|paint|remodel|weld|pest|builder|handyman", "contractor"),
]


def vertical_for_trade(trade: str) -> str:
    t = (trade or "").lower()
    for rx, v in _TRADE_VERTICAL:
        if re.search(rx, t):
            return v
    return "generic"


def lead_searches() -> dict:
    try:
        v = json.loads(setting(LEAD_SEARCH_SETTING, "") or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def work_cities() -> list:
    """The default city first, then the metros on file - the towns worked."""
    out = [setting("default_city", "").strip() or "San Antonio, TX"]
    for k, m in load_metros().items():
        lab = "%s, %s" % (m.get("label") or k, m.get("state") or "TX")
        if lab.split(",")[0].lower() not in [o.split(",")[0].lower() for o in out] and "corridor" not in k:
            out.append(lab)
    return out[:4]


def best_yield_trades(c, limit: int = 6) -> list:
    """Trades whose websites have actually given us addresses, best first."""
    rows = c.execute(
        """SELECT category, SUM(CASE WHEN COALESCE(email,'')<>'' THEN 1 ELSE 0 END) AS got, COUNT(*) AS n
             FROM prospects WHERE COALESCE(category,'')<>'' AND COALESCE(email_checked_at,'')<>''
            GROUP BY category HAVING n >= 3 ORDER BY (1.0*got/n) DESC, got DESC LIMIT ?""", (limit,)).fetchall()
    return [r["category"] for r in rows if r["got"]]


def find_more_plan(c, n: int = FIND_MORE_PER_RUN) -> dict:
    """What 'find more leads' would search for, before spending anything."""
    focus = focus_trades()
    trades = [t for t in focus if t not in ("restaurant", "auto", "chiro", "appointment", "contractor", "generic")]
    trades += [{"restaurant": "Restaurant", "auto": "Auto Repair", "chiro": "Chiropractor",
                "appointment": "Dentist", "contractor": "HVAC"}.get(t, "") for t in focus
               if t in ("restaurant", "auto", "chiro", "appointment", "contractor")]
    hid = hidden_trades()
    trades = [t for t in trades if t and not is_hidden_trade(t, hid)]
    why = "the trades you're working" if trades else ""
    if not trades:
        trades = [t for t in best_yield_trades(c, 12) if not is_hidden_trade(t, hid)][:6]
        why = "the trades whose websites have given us addresses" if trades else ""
    if len(trades) < 4 and not focus:
        # never leave the owner with two chips because he hid the rest
        # (a trade he chose to work is searched on its own - no padding)
        for t in DEFAULT_TRADES.get(ws.current(), GENERIC_TRADES):
            if len(trades) >= 6:
                break
            if not is_hidden_trade(t, hid) and t.lower() not in [x.lower() for x in trades]:
                trades.append(t)
        why = why or "a starter list for what you sell"
    done = lead_searches()
    fresh_after = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    pairs = []
    for city in work_cities():
        for t in trades:
            key = "%s|%s" % (t, city)
            if done.get(key, "") < fresh_after:
                pairs.append({"trade": t, "city": city})
    widened = False
    if not pairs and trades:
        # Every trade in every home town was searched inside the last 30 days.
        # Stopping here is what made "do this on its own" stall and ask instead:
        # three towns x a handful of trades is only ~100 businesses, and one
        # page of Google results each. Go to the neighbouring towns, nearest
        # first, same trades - so it keeps filling without anyone tapping.
        home = {x.split(",")[0].lower() for x in work_cities()}
        for city in WIDEN_TOWNS:
            if city.split(",")[0].lower() in home:
                continue
            for t in trades:
                if done.get("%s|%s" % (t, city), "") < fresh_after:
                    pairs.append({"trade": t, "city": city})
        widened = bool(pairs)
    on_file = [r[0] for r in c.execute("SELECT DISTINCT category FROM prospects WHERE COALESCE(category,'')<>''")]
    menu = sorted({t for t in TRADE_MENU + on_file if not is_hidden_trade(t, hid)}, key=str.lower)
    shown = pairs[:n]
    cities_out = work_cities()
    for sch in shown:
        if sch["city"] not in cities_out:
            cities_out.append(sch["city"])
    return {"searches": shown, "why": why, "trades": trades, "cities": cities_out,
            "widened": widened, "hidden": hid, "menu": menu,
            "requests_left": remaining_requests(), "has_key": bool(google_key()),
            "all_searched": not pairs}


def find_more_leads(slug: str, searches: list, key: str = "") -> dict:
    """Run the planned searches. Every new business is due a call today and
    goes on the list for the refill to read for an address."""
    tok = ws.CURRENT.set(slug)
    out = {"searched": 0, "found": 0, "added": 0, "skipped": 0, "stopped": ""}
    try:
        key = key or google_key()
        if not key:
            out["stopped"] = "No Google Maps key - add one under Setup → Lead finder."
            return out
        done = lead_searches()
        for sch in searches:
            if remaining_requests() <= 0:
                out["stopped"] = cap_message()
                break
            trade, city = sch["trade"], sch["city"]
            try:
                places = places_search("%s in %s" % (trade, city), key, pages=1)
            except HTTPException as e:
                out["stopped"] = str(e.detail)
                break
            added, skipped, new_ids = insert_places(places, city, vertical_for_trade(trade))
            if new_ids:
                with closing(db(slug)) as c:
                    marks = ",".join("?" * len(new_ids))
                    c.execute("UPDATE prospects SET category=?, source='find_more' WHERE id IN (%s) "
                              "AND COALESCE(category,'')=''" % marks, [trade] + list(new_ids))
                    c.commit()
            done["%s|%s" % (trade, city)] = now()
            out["searched"] += 1
            out["found"] += len(places)
            out["added"] += added
            out["skipped"] += skipped
        set_setting(LEAD_SEARCH_SETTING, json.dumps(done))
    finally:
        ws.CURRENT.reset(tok)
    return out


def auto_find_on() -> bool:
    return setting("auto_find_leads", "").strip() == "1"


def refill_auto(slug: str) -> bool:
    """The automatic version: when the Outbox is empty, fill it - at most
    once every REFILL_AUTO_EVERY. Called from the loop with the workspace set."""
    st = refill_state(slug)
    if st["running"]:
        return False
    with closing(db(slug)) as c:
        if not has_catalog():
            return False
        waiting = waiting_now(c)
        # A run that stopped at its site budget with the Outbox still short
        # picks straight back up - reading 60 sites takes five minutes, and
        # "50 at a time" should not take an afternoon of button presses.
        resume = (st["step"].startswith("paused") or st["step"].startswith("found ")) and waiting < OUTBOX_TARGET
        if not resume:
            # Short of 50 with unread leads left: keep working toward it
            # (a restart forgets a paused run; this picks it back up).
            # Empty: refill, which asks - or finds more - when out of leads.
            short = waiting < OUTBOX_TARGET and bool(email_candidates(c, 1))
            if not (waiting == 0 or short) or time.time() - st["auto_at"] < REFILL_AUTO_EVERY:
                return False
    st["auto_at"] = time.time()
    threading.Thread(target=refill_outbox, args=(slug,), daemon=True, name="outbox-refill-" + slug).start()
    return True


def _refill_loop(every: int = 120):
    while True:
        beat("outbox-refill")
        time.sleep(every)
        for slug in list(ws.WORKSPACES):
            tok = ws.CURRENT.set(slug)
            try:
                refill_auto(slug)
            except Exception as e:
                print("refill loop [%s]: %s: %s" % (slug, type(e).__name__, e))
            finally:
                ws.CURRENT.reset(tok)


class RefillBody(BaseModel):
    want: int = OUTBOX_TARGET


@app.post("/api/outreach/refill")
def outreach_refill(request: Request, body: RefillBody):
    """Start a refill in the background; poll GET for progress."""
    require_auth(request)
    catalog_ready("write emails")
    slug = ws.current()
    st = refill_state(slug)
    if not st["running"]:
        threading.Thread(target=refill_outbox, args=(slug, max(1, min(body.want, 200))),
                         daemon=True, name="outbox-refill-" + slug).start()
        time.sleep(0.2)
    return refill_status(request)


@app.get("/api/outreach/refill")
def refill_status(request: Request):
    require_auth(request)
    st = dict(refill_state(ws.current()))
    with closing(db()) as c:
        st["waiting"] = waiting_now(c)
        st["candidates"] = len(email_candidates(c, 500))
    st["target"] = OUTBOX_TARGET
    st["auto_find"] = auto_find_on()
    if not st.get("running") and (st.get("need_leads") or (st["candidates"] == 0 and st["waiting"] == 0)):
        st["need_leads"] = True
        with closing(db()) as c:
            st["plan"] = find_more_plan(c)
        dismissed = setting("find_more_dismissed_at", "")
        if dismissed and (st.get("finished") or "") <= dismissed:
            st["need_leads"] = False            # "not now" - until it runs short again
            st["dismissed"] = True
    return st


@app.post("/api/leads/find_more/dismiss")
def find_more_dismiss(request: Request):
    require_auth(request)
    set_setting("find_more_dismissed_at", now())
    return {"ok": True}


class FindMoreBody(BaseModel):
    trades: list = []
    cities: list = []
    auto: Optional[bool] = None       # also remember "do this automatically next time"


@app.post("/api/leads/find_more")
def leads_find_more(request: Request, body: FindMoreBody):
    """'You're out of leads - find more?' → yes. Runs the searches, then
    starts a refill so the new businesses are read for addresses and
    written to straight away."""
    require_auth(request)
    if body.auto is not None:
        set_setting("auto_find_leads", "1" if body.auto else "")
    slug = ws.current()
    with closing(db()) as c:
        plan = find_more_plan(c)
    if body.trades:
        cities = [x for x in (body.cities or plan["cities"][:1]) if str(x).strip()]
        searches = [{"trade": str(t).strip()[:60], "city": str(ci).strip()[:60]}
                    for ci in cities for t in body.trades if str(t).strip()][:FIND_MORE_PER_RUN * 2]
    else:
        searches = plan["searches"]
    if not searches:
        raise HTTPException(400, "Every trade on the list was searched in the last month. "
                                 "Type a different trade or town.")
    res = find_more_leads(slug, searches)
    if res["added"]:
        st = refill_state(slug)
        if not st["running"]:
            threading.Thread(target=refill_outbox, args=(slug,), daemon=True,
                             name="outbox-refill-" + slug).start()
    res["searches"] = searches
    res["requests_left"] = remaining_requests()
    return res


@app.post("/api/leads/auto_find")
def leads_auto_find(request: Request, body: FindMoreBody):
    require_auth(request)
    set_setting("auto_find_leads", "1" if body.auto else "")
    return {"auto_find": auto_find_on()}


# ── email autopilot ──────────────────────────────────────────────────
# "I would like an option on the follow-up emails to be done automatically
# instead of me having to go through and approve each one - but I want the
# option to turn on and off." Then: "probably should have that option for
# all the email outreach." And: "it will need to update the CRM and mark as
# sent as well."
#
# Three modes, one setting: '' (off - the Outbox as it was), 'followups'
# (the app sends steps 2 and 3 itself; first emails still wait for a tap),
# 'all' (first emails too). Whatever the mode, an auto-sent email goes
# through record_sent() exactly like a tapped one, so the prospect's history,
# the queue and the scoreboard cannot tell the difference - except for
# sent_via, which is how the Outbox shows "sent by autopilot".
#
# The rails, none of them optional:
#   * the mailbox has to be connected - autopilot reads the inbox before it
#     sends, so it never follows up on someone who already wrote back;
#   * weekdays, working hours, in the workspace's time zone;
#   * the same daily cap as the Outbox, plus a per-hour pace so ten emails
#     don't leave in the same second;
#   * only rows in 'draft'. An 'approved' row was handed to Mail already and
#     may be sitting in a compose window - sending it again is a duplicate;
#   * a draft with the mailing-address placeholder still in it never goes.
AUTOPILOT_MODES = ("", "followups", "all")
AUTOPILOT_PER_HOUR_DEFAULT = 8
# Daniel's cap is 999,999. A human tapping Send is its own brake; a loop is
# not, so autopilot has a ceiling of its own that the Settings cap cannot
# raise. Warmbly's ramp tops out at 50/day for a warmed domain.
AUTOPILOT_DAILY_MAX = 40
AUTOPILOT_SCAN_EVERY = 15 * 60          # seconds between inbox refreshes
_AUTOPILOT_STATE = {}                   # slug -> {"last_scan", "last_run", "last_error", "last_sent"}


def autopilot_mode() -> str:
    m = (setting("autopilot_mode", "") or "").strip().lower()
    return m if m in AUTOPILOT_MODES else ""


def autopilot_per_hour() -> int:
    try:
        return max(1, min(60, int(setting("autopilot_per_hour", "") or AUTOPILOT_PER_HOUR_DEFAULT)))
    except (TypeError, ValueError):
        return AUTOPILOT_PER_HOUR_DEFAULT


def workspace_tz():
    from zoneinfo import ZoneInfo
    name = (setting("timezone", "") or "America/Chicago").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Chicago")


def autopilot_blockers(c) -> list:
    """Why autopilot would not send right now, in words. Empty = clear."""
    out = []
    host, user, pw = mailbox_creds()
    if not (host and user and pw):
        out.append("Your mailbox isn't connected (Setup → Your mailbox). Autopilot reads "
                   "the inbox before it sends so it never follows up on someone who "
                   "already replied - it won't send blind.")
    if not setting("sender_email", "").strip():
        out.append("No sending address under Setup → Your business.")
    if not setting("sender_address", "").strip():
        out.append("No mailing address under Setup → Your business - the law requires "
                   "one in every commercial email.")
    cap = min(daily_email_cap(), AUTOPILOT_DAILY_MAX)
    if sent_today(c) >= cap:
        out.append("Today's limit of %d emails is used up." % cap)
    return out


TOUCH_GAP_HOURS = 48       # never two messages to the same person inside 48 hours, any channel


def recent_touch(c, prospect_id: int, phone: str = "", hours: int = TOUCH_GAP_HOURS) -> str:
    """'emailed 5 hours ago' / 'texted yesterday' if we reached this person
    inside the gap, on any channel; '' if not. The playbook's 48-hour rule
    (Small Business plugin, sequence_patterns.md): a text on Monday and an
    email on Tuesday is one person being chased, however it was sent."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    last = c.execute("SELECT MAX(sent_at) FROM outreach WHERE prospect_id=? AND state='sent' AND sent_at >= ?",
                     (prospect_id, since)).fetchone()[0]
    kind = "emailed" if last else ""
    n = telnyx.to_e164(phone or "") if phone else ""
    if n:
        t = c.execute("SELECT MAX(at) FROM sms_log WHERE direction='out' AND number=? AND at >= ?",
                      (n, since)).fetchone()[0]
        if t and (not last or t > last):
            last, kind = t, "texted"
    if not last:
        return ""
    try:
        ago = datetime.now(timezone.utc) - datetime.fromisoformat(last).astimezone(timezone.utc)
    except ValueError:
        return "%s recently" % kind
    h = int(ago.total_seconds() // 3600)
    return "%s %s" % (kind, "just now" if h < 1 else ("%d hour%s ago" % (h, "" if h == 1 else "s") if h < 24 else "yesterday"))


def row_audience(r: dict) -> str:
    return windows.audience_for(r.get("category") or "", ws.current())


def autopilot_sendable(c, mode: str, now_only: bool = False) -> list:
    """The drafts autopilot is allowed to send, oldest first. With
    `now_only`, only the ones whose audience is reading right now (the
    sending window) and who haven't been reached on any channel in the
    last 48 hours - the two playbook rules, applied at the moment of
    sending rather than at drafting."""
    where = ("o.state='draft' AND COALESCE(o.send_error,'')='' "
             "AND p.status NOT IN ('booked','dead','corporate','cold','conversation') "
             "AND NOT EXISTS (SELECT 1 FROM outreach r WHERE r.prospect_id=o.prospect_id "
             "                 AND (COALESCE(r.reply,'')<>'' OR COALESCE(r.reply_text,'')<>''))")
    if mode == "followups":
        where += " AND o.variant LIKE 'followup:%'"
    rows = [dict(r) for r in c.execute(
        "SELECT o.*, p.category, p.phone FROM outreach o JOIN prospects p ON p.id=o.prospect_id "
        "WHERE %s ORDER BY o.id" % where)]
    dead = dead_addresses(c)
    rows = [r for r in rows if (r["to_addr"] or "").lower() not in dead]
    rows = [r for r in rows if "[ADD YOUR MAILING ADDRESS" not in (r["body"] or "")]
    if now_only:
        local = datetime.now(workspace_tz())
        rows = [r for r in rows if windows.is_open(local, row_audience(r))
                and not recent_touch(c, r["prospect_id"], r.get("phone") or "")]
    return rows


def autopilot_window_note(c, mode: str) -> str:
    """Why nothing goes right now, in words, when drafts are waiting but no
    audience is reading: 'Offices read again Tue 8:00 AM.' '' if clear."""
    waiting = autopilot_sendable(c, mode)
    if not waiting:
        return ""
    local = datetime.now(workspace_tz())
    open_now = [r for r in waiting if windows.is_open(local, row_audience(r))]
    if open_now:
        held = [r for r in open_now if recent_touch(c, r["prospect_id"], r.get("phone") or "")]
        if len(held) == len(open_now):
            return "Everyone whose window is open was reached in the last 48 hours - holding."
        return ""
    nxt = min(windows.next_open(local, row_audience(r)) for r in waiting)
    who = sorted({row_audience(r) for r in waiting})
    return "Outside sending hours for everyone waiting (%s). Next window opens %s." % (
        "; ".join(windows.describe(a) for a in who), local_clock(nxt.astimezone(timezone.utc).isoformat()))


def sent_last_hour(c) -> int:
    return c.execute("SELECT COUNT(*) FROM outreach WHERE state='sent' AND sent_via='autopilot' "
                     "AND sent_at >= ?",
                     ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),)).fetchone()[0]


def autopilot_gap_minutes() -> float:
    """Space between autopilot sends. It used to send three a pass until the
    hour's allowance was gone - 8 emails in two minutes, then nothing for
    58, which looks stalled on screen and looks like a blast to Gmail."""
    return 60.0 / autopilot_per_hour()


def autopilot_next_send(c) -> dict:
    """When the next email can go, and why not sooner. {"at": iso-utc or "",
    "why": words}. at="" means as soon as the next pass (within ~90s)."""
    tz = workspace_tz()
    utcnow = datetime.now(timezone.utc)
    hour_ago = (utcnow - timedelta(hours=1)).isoformat()
    recent = [r[0] for r in c.execute(
        "SELECT sent_at FROM outreach WHERE state='sent' AND sent_via='autopilot' AND sent_at >= ? "
        "ORDER BY sent_at", (hour_ago,))]
    at, why = utcnow, ""
    per = autopilot_per_hour()
    if len(recent) >= per:
        at = datetime.fromisoformat(recent[len(recent) - per]) + timedelta(hours=1)
        why = "this hour's %d are out" % per
    elif recent:
        spaced = datetime.fromisoformat(recent[-1]) + timedelta(minutes=autopilot_gap_minutes())
        if spaced > at:
            at, why = spaced, "spacing them about %d minutes apart" % round(autopilot_gap_minutes())
    local = at.astimezone(tz)
    waiting = autopilot_sendable(c, autopilot_mode() or "all")
    audiences = sorted({row_audience(r) for r in waiting}) or [windows.DEFAULT_AUDIENCE]
    if not any(windows.is_open(local, a) for a in audiences):
        nxt = min(windows.next_open(local, a) for a in audiences)
        at, why = nxt.astimezone(timezone.utc), "outside sending hours for everyone waiting"
    if at <= utcnow + timedelta(seconds=5):
        return {"at": "", "why": ""}
    return {"at": at.isoformat(timespec="seconds"), "why": why}


def local_clock(iso: str) -> str:
    """'2026-09-24T18:47:28+00:00' -> '1:47 PM' (or 'Fri 8:00 AM' on another day)."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso).astimezone(workspace_tz())
    except ValueError:
        return iso
    today = datetime.now(workspace_tz()).date()
    clock = t.strftime("%I:%M %p").lstrip("0")
    return clock if t.date() == today else t.strftime("%a ") + clock


def autopilot_send_one(c, row: dict, smtp_factory=None, imap_factory=None) -> dict:
    """Send one draft through the mailbox and record it exactly as a tapped
    'Mark sent' would. Returns {"ok", "message_id" | "error"}."""
    host, user, pw = mailbox_creds()
    from_addr = setting("sender_email", "").strip() or user
    from_name = setting("sender_name", "").strip()
    smtp_host = setting("smtp_host", "").strip() or mailer.smtp_host_for(host)
    smtp_port = int(setting("smtp_port", "") or mailer.DEFAULT_SMTP_PORT)

    # Thread a follow-up under the last thing we sent them, when we know it.
    prev = c.execute("SELECT message_id FROM outreach WHERE prospect_id=? AND state='sent' "
                     "AND COALESCE(message_id,'')<>'' ORDER BY sent_at DESC LIMIT 1",
                     (row["prospect_id"],)).fetchone()
    in_reply_to = prev["message_id"] if prev else ""
    try:
        msg = mailer.build(from_addr, from_name, row["to_addr"], row["subject"], row["body"],
                           in_reply_to=in_reply_to)
        mid = mailer.send(smtp_host, smtp_port, user, pw, msg, smtp_factory=smtp_factory)
    except mailer.MailError as e:
        c.execute("UPDATE outreach SET send_error=? WHERE id=?", (str(e)[:400], row["id"]))
        return {"ok": False, "error": str(e)}

    # It left. From here on, nothing may fail loudly - the email is gone and
    # the record must say so.
    c.execute("UPDATE outreach SET state='sent', decided_at=?, sent_at=?, sent_via='autopilot', "
              "message_id=?, copy_check='as_drafted', sent_subject=?, sent_body=? WHERE id=?",
              (now(), now(), mid, (row["subject"] or "")[:300], (row["body"] or "")[:8000], row["id"]))
    record_sent(c, row)
    c.commit()          # the send is a fact now; the Sent-folder copy below is a nicety
    try:
        folder = setting("sent_folder", "").strip()
        if not folder:
            folder = sentcheck.find_sent_folder(inbox.list_folders(host, user, pw)) or ""
            if folder:
                set_setting("sent_folder", folder)
        mailer.append_sent(host, user, pw, msg, folder, imap_factory=imap_factory)
    except Exception as e:
        # The email went; a missing Sent copy is worth a log line, never a failure.
        print("autopilot: could not file a Sent copy: %s: %s" % (type(e).__name__, e))
    return {"ok": True, "message_id": mid}


def autopilot_pass(slug: str, force: bool = False, smtp_factory=None, imap_factory=None) -> dict:
    """One tick for one workspace: refresh the inbox if it's been a while,
    draft what's owed, send what the rails allow. Called with the workspace
    already set. `force` skips the time window (for the 'Send now' button)."""
    st = _AUTOPILOT_STATE.setdefault(slug, {"last_scan": 0.0, "last_run": "", "last_error": "",
                                            "last_sent": ""})
    mode = autopilot_mode()
    out = {"mode": mode, "sent": 0, "drafted": 0, "skipped": [], "errors": []}
    if not mode:
        return out
    st["last_run"] = now()
    with closing(db(slug)) as c:
        blockers = autopilot_blockers(c)
        if force:
            blockers = [b for b in blockers if not b.startswith("Outside sending hours")]
        if blockers:
            out["skipped"] = blockers
            return out

        # Replies first. A person who wrote back an hour ago is the one
        # thing this must never write to again.
        if time.time() - st["last_scan"] > AUTOPILOT_SCAN_EVERY:
            try:
                scan_mailbox(days=7, apply=True)
                st["last_scan"] = time.time()
            except Exception as e:
                st["last_error"] = "inbox: %s" % str(e)[:200]
                out["errors"].append(st["last_error"])
                return out               # no send without a fresh read of the inbox

        try:
            d = draft_batch(c, followups_only=(mode == "followups"))
            out["drafted"] = (d.get("drafted") or 0) + (d.get("followups_drafted") or 0)
        except HTTPException as e:
            out["errors"].append(str(e.detail))
        c.commit()

        room = max(0, min(daily_email_cap(), AUTOPILOT_DAILY_MAX) - sent_today(c))
        pace = max(0, autopilot_per_hour() - sent_last_hour(c))
        # One at a time, spaced out. "Send what's ready now" may send up to
        # three at once, but never past the hour's allowance.
        n = min(room, pace, 3 if force else 1)
        if not force and autopilot_next_send(c)["at"]:
            n = 0
        ready = autopilot_sendable(c, mode, now_only=True)     # in their window, past the 48-hour gap
        if autopilot_sendable(c, mode) and (n == 0 or not ready):
            n = 0
            nxt = autopilot_next_send(c)
            out["waiting"] = autopilot_window_note(c, mode) or (
                "Next email goes out about %s (%s)." % (local_clock(nxt["at"]), nxt["why"]) if nxt["at"] else "")
        for row in ready[:n]:
            res = autopilot_send_one(c, row, smtp_factory=smtp_factory, imap_factory=imap_factory)
            c.commit()
            if res["ok"]:
                out["sent"] += 1
                st["last_sent"] = now()
            else:
                out["errors"].append("%s: %s" % (row["to_addr"], res["error"]))
                st["last_error"] = out["errors"][-1]
                break                    # one refusal is a mailbox problem; don't hammer it
    return out


def _autopilot_loop(every: int = 90):
    while True:
        beat("email-autopilot")
        time.sleep(every)
        for slug in list(ws.WORKSPACES):
            tok = ws.CURRENT.set(slug)
            try:
                autopilot_pass(slug)
            except Exception as e:
                print("autopilot [%s]: %s: %s" % (slug, type(e).__name__, e))
            finally:
                ws.CURRENT.reset(tok)


def autopilot_window_text(c, mode: str) -> str:
    """'Sends when they read: offices Tue-Thu 8-5...; HOA boards weekday
    evenings...' for the audiences with a draft waiting (or every audience
    this workspace writes to, when nothing is waiting)."""
    who = sorted({row_audience(r) for r in autopilot_sendable(c, mode)})
    if not who:
        who = ["office", "hoa"] if ws.current() == "homerepair" else ["retail"]
    tz = datetime.now(workspace_tz()).tzname() or ""
    return "sends when they read (%s) %s" % ("; ".join(windows.describe(a) for a in who), tz)


class AutopilotBody(BaseModel):
    mode: str = ""


@app.get("/api/autopilot")
def autopilot_status(request: Request):
    require_auth(request)
    slug = ws.current()
    st = _AUTOPILOT_STATE.get(slug, {})
    with closing(db()) as c:
        mode = autopilot_mode()
        waiting = len(autopilot_sendable(c, mode or "all"))
        followups_waiting = len(autopilot_sendable(c, "followups"))
        today = c.execute("SELECT COUNT(*) FROM outreach WHERE state='sent' AND sent_via='autopilot' "
                          "AND date(sent_at)=date('now')").fetchone()[0]
        return {"mode": mode, "modes": list(AUTOPILOT_MODES),
                "blockers": autopilot_blockers(c) if mode else [],
                "waiting": waiting, "followups_waiting": followups_waiting,
                "sent_today": today, "cap": min(daily_email_cap(), AUTOPILOT_DAILY_MAX),
                "per_hour": autopilot_per_hour(),
                "window": autopilot_window_text(c, mode or "all"),
                "window_note": autopilot_window_note(c, mode or "all"),
                "last_run": st.get("last_run", ""), "last_sent": st.get("last_sent", ""),
                "last_sent_local": local_clock(c.execute(
                    "SELECT MAX(sent_at) FROM outreach WHERE state='sent' AND sent_via='autopilot'").fetchone()[0] or ""),
                "next_send": dict(autopilot_next_send(c), local=local_clock(autopilot_next_send(c)["at"])),
                "gap_minutes": round(autopilot_gap_minutes()),
                "last_error": st.get("last_error", "")}


@app.post("/api/autopilot")
def autopilot_set(request: Request, body: AutopilotBody):
    require_auth(request)
    mode = (body.mode or "").strip().lower()
    if mode not in AUTOPILOT_MODES:
        raise HTTPException(400, "mode must be one of: off, followups, all")
    set_setting("autopilot_mode", mode)
    return autopilot_status(request)


@app.post("/api/autopilot/run")
def autopilot_run_now(request: Request):
    """One pass right now, ignoring the clock but not the other rails."""
    require_auth(request)
    return autopilot_pass(ws.current(), force=True)


@app.get("/api/prospect/{pid}/campaign_brief")
def campaign_brief(request: Request, pid: int):
    """Research first, then a plan - or a refusal and the reason.

    The old campaign tab asked the operator to type what the business sells.
    This reads it: vertical, service area, rating, reviews, offers, and every
    finding from the scan. If the site can't convert paid traffic it returns
    ready=false and the fix list instead of a campaign, because a plan built on
    top of a broken landing page is a plan to lose money slowly.
    """
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        d = row_to_dict(r)
        gate = camp.readiness(d)
        econ = camp.economics(c)
    out = {"prospect_id": pid, "company": d.get("company"),
           "domain": d.get("domain"), "readiness": gate, "economics": econ}
    out["brief"] = camp.brief(d, econ) if gate.get("ready") else None
    if not gate.get("ready"):
        out["instead"] = ("Fix the blockers first. That work is billable on its own, and "
                          "it is the reason the campaign will have something to measure.")
    return out


@app.get("/api/revenue")
def revenue(request: Request):
    """Real money, from the deals already in the CRM.

    This tab said "not connected" while the deals table sat next to it holding
    amounts, MRR and outcomes. Ad spend genuinely isn't connected - that needs
    platform APIs - so spend, CPL and ROAS stay explicitly absent rather than
    being estimated into existence.
    """
    require_auth(request)
    with closing(db()) as c:
        econ = camp.economics(c)
        by_state = {s: {"n": n, "amount": round(a or 0), "mrr": round(m or 0)}
                    for s, n, a, m in c.execute(
                        "SELECT state, COUNT(*), SUM(amount), SUM(mrr) "
                        "FROM deals GROUP BY state")}
        by_kind = [{"kind": k or "other", "label": DEAL_LABELS.get(k or "other", k or "other"),
                    "n": n, "amount": round(a or 0)}
                   for k, n, a in c.execute(
                       "SELECT kind, COUNT(*), SUM(amount) FROM deals "
                       "WHERE state='won' GROUP BY kind ORDER BY SUM(amount) DESC")]
        # Where the money came from, traced back to how the lead was found.
        # This is the join the Revenue tab was missing, and it needs no API.
        by_source = [{"source": (s or "unknown").split(":")[0].strip() or "unknown",
                      "n": n, "amount": round(a or 0)}
                     for s, n, a in c.execute(
                         "SELECT p.source, COUNT(*), SUM(d.amount) FROM deals d "
                         "JOIN prospects p ON p.id = d.prospect_id "
                         "WHERE d.state='won' GROUP BY p.source "
                         "ORDER BY SUM(d.amount) DESC")]
        recent = [{"company": co, "title": t, "amount": round(a or 0),
                   "mrr": round(m or 0), "closed_at": ca}
                  for co, t, a, m, ca in c.execute(
                      "SELECT p.company, d.title, d.amount, d.mrr, d.closed_at "
                      "FROM deals d LEFT JOIN prospects p ON p.id = d.prospect_id "
                      "WHERE d.state='won' ORDER BY d.closed_at DESC LIMIT 10")]
    return {"economics": econ, "by_state": by_state, "by_kind": by_kind,
            "by_source": by_source, "recent_wins": recent,
            "ad_spend": None,
            "ad_spend_note": ("Not connected. Spend, cost-per-lead and ROAS need the "
                              "Google Ads and Meta APIs; until those are wired in this "
                              "stays empty rather than estimated.")}


@app.post("/api/outreach/{oid}/reply")
def log_reply(request: Request, oid: int, body: ReplyBody):
    """Record that somebody wrote back. This is the only input the scoreboard
    has, so it has to be one tap in the Outbox, not a spreadsheet."""
    require_auth(request)
    kind = (body.kind or "").strip().lower()
    if kind not in REPLY_KINDS:
        raise HTTPException(400, "kind must be one of: %s" % ", ".join(sorted(REPLY_KINDS)))
    with closing(db()) as c:
        r = c.execute("SELECT prospect_id FROM outreach WHERE id=?", (oid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such draft")
        c.execute("UPDATE outreach SET reply=?, replied_at=? WHERE id=?", (kind, now(), oid))
        # A reply is a touch. It belongs in the history with the calls, or the
        # prospect card tells a different story than the scoreboard does.
        # The column is `at`. This said `created_at` and was wrapped in a
        # silent except, so every reply ever logged vanished from the prospect
        # history instead of sitting with the calls - invisible because nothing
        # had ever logged a reply to notice it with.
        c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at)"
                  " VALUES (?,?,?,?,?)",
                  (r[0], "email", "reply:" + kind, body.note or "", now()))
        # An unsubscribe is not a data point to think about later. Honour it now.
        if kind == "unsubscribe":
            d = c.execute("SELECT domain FROM prospects WHERE id=?", (r[0],)).fetchone()
            if d and d[0]:
                try:
                    suppress(d[0])
                except Exception:
                    pass
            c.execute("UPDATE prospects SET status='dead', next_due=NULL WHERE id=?", (r[0],))
            retire_drafts(c, r[0], "unsubscribed")
            sync_stage(c, r[0], "dead")
        # A real reply - or a bounce, which means the address is dead - means
        # a human is already on this. The automated follow-up sweep does not
        # talk over them. Gated to next_action='email_followup' so this only
        # ever touches a row this mechanism itself scheduled; Watson Factor's
        # call/email cadence never sees this branch.
        if kind in ("positive", "negative", "bounce", "unsubscribe"):
            c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
                      "WHERE id=? AND next_action='email_followup'", (r[0],))
        # Same two triggers the inbox scan fires, so it makes no difference
        # whether the reply was found by the scanner or typed in by hand.
        # A flat no still earns the call: at this volume a negative reply is
        # the clearest read available on whether the pitch landed.
        queued = False
        # The inbox scan already put them on Today when it found the reply,
        # and Daniel may have called since. Tapping the label a day later
        # must not drag them back to the top of the list a second time.
        already = c.execute("SELECT reply_detected_at FROM outreach WHERE id=?", (oid,)).fetchone()
        called_since = False
        if already and (already["reply_detected_at"] or "").strip():
            called_since = bool(c.execute(
                "SELECT 1 FROM touches WHERE prospect_id=? AND kind='call' AND at>=?",
                (r[0], already["reply_detected_at"])).fetchone())
        if trig.wants_callback(kind) and not called_since:
            queued = put_on_the_phone(c, r[0], "Replied: %s" % kind)
        moved = None
        if kind == "bounce":
            bad = c.execute("SELECT to_addr FROM outreach WHERE id=?", (oid,)).fetchone()
            if bad and (bad[0] or "").strip():
                moved = retarget_after_bounce(c, r[0], bad[0].strip())
        c.commit()
    return {"ok": True, "id": oid, "reply": kind,
            "call_back_queued": queued, "retargeted_to": moved}


@app.post("/api/prospect/{pid}/reply_not_real")
def reply_not_real(request: Request, pid: int):
    """QC on a reply: "that wasn't a person". Files it as an auto-reply and
    undoes what the reply set in motion (call-back, warm stage), so the lead
    goes back to the email cadence instead of sitting on the phone list."""
    require_auth(request)
    with closing(db()) as c:
        o = c.execute("SELECT id FROM outreach WHERE prospect_id=? AND coalesce(reply_text,'')<>'' "
                      "ORDER BY reply_detected_at DESC LIMIT 1", (pid,)).fetchone()
        if not o:
            raise HTTPException(404, "no reply on file for this prospect")
        c.execute("UPDATE outreach SET reply='auto_reply', replied_at=? WHERE id=?", (now(), o["id"]))
        p = c.execute("SELECT status, stage, next_action FROM prospects WHERE id=?", (pid,)).fetchone()
        if p and p["status"] == "conversation" and p["next_action"] == CALLBACK_ACTION:
            c.execute("UPDATE prospects SET status='working', next_action='email', "
                      "next_due=date('now','+3 day') WHERE id=?", (pid,))
        if p and p["stage"] == "warm":
            c.execute("UPDATE prospects SET stage='working', stage_at=? WHERE id=?", (now(), pid))
            log(c, pid, "stage", "working", "warm \u2192 working (reply marked not real)")
        note_touch(c, pid, "reply:auto_reply", "Marked: not a real reply (auto-reply / system message)")
        c.commit()
    return {"ok": True}


class InboxScanBody(BaseModel):
    days: int = 30
    apply: bool = True


def mailbox_creds() -> tuple:
    """(host, user, password) for the connected mailbox, or ('', '', '')."""
    host = setting("imap_host", "").strip()
    user = setting("imap_user", "").strip() or setting("sender_email", "").strip()
    pw = setting("imap_password", "").strip()
    return (host, user, pw) if (host and user and pw) else ("", "", "")


@app.post("/api/inbox/scan")
def inbox_scan(request: Request, body: InboxScanBody):
    require_auth(request)
    return scan_mailbox(days=body.days, apply=body.apply)


def _reply_dt(v):
    try:
        d = datetime.fromisoformat((v or "").replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


_FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "aol.com", "icloud.com", "me.com",
              "outlook.com", "hotmail.com", "live.com", "msn.com", "att.net", "sbcglobal.net",
              "comcast.net", "verizon.net", "protonmail.com"}


def _reply_dom(addr: str) -> str:
    host = (addr or "").lower().rsplit("@", 1)[-1]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def match_reply_to_send(c, from_addr: str, m: dict):
    """Which email of ours is this a reply to?

    Address first, then the thread (In-Reply-To / References carry our
    Message-ID), then the subject line. Two rules keep it honest:

      * A reply cannot predate the email it answers. One leasing-bot
        auto-reply used to be re-filed against every later send that shared
        the subject "tenant maintenance requests" - seven "THEY REPLIED"
        cards from one message nobody at those companies wrote.
      * A subject shared by several sends only matches the one whose
        recipient is on the sender's own domain. A subject nobody else
        received (one board member writing from Gmail) still matches.
    """
    cols = "id, prospect_id, reply, reply_detected_at, to_addr, sent_at"
    when = _reply_dt(m.get("date"))

    def after_send(row) -> bool:
        st = _reply_dt(row["sent_at"])
        return not (when and st and when < st - timedelta(minutes=2))

    for row in c.execute("SELECT %s FROM outreach WHERE state='sent' AND lower(to_addr)=? "
                         "ORDER BY sent_at DESC" % cols, ((from_addr or "").lower(),)):
        if after_send(row):
            return row
    hdr = {k.lower(): (v or "") for k, v in (m.get("headers") or {}).items()}
    refs = " ".join([hdr.get("in-reply-to", ""), hdr.get("references", "")])
    for mid in re.findall(r"<[^>]+>", refs):
        row = c.execute("SELECT %s FROM outreach WHERE state='sent' AND message_id=? "
                        "LIMIT 1" % cols, (mid,)).fetchone()
        if row:
            return row
    subj = sentcheck.norm_subject(m.get("subject") or "")
    if len(subj) >= 12:
        hits = []
        for cand in c.execute("SELECT %s, subject, subject_original FROM outreach WHERE state='sent' "
                              "ORDER BY sent_at DESC LIMIT 400" % cols):
            if (sentcheck.norm_subject(cand["subject"] or "") == subj or
                    sentcheck.norm_subject(cand["subject_original"] or "") == subj) \
                    and after_send(cand):
                hits.append(cand)
        if len({h["prospect_id"] for h in hits}) == 1:
            return hits[0]
        for h in hits:
            d = _reply_dom(from_addr)
            if d and d not in _FREE_MAIL and _reply_dom(h["to_addr"]) == d:
                return h
    return None


def recheck_replies(c) -> dict:
    """One-time cleanup of replies filed before the matcher knew better.

    A leasing bot's auto-reply had been filed against seven unrelated
    prospects as "they replied - call back", and the words in it ("we would
    love to have you come in") moved them to Replied. Re-read every stored
    reply that nobody has scored: machine messages are marked as such, the
    ones filed against the wrong company are detached, and a prospect with no
    real reply and no call goes back to the email cadence.
    """
    fixed = detached = restored = 0
    rows = c.execute(
        "SELECT id, prospect_id, to_addr, subject, reply_from, reply_text, sent_at "
        "FROM outreach WHERE coalesce(reply_text,'')<>'' AND coalesce(reply,'')=''").fetchall()
    touched = set()
    for o in rows:
        kind = inbox.classify(o["reply_from"], o["subject"], o["reply_text"], {})
        if kind not in ("auto_reply", "out_of_office"):
            # A real one: put what they said into the history line.
            quote = "\u201c" + re.sub(r"\s+", " ", o["reply_text"])[:500] + "\u201d"
            c.execute("UPDATE touches SET note=? WHERE prospect_id=? AND kind='email' "
                      "AND outcome='reply' AND note LIKE 'They wrote back: Re:%'",
                      ("They wrote back: " + quote, o["prospect_id"]))
            c.execute("UPDATE touches SET note=? WHERE prospect_id=? AND outcome='queued:call_back' "
                      "AND note LIKE 'Replied: Re:%'", ("Replied: " + quote[:210], o["prospect_id"]))
            continue
        related = (_reply_dom(o["reply_from"]) == _reply_dom(o["to_addr"])
                   and _reply_dom(o["reply_from"]) not in _FREE_MAIL)
        if related:
            c.execute("UPDATE outreach SET reply=?, replied_at=? WHERE id=?", (kind, now(), o["id"]))
            fixed += 1
        else:
            c.execute("UPDATE outreach SET reply_text='', reply_from='', reply_detected_at=NULL "
                      "WHERE id=?", (o["id"],))
            detached += 1
        touched.add((o["prospect_id"], o["sent_at"]))
    for pid, sent_at in touched:
        real = c.execute("SELECT 1 FROM outreach WHERE prospect_id=? AND coalesce(reply_text,'')<>'' "
                         "AND coalesce(reply,'')=''", (pid,)).fetchone()
        called = c.execute("SELECT 1 FROM touches WHERE prospect_id=? AND kind='call'", (pid,)).fetchone()
        if real or called:
            continue
        p = c.execute("SELECT status, stage, next_action FROM prospects WHERE id=?", (pid,)).fetchone()
        if not p:
            continue
        if p["status"] == "conversation" and p["next_action"] == CALLBACK_ACTION:
            c.execute("UPDATE prospects SET status='working', next_action='email', "
                      "next_due=max(date('now'), date(?, '+3 day')) WHERE id=?", (sent_at or now(), pid))
        if p["stage"] == "warm":
            c.execute("UPDATE prospects SET stage='working', stage_at=? WHERE id=?", (now(), pid))
            log(c, pid, "stage", "working", "warm \u2192 working (that reply was an auto-reply)")
        c.execute("DELETE FROM touches WHERE prospect_id=? AND ((kind='email' AND outcome='reply') "
                  "OR outcome='queued:call_back')", (pid,))
        note_touch(c, pid, "reply:auto_reply",
                   "Re-checked: the 'reply' was an auto-reply, not a person - back on the email cadence")
        restored += 1
    return {"auto_marked": fixed, "detached": detached, "restored": restored}


@app.post("/api/replies/recheck")
def replies_recheck(request: Request):
    require_auth(request)
    with closing(db()) as c:
        out = recheck_replies(c)
        c.commit()
    return {"ok": True, **out}


def scan_mailbox(days: int = 30, apply: bool = True) -> dict:
    """Read the mailbox and give every sent email an actual outcome.

    This is the missing half of the whole outreach system. Ninety-four emails
    went out before this existed and all ninety-four still read `reply=''`,
    which `send_health()` then reported as a 0.0% bounce rate - a confident
    number computed from a column nobody had ever written to. Until something
    reads the inbox, a dead address and a disinterested owner are the same
    row, and there is no way to tell a list problem from a copy problem.

    Bounces, out-of-offices and autoresponders are classified and applied.
    A human's actual answer is NOT scored - it is stored with its text and
    waits for one tap in the Outbox, because guessing whether two lines mean
    yes or no would put the same lie back in a different column.
    """
    try:
        with closing(db()) as c0:
            if setting("replies_rechecked", "") != "v1":
                recheck_replies(c0)
                c0.commit()
                set_setting("replies_rechecked", "v1")
    except Exception:
        pass
    host, user, pw = mailbox_creds()
    if not (host and user and pw):
        raise HTTPException(400,
            "No mailbox connected yet. Add the IMAP host, address and password "
            "in Setup - for Namecheap PrivateEmail that is mail.privateemail.com.")

    try:
        msgs = inbox.fetch(host, user, pw, since_days=max(1, min(days, 180)))
    except Exception as e:
        raise HTTPException(502, "Could not read the mailbox: %s: %s"
                            % (type(e).__name__, str(e)[:160]))

    ours = {user.lower(), setting("sender_email", "").lower()} - {""}
    found, applied, unmatched = [], 0, 0
    callbacks, exhausted, retargeted = 0, 0, []

    with closing(db()) as c:
        for m in msgs:
            kind = inbox.classify(m["from"], m["subject"], m["body"], m["headers"])
            # Who was this about? A human replies from the address we wrote
            # to; a bounce comes from the mail server and names the dead
            # address inside the report instead.
            target = m["from"]
            if kind == "bounce":
                target = inbox.bounced_address(m["raw"], our_own=ours) or ""
            if not target or target in ours:
                continue
            row = match_reply_to_send(c, target, m)
            if not row:
                unmatched += 1
                continue
            if (row["reply"] or "").strip():
                continue                      # already has an outcome

            typed_reply = inbox.strip_quoted(m["body"])[:4000]
            if kind != "bounce" and c.execute(
                    "SELECT 1 FROM outreach WHERE id<>? AND reply_from=? AND reply_text=? LIMIT 1",
                    (row["id"], m["from"], typed_reply)).fetchone():
                continue                      # this exact message is already filed elsewhere
            rec = {"outreach_id": row["id"], "kind": kind, "from": m["from"],
                   "subject": m["subject"], "when": m["date"],
                   "preview": inbox.strip_quoted(m["body"])[:200]}
            found.append(rec)
            if not apply:
                continue
            if kind == "human" and (row["reply_detected_at"] or "").strip():
                # Already seen and already put on the phone. A human reply
                # keeps reply='' until the Outbox tap, so the guard above
                # never fires for it - and the UI asks for 60 days every
                # time, so the same message came back every morning, reset
                # the prospect to today's call list and wrote another
                # queued:call_back over whatever Daniel had logged after the
                # call. It stays in needs_a_tap (it still does), but the
                # queue is not touched twice for one reply.
                continue

            c.execute("UPDATE outreach SET reply_text=?, reply_from=?, reply_detected_at=? "
                      "WHERE id=?",
                      (typed_reply, m["from"], now(), row["id"]))
            if kind == "human":
                # Deliberately unscored - the Outbox still wants one tap to
                # say whether it was a yes or a no. But the queue does not
                # need to know which to know that somebody should ring them
                # today. This used to clear next_due and nothing else, which
                # meant the warmest lead in the system vanished from the list
                # at the exact moment it got warm.
                c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
                          "WHERE id=? AND next_action='email_followup'", (row["prospect_id"],))
                c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at)"
                          " VALUES (?,?,?,?,?)",
                          (row["prospect_id"], "email", "reply",
                           "They wrote back: \u201c" + re.sub(r"\s+", " ", typed_reply)[:500] + "\u201d", now()))
                if put_on_the_phone(c, row["prospect_id"],
                                    "Replied: \u201c" + re.sub(r"\s+", " ", typed_reply)[:200] + "\u201d"):
                    callbacks += 1
                # Auto-advance to "warm" only on what THEY typed - the quoted
                # copy of our own email is full of "quote" and "schedule".
                if classify_outreach_reply(typed_reply[:600]) == "positive":
                    sync_stage(c, row["prospect_id"], "warm_reply")
                applied += 1
                continue

            c.execute("UPDATE outreach SET reply=?, replied_at=? WHERE id=?",
                      (kind, now(), row["id"]))
            c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at)"
                      " VALUES (?,?,?,?,?)",
                      (row["prospect_id"], "email", "reply:" + kind,
                       "Found in the inbox: " + (m["subject"] or "")[:160], now()))
            if kind == "unsubscribe":
                d = c.execute("SELECT domain FROM prospects WHERE id=?",
                              (row["prospect_id"],)).fetchone()
                if d and d[0]:
                    try:
                        suppress(d[0])
                    except Exception:
                        pass
                c.execute("UPDATE prospects SET status='dead', next_due=NULL WHERE id=?",
                          (row["prospect_id"],))
                retire_drafts(c, row["prospect_id"], "unsubscribed")
                sync_stage(c, row["prospect_id"], "dead")
            if kind == "bounce":
                # The address is dead - the lead is not. Record the address,
                # then move to the next person at that business. Before this,
                # one bad address ended the conversation with the whole
                # company, which is a list problem misread as a no.
                moved = retarget_after_bounce(c, row["prospect_id"], target)
                if moved:
                    retargeted.append({"prospect_id": row["prospect_id"],
                                       "from": target, "to": moved})
                else:
                    exhausted += 1
            if kind in ("positive", "negative", "bounce", "unsubscribe"):
                c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL "
                          "WHERE id=? AND next_action='email_followup'", (row["prospect_id"],))
            applied += 1
        c.commit()

    counts = {}
    for f in found:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1

    # Same mailbox, other direction: what actually LEFT. Never allowed to
    # break the inbox pass - a mailbox with an unfindable Sent folder still
    # gets its bounces and replies applied.
    sent_check = None
    if apply:
        try:
            folder, sent_msgs = inbox.fetch_sent(host, user, pw,
                                                 since_days=max(1, min(days, 180)))
            with closing(db()) as c:
                sent_check = verify_sent_copy(c, sent_msgs)
                c.commit()
            sent_check["folder"] = folder
        except Exception as e:
            sent_check = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}

    return {"scanned": len(msgs), "matched": len(found), "applied": applied,
            "unmatched_senders": unmatched, "by_kind": counts,
            "call_backs_queued": callbacks,
            "retargeted": retargeted,
            "bounced_with_no_alternative": exhausted,
            "needs_a_tap": [f for f in found if f["kind"] == "human"],
            "found": found[:60],
            "sent_check": sent_check}


def verify_sent_copy(c, msgs, only_unverified: bool = True) -> dict:
    """Match Sent-folder messages to outreach rows and record whether each
    went out as drafted or rewritten. Pure over the messages it is handed -
    the network happens in inbox.fetch_sent, the verdict in sentcheck.

    A row that comes back 'edited' also gets the same feedback entry the
    Outbox writes for an in-app edit, because that is exactly what it is:
    the gap between what was written and what Daniel was willing to put his
    name on, filed without him having to type anything.
    """
    checked = as_drafted = edited = unmatched = 0
    details = []
    for m in msgs:
        to = (m.get("to") or "").strip().lower()
        if not to:
            continue
        q = ("SELECT id, prospect_id, offer, variant, sent_at, subject, body, "
             "subject_original, body_original, copy_check FROM outreach "
             "WHERE state='sent' AND lower(to_addr)=?")
        rows = [dict(r) for r in c.execute(q, (to,))]
        if only_unverified:
            rows = [r for r in rows if not (r["copy_check"] or "").strip()]
        row = sentcheck.pick_nearest(rows, m.get("date") or "")
        if not row:
            unmatched += 1
            continue
        # The draft as it was WRITTEN is the baseline, not the row's current
        # subject/body - an Outbox edit already moved those.
        draft_s = row["subject_original"] or row["subject"] or ""
        draft_b = row["body_original"] or row["body"] or ""
        v = sentcheck.compare(draft_s, draft_b, m.get("subject") or "", m.get("body") or "")
        sent_b = sentcheck.strip_history(m.get("body") or "").strip()
        verdict = "edited" if v["edited"] else "as_drafted"
        c.execute("UPDATE outreach SET copy_check=?, sent_subject=?, sent_body=? WHERE id=?",
                  (verdict, (m.get("subject") or "")[:300], sent_b[:8000], row["id"]))
        checked += 1
        if v["edited"]:
            edited += 1
            already = c.execute("SELECT 1 FROM feedback WHERE kind='edit' AND "
                                "subject_type='outreach' AND subject_id=?",
                                (row["id"],)).fetchone()
            if not already:
                comp = c.execute("SELECT company FROM prospects WHERE id=?",
                                 (row["prospect_id"],)).fetchone()
                c.execute(
                    "INSERT INTO feedback (kind, tab, rating, subject_type, subject_id,"
                    " label, body, before_text, after_text, created_at)"
                    " VALUES ('edit','outbox','bad','outreach',?,?,?,?,?,?)",
                    (row["id"], (comp["company"] if comp else "") + " - " + (row["offer"] or ""),
                     "Rewritten in the mail app before sending: " + v["reason"],
                     draft_b, sent_b, now()))
        else:
            as_drafted += 1
        details.append({"outreach_id": row["id"], "to": to, "variant": row["variant"],
                        "verdict": verdict, "reason": v["reason"],
                        "coverage": v["coverage"]})
    return {"checked": checked, "as_drafted": as_drafted, "edited": edited,
            "unmatched": unmatched, "details": details[:60]}


class VerifySentBody(BaseModel):
    days: int = 60
    recheck: bool = False        # re-judge rows already verified


@app.post("/api/outreach/verify_sent")
def verify_sent(request: Request, body: VerifySentBody):
    """Read the Sent folder and settle, for every send it can match, whether
    the draft or a rewrite is what left. The mailbox scan does this too;
    this is the button next to the scoreboard."""
    require_auth(request)
    host = setting("imap_host", "").strip()
    user = setting("imap_user", "").strip() or setting("sender_email", "").strip()
    pw = setting("imap_password", "").strip()
    if not (host and user and pw):
        raise HTTPException(400,
            "No mailbox connected yet. Add the IMAP host, address and password "
            "in Setup - the same login the reply scan uses.")
    try:
        folder, msgs = inbox.fetch_sent(host, user, pw,
                                        since_days=max(1, min(body.days, 180)))
    except LookupError as e:
        raise HTTPException(404, "Could not find a Sent folder on this mailbox: %s" % e)
    except Exception as e:
        raise HTTPException(502, "Could not read the Sent folder: %s: %s"
                            % (type(e).__name__, str(e)[:160]))
    with closing(db()) as c:
        out = verify_sent_copy(c, msgs, only_unverified=not body.recheck)
        c.commit()
    out["folder"] = folder
    out["scanned"] = len(msgs)
    return out


@app.get("/api/outreach/scoreboard")
def scoreboard(request: Request):
    """Per-variant results, with an explicit statement of whether the numbers
    are allowed to mean anything yet.

    At ten sends a day a good cold email replies somewhere around 3-8%, so
    thirty sends per cell is an expected zero to two replies. Two-versus-one is
    noise. The endpoint says so rather than letting a bar chart imply a winner,
    because the most expensive mistake available here is killing the variant
    that would have worked.
    """
    require_auth(request)
    rows = []
    with closing(db()) as c:
        def cell(where, args):
            """One scoreboard row's numbers for the sends matching `where`."""
            sent = c.execute("SELECT COUNT(*) FROM outreach WHERE state='sent' AND " + where,
                             args).fetchone()[0]
            kinds = {k: 0 for k in REPLY_KINDS}
            for k, n in c.execute("SELECT reply, COUNT(*) FROM outreach WHERE state='sent' "
                                  "AND COALESCE(reply,'') <> '' AND " + where +
                                  " GROUP BY reply", args):
                if k in kinds:
                    kinds[k] = n
            human = sum(kinds[k] for k in HUMAN_REPLIES)
            return {"sent": sent, "replies": human, "bounces": kinds["bounce"],
                    "positive": kinds["positive"], "breakdown": kinds,
                    "rate": round(100.0 * human / sent, 1) if sent else None}

        # A variant's cell holds only sends of THAT copy. A send Daniel
        # rewrote before it left (copy_check='edited') is his email, not the
        # variant's, and its reply - or its silence - belongs to him.
        for v in VARIANT_ORDER:
            r = cell("variant=? AND COALESCE(copy_check,'') <> 'edited'", (v,))
            r["variant"] = v
            r["waiting"] = c.execute("SELECT COUNT(*) FROM outreach WHERE variant=? AND "
                                     "state IN ('draft','approved')", (v,)).fetchone()[0]
            r["unverified"] = c.execute(
                "SELECT COUNT(*) FROM outreach WHERE variant=? AND state='sent' "
                "AND COALESCE(copy_check,'') = ''", (v,)).fetchone()[0]
            rows.append(r)

        yours = cell("COALESCE(copy_check,'') = 'edited'", ())
        yours.update({"variant": "yours", "waiting": 0, "unverified": 0})

        checked = {}
        for k, n in c.execute("SELECT COALESCE(copy_check,''), COUNT(*) FROM outreach "
                              "WHERE state='sent' GROUP BY COALESCE(copy_check,'')"):
            checked[k or "unverified"] = n
        checked.setdefault("unverified", 0)
        checked.setdefault("as_drafted", 0)
        checked.setdefault("edited", 0)
        has_mailbox = bool(setting("imap_host", "").strip() and
                           setting("imap_password", "").strip())

    least = min((r["sent"] for r in rows), default=0)
    if least < 30:
        verdict = ("Too early. %d sends in the thinnest cell; 30 is the floor before any "
                   "of these numbers are worth reading." % least)
    else:
        best = max(rows, key=lambda r: r["replies"])
        worst = min(rows, key=lambda r: r["replies"])
        if best["replies"] - worst["replies"] >= 3:
            verdict = ("'%s' is beating '%s' by %d replies. That gap is big enough to act on: "
                       "drop the loser, keep testing the rest."
                       % (best["variant"], worst["variant"],
                          best["replies"] - worst["replies"]))
        else:
            verdict = ("No separation yet - the spread is inside the noise at this volume. "
                       "Keep sending, or get the answer faster by calling the people you "
                       "already emailed.")
    # How much of this board is verified. Every unverified send is a row
    # whose copy is assumed, not known - said out loud rather than folded in.
    if checked["unverified"]:
        if has_mailbox:
            copy_note = ("%d of these sends have not been checked against your Sent "
                         "folder yet - run the mailbox scan in Setup and they will be."
                         % checked["unverified"])
        else:
            copy_note = ("%d of these sends have never been checked against what actually "
                         "left your mailbox. Until the mailbox is connected in Setup, an "
                         "email you rewrote in Apple Mail still counts for the variant it "
                         "started as." % checked["unverified"])
    else:
        copy_note = "Every send on this board was checked against your Sent folder."
    return {"rows": rows, "yours": yours, "verdict": verdict, "min_sent": least,
            "checked": checked, "copy_note": copy_note}


@app.get("/api/outreach")
def list_outreach(request: Request, state: str = "draft", limit: int = 100):
    require_auth(request)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT o.*, p.company, p.tier, p.lead_score, p.lead_quality, p.city,
                      p.complaint, p.domain, p.phone, p.offer_evidence
               FROM outreach o JOIN prospects p ON p.id = o.prospect_id
               WHERE o.state = ?
               ORDER BY COALESCE(p.tier,1) DESC, COALESCE(p.lead_score,0) DESC, o.id
               LIMIT ?""", (state, limit)).fetchall()]
        counts = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM outreach GROUP BY state")}
    never = slop.never_list(setting("never_say", ""))
    vp = voice_profile()
    with closing(db()) as c:
        for r in rows:
            if r.get("state") in ("draft", "approved"):
                r["slop"] = slop.check(r.get("subject", ""), r.get("body", ""), r.get("step") or 1, never,
                                       signature=setting("sender_name", ""), ceiling=slop_ceiling(r.get("offer"), r.get("step") or 1))
                w = voice.long_sentence_warning(r.get("body", ""), vp)
                if w:
                    r["slop"].append(w)
                r["recent_touch"] = recent_touch(c, r["prospect_id"], r.get("phone") or "")
                r["audience"] = row_audience(r)
    return {"outreach": rows, "counts": counts,
            "send_from": setting("sender_email", ""),
            "brand": sender_brand()}


STORY_CEILING = 220


def slop_ceiling(offer: str, step: int) -> int:
    """Daniel's picture-painting AI Receptionist email (2026-09-23) tells a
    story on purpose - 5:30 on a Friday, a party of eight - and he chose
    that over a short one. It gets room for the story; everything else
    gets the playbook's ceiling. 0 = the default."""
    if ws.current() == ws.PRIMARY and (offer or "") == "AI Receptionist" and int(step or 1) == 1:
        return STORY_CEILING
    return 0


class SlopBody(BaseModel):
    subject: str = ""
    body: str = ""
    step: int = 1
    offer: str = ""


@app.post("/api/outreach/slop")
def outreach_slop(request: Request, body: SlopBody):
    """The slop test on whatever is in the box right now - the Outbox calls
    it as Daniel edits. Lifted from the Small Business plugin's outreach
    playbook: warns, never rewrites, never blocks."""
    require_auth(request)
    out = slop.score(body.subject, body.body, body.step, slop.never_list(setting("never_say", "")),
                     signature=setting("sender_name", ""), ceiling=slop_ceiling(body.offer, body.step))
    w = voice.long_sentence_warning(body.body, voice_profile())
    if w:
        out["warnings"].append(w); out["clean"] = False
    return out



# ── voice: learn, show, suggestions from edits ──────────────────────────
class VoiceLearnBody(BaseModel):
    samples: list = Field(default_factory=list)   # pasted emails
    from_mailbox: bool = False                    # or read the Sent folder


@app.get("/api/voice")
def voice_get(request: Request):
    require_auth(request)
    p = voice_profile()
    return {"profile": p, "summary": voice.summary(p), "built": p.get("built", ""), "sources": p.get("sources", "")}


@app.post("/api/voice/learn")
def voice_learn(request: Request, body: VoiceLearnBody):
    """Build the profile from pasted emails, or from the last 30 days of the
    connected mailbox's Sent folder (outbound, to people outside the
    business, quoted text stripped). Never invents: fewer than three usable
    samples is an error, not a guess."""
    require_auth(request)
    samples = [str(x) for x in (body.samples or []) if str(x).strip()]
    src = "pasted"
    if body.from_mailbox:
        host, user, pw = mailbox_creds()
        if not host:
            raise HTTPException(400, "No mailbox is connected. Connect one under Setup → Your mailbox, or paste three emails instead.")
        try:
            folder, msgs = inbox.fetch_sent(host, user, pw, since_days=60, limit=120)
        except LookupError as e:
            raise HTTPException(400, "Couldn't find a Sent folder on that mailbox (%s). Paste three emails instead." % e)
        except Exception as e:
            raise HTTPException(502, "Couldn't read the mailbox: %s" % str(e)[:160])
        mine = {user.lower(), setting("sender_email", "").strip().lower()}
        own_dom = user.split("@")[-1].lower()
        for m in msgs:
            to = (m.get("to") or "").lower()
            if not to or to in mine or to.endswith("@" + own_dom):
                continue
            b = inbox.strip_quoted(m.get("body") or "")
            if 15 <= len(b.split()) <= 400 and "unsubscribe" not in b.lower() and "you're receiving this" not in b.lower():
                samples.append(b)
            if len(samples) >= 30:
                break
        src = "%d sent emails" % len(samples)
    p = voice.learn(samples, (setting("sender_name", "").strip().split(" ") or [""])[0])
    if p.get("error"):
        raise HTTPException(400, p["error"])
    p["built"] = local_today(); p["sources"] = src if body.from_mailbox else "%d pasted emails" % len(samples)
    set_setting("voice_profile", json.dumps(p))
    return {"ok": True, "profile": p, "summary": voice.summary(p)}


@app.post("/api/voice/forget")
def voice_forget(request: Request):
    require_auth(request)
    set_setting("voice_profile", "")
    return {"ok": True}


@app.get("/api/voice/suggestions")
def voice_suggestions(request: Request):
    """Phrases the owner has struck from drafts at least twice - offered
    once for the never-say list, dismissable."""
    require_auth(request)
    with closing(db()) as c:
        pairs = [(r["body_original"], r["body"]) for r in c.execute(
            "SELECT body_original, body FROM outreach WHERE COALESCE(body_original,'')<>'' AND body<>body_original "
            "ORDER BY id DESC LIMIT 200")]
    never = slop.never_list(setting("never_say", ""))
    dismissed = slop.never_list(setting("voice_dismissed", ""))
    return {"suggestions": voice.suggestions(pairs, never, dismissed), "edits": len(pairs)}


class VoiceSuggestBody(BaseModel):
    phrase: str = ""
    action: str = "add"      # add | dismiss


@app.post("/api/voice/suggestions")
def voice_suggest_act(request: Request, body: VoiceSuggestBody):
    require_auth(request)
    ph = (body.phrase or "").strip().lower()
    if not ph:
        raise HTTPException(400, "No phrase.")
    if body.action == "add":
        cur = slop.never_list(setting("never_say", ""))
        if ph not in [x.lower() for x in cur]:
            set_setting("never_say", ", ".join(cur + [ph]))
    else:
        cur = slop.never_list(setting("voice_dismissed", ""))
        if ph not in [x.lower() for x in cur]:
            set_setting("voice_dismissed", ", ".join(cur + [ph]))
    return {"ok": True, "never_say": setting("never_say", "")}


class OutreachBody(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    state: Optional[str] = None      # approved | sent | skipped | draft
    note: str = ""


def mark_waiting_draft_sent(c, pid: int) -> int:
    """Daniel emailed them himself (Today's Email button, or from Mail). If
    the Outbox was holding a draft for them, that draft is what went - mark
    it sent so it is counted, verified against the Sent folder later, and
    never sent a second time. The queue move is the caller's."""
    row = c.execute("SELECT id, subject, body FROM outreach WHERE prospect_id=? AND "
                    "state IN ('draft','approved') ORDER BY id LIMIT 1", (pid,)).fetchone()
    if not row:
        return 0
    c.execute("UPDATE outreach SET state='sent', decided_at=?, sent_at=?, sent_via='mail', "
              "note='Marked sent from the lead card' WHERE id=?", (now(), now(), row["id"]))
    return 1


def record_sent(c, row) -> None:
    """Everything that follows an email actually leaving - the CRM side of
    a send. One function, whether Daniel pressed Send in Mail and tapped
    "Mark sent", or autopilot sent it over SMTP: the touch on the prospect's
    history, and the queue moving the way it would after a call.

    `row` is the outreach row as it was BEFORE being marked sent."""
    pid = row["prospect_id"]
    # Sending is a touch. The queue reschedules exactly as a call would.
    c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at)"
              " VALUES (?, 'email', 'sent', ?, ?)",
              (pid, "Sent: " + (row["subject"] or ""), now()))
    sync_stage(c, pid, "sent")
    seg, sig = seq.parse_variant(row["variant"])
    if seg:
        # A HomeRepair sequence step - schedule the step after this
        # one (or close the sequence out), not Watson's call cadence.
        homerepair_advance(c, pid, seg, sig, int(row["step"] or 1))
    elif (row["variant"] or "").startswith("followup:"):
        # Watson's own email follow-up track, which is a separate
        # channel from the call cadence the way HomeRepair's is.
        # Falling through to advance_queue() unconditionally would walk a
        # prospect from call-step 1 to call-step 4 across the two
        # follow-ups without anyone ever picking up a phone, and two more
        # sends would push them past LAST_STEP and mark them cold.
        #
        # But doing NOTHING was its own bug: after the first email the
        # queue says "email, in 2 days", the follow-up goes out, and the
        # queue is never told - so the lead sits in Today as an overdue
        # EMAIL forever. "I feel like I'm seeing all the same leads over
        # and over again": 37 of the 38 overdue emails had already been
        # sent. So: if the step the queue is waiting on IS an email, this
        # was that email - advance to the call that follows it. If the
        # queue is waiting on a call, the call is still owed; leave it.
        p = c.execute("SELECT next_action FROM prospects WHERE id=?", (pid,)).fetchone()
        if p and (p["next_action"] or "") == "email":
            advance_queue(c, pid, "sent", None)
    else:
        advance_queue(c, pid, "sent", None)


def reconcile_email_steps(c) -> int:
    """One-time repair for the bug above, safe to run every start: any lead
    the queue is holding for an EMAIL that has already had a follow-up sent
    since that step was set moves on to the step after it. Idempotent -
    once advanced, the step is a call and the query no longer matches."""
    rows = c.execute(
        """SELECT p.id FROM prospects p
            WHERE p.next_action='email' AND p.next_due IS NOT NULL
              AND p.status NOT IN ('booked','dead','cold','corporate')
              AND EXISTS (SELECT 1 FROM outreach o
                           WHERE o.prospect_id=p.id AND o.state='sent'
                             AND o.variant LIKE 'followup:%'
                             AND o.sent_at >= COALESCE(p.touched_at, ''))""").fetchall()
    for r in rows:
        advance_queue(c, r["id"], "sent", None)
    return len(rows)


@app.post("/api/outreach/{oid}")
def update_outreach(request: Request, oid: int, body: OutreachBody):
    require_auth(request)
    valid = {"draft", "approved", "sent", "skipped"}
    if body.state and body.state not in valid:
        raise HTTPException(400, "state must be one of %s" % sorted(valid))
    with closing(db()) as c:
        row = c.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        sets, args = [], []
        if body.subject is not None:
            sets.append("subject=?"); args.append(body.subject.strip()[:300])
        if body.body is not None:
            sets.append("body=?"); args.append(body.body.strip()[:8000])
        if body.note:
            sets.append("note=?"); args.append(body.note.strip()[:600])
        if body.state:
            sets += ["state=?", "decided_at=?"]; args += [body.state, now()]
            if body.state == "sent":
                sets.append("sent_at=?"); args.append(now())
        if not sets:
            raise HTTPException(400, "Nothing to change.")
        c.execute("UPDATE outreach SET %s WHERE id=?" % ", ".join(sets), args + [oid])

        pid = row["prospect_id"]

        # The correction nobody has to file. If the email that went out reads
        # differently to the one that was written, that gap IS the feedback -
        # more honest than anything typed into a box, because it is what he
        # was willing to put his name on.
        if body.state in ("sent", "approved") and row["state"] not in ("sent", "approved"):
            orig_s = row["subject_original"] or row["subject"] or ""
            orig_b = row["body_original"] or row["body"] or ""
            final_s = (body.subject if body.subject is not None else row["subject"]) or ""
            final_b = (body.body if body.body is not None else row["body"]) or ""
            if orig_b.strip() != final_b.strip() or orig_s.strip() != final_s.strip():
                what = []
                if orig_s.strip() != final_s.strip():
                    what.append("changed the subject")
                if orig_b.strip() != final_b.strip():
                    what.append(diff_summary(orig_b, final_b))
                comp = c.execute("SELECT company FROM prospects WHERE id=?", (pid,)).fetchone()
                c.execute(
                    "INSERT INTO feedback (kind, tab, rating, subject_type, subject_id,"
                    " label, body, before_text, after_text, created_at)"
                    " VALUES ('edit','outbox','bad','outreach',?,?,?,?,?,?)",
                    (oid, (comp["company"] if comp else "") + " - " + (row["offer"] or ""),
                     "Edited before sending: " + "; ".join(what),
                     orig_b, final_b, now()))
                # An Outbox edit is a known edit, whether it is caught at
                # Approve (the usual path - Approve is what opens the mail
                # app) or at Mark sent. The reverse is NOT known: an
                # unchanged Outbox draft can still be rewritten in Apple
                # Mail after the mailto: handoff, so copy_check stays ''
                # there and the Sent-folder check settles it.
                c.execute("UPDATE outreach SET copy_check='edited', sent_subject=?, "
                          "sent_body=? WHERE id=?",
                          (final_s[:300], final_b[:8000], oid))

        if body.state == "sent" and row["state"] != "sent":
            cap = daily_email_cap()
            if sent_today(c) > cap:
                raise HTTPException(429,
                    "That would put you over today's limit of %d emails. The cap is "
                    "there so this mailbox doesn't get flagged - the rest keep until "
                    "tomorrow." % cap)
        if body.state == "sent" and row["state"] != "sent":
            record_sent(c, row)
        elif body.state == "skipped":
            log(c, pid, "note", "", "Skipped the drafted email" +
                (": " + body.note if body.note else "."))
        c.commit()
    return {"ok": True}


def report_brand() -> dict:
    """What goes at the top of a site-check report: the business's real
    logo (the one uploaded on the Social tab) as a data URI, its name, and
    the phone/site line. No logo uploaded = the JG mark, as before."""
    logo = ""
    raw = brand_logo_bytes()
    if raw:
        mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else ""
        if mime:
            logo = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())
    contact = " · ".join(x for x in (setting("sender_phone", "").strip(), setting("sender_site", "").strip()) if x)
    return {"brand": sender_brand(), "logo": logo, "contact": contact}


@app.get("/report", response_class=HTMLResponse)
def report(request: Request, url: str = Query(..., min_length=3)):
    require_auth(request)
    from urllib.parse import quote
    d = analyze(url, deep=True)
    # pdf_url turns on the "Download as PDF" button in the report toolbar
    return HTMLResponse(render_report(d, pdf_url=f"/report.pdf?url={quote(url, safe='')}", **report_brand()))


@app.get("/report.pdf")
def report_pdf(request: Request, url: str = Query(..., min_length=3)):
    """Branded PDF site audit — the thing you email or hand a prospect."""
    require_auth(request)
    from grit_analyzer.pdfgen import PdfUnavailable, html_to_pdf
    d = analyze(url, deep=True)
    html = render_report(d, **report_brand())   # no toolbar inside the PDF itself
    try:
        pdf = html_to_pdf(html)
    except PdfUnavailable as e:
        # 501: the feature exists but this machine has no Chrome to render with
        raise HTTPException(501, detail=str(e))
    host = (d.get("host") or "site").replace("/", "_")
    return Response(pdf, media_type="application/pdf", headers={
        "content-disposition": f'attachment; filename="site-check-{host}.pdf"'})


# ── door-knock leave-behind ──────────────────────────────────────────────
# Daniel, 2026-09-27: a button on the Today card that prints a two-page sheet
# for the business you're walking into - what the research found, the
# product(s) it points to with a picture painted, and a page showing what the
# product looks like. Rendering lives in leavebehind.py; this gathers the lead.
SEVERITY_RANK = {"critical": 0, "serious": 1, "warning": 2}
# Only the findings an owner feels, said the way he'd say them. "No
# LocalBusiness structured data" is true and useless on a sheet handed across
# a counter, so anything not listed here stays in the full site report.
OWNER_FINDINGS = [
    (r"^No phone number on the homepage", "There's no phone number on your homepage for someone to tap."),
    (r"phone number isn't tap-to-call", "Your number is on the site, but tapping it on a phone doesn't dial."),
    (r"^No contact form", "There's no form on your site - if someone can't call right now, they have no other way to reach you."),
    (r"^Not built for phones", "Your site isn't built for phones, which is where most people will find you."),
    (r"^No call-to-action near the top", "The top of your site never asks a visitor to call or book."),
    (r"reviews aren't shown", "Your site doesn't show any of your customer reviews."),
    (r"^No street address on the (homepage|page)", "Your street address isn't on your homepage."),
]


def leave_behind_data(pid: int) -> dict:
    with closing(db()) as c:
        p = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "No such lead.")
        p = dict(p)
        ct = c.execute("SELECT name, role FROM contacts WHERE prospect_id=? AND name != '' "
                       "ORDER BY is_primary DESC, id LIMIT 1", (pid,)).fetchone()
    offers = [o.strip() for o in (p.get("offers") or "").split(",") if o.strip()]
    if not offers:
        offers = list(HR_OFFERS.get(homerepair_segment(p), HR_OFFERS["property_manager"])) \
            if ws.current() == "homerepair" else ["Website"]
    try:
        evidence = json.loads(p.get("offer_evidence") or "{}")
    except (ValueError, TypeError):
        evidence = {}
    try:
        findings = [f for f in json.loads(p.get("findings") or "[]") if isinstance(f, dict)]
    except (ValueError, TypeError):
        findings = []
    noticed = []
    # HomeRepair's "evidence" is our reason for calling ("they care how
    # they're seen...") - a note to Daniel, not something to hand the prospect.
    for o in offers[:2]:
        if evidence.get(o) and ws.current() != "homerepair":
            noticed.append(lb.you(evidence[o]))
    if p.get("opener") and any(o in ("Website", "AI Receptionist") for o in offers[:2]):
        line = "On your site, %s." % p["opener"].rstrip(".")
        if not any(p["opener"][:30].lower() in n.lower() for n in noticed):
            noticed.append(line)
    findings.sort(key=lambda f: SEVERITY_RANK.get(f.get("severity"), 3))
    if ws.current() == "homerepair":
        findings = []          # website findings are Watson Factor's pitch, not HomeRepair's
    for f in findings:
        if len(noticed) >= 4:
            break
        t = (f.get("title") or "").strip()
        plain = next((line for rx, line in OWNER_FINDINGS if re.search(rx, t, re.I)), None)
        if plain and plain not in noticed:
            noticed.append(plain)
    if p.get("rating") and p.get("reviews") and "Reviews & Rewards" in offers[:2] and len(noticed) < 4:
        noticed.append("%.1f stars from %d Google reviews." % (float(p["rating"]), int(p["reviews"])))

    # the receptionist scene is the one the emails already paint
    rec = {}
    story = receptionist_story(p)
    if story:
        _, picture, fix, _ = story
        rec = {"picture": picture.replace("\n\n", " "),
               "with": re.sub(r"^Here's what I'd do\.\s*", "", fix)}
    else:
        setup, _place, busy = STORY_BY_VERTICAL.get(p.get("vertical") or "", STORY_BY_VERTICAL["generic"])
        rec = {"picture": "%s %s" % (setup, busy),
               "with": "When you can't pick up, an AI assistant answers - nights and weekends too - "
                       "gets their name, number and what they need, and texts it to you within "
                       "seconds. You call back when your hands are free."}
    tz = workspace_tz()
    return {
        "id": pid, "company": p.get("company") or "Your business",
        "contact": (ct["name"] if ct else ""), "role": (ct["role"] if ct else ""),
        "vertical": p.get("vertical") or "", "category": p.get("category") or "",
        "city": p.get("city") or "", "phone": p.get("phone") or "", "domain": p.get("domain") or "",
        "rating": ("%.1f" % float(p["rating"])) if p.get("rating") else "",
        "reviews": p.get("reviews") or "", "opener": p.get("opener") or "",
        "offers": offers, "noticed": noticed[:4], "copy": offer_copy_map(),
        "receptionist": rec, "date": datetime.now(tz).strftime("%B %-d, %Y"),
        **(HR_SHEET_INTRO.get(homerepair_segment(p), HR_SHEET_INTRO["property_manager"])(p)
           if ws.current() == "homerepair" else {}),
    }


def leave_behind_brand() -> dict:
    b = report_brand()
    return {"brand": b["brand"], "logo": b["logo"],
            "name": setting("sender_name", "").strip(),
            "phone": setting("sender_phone", "").strip(),
            "email": setting("sender_email", "").strip(),
            "site": setting("sender_site", "").strip(),
            "accent": ws.info().get("accent") or "#e8a33d"}


@app.get("/leave-behind/{pid:int}", response_class=HTMLResponse)
def leave_behind(request: Request, pid: int):
    require_auth(request)
    d = leave_behind_data(pid)
    return HTMLResponse(lb.render(d, leave_behind_brand(), toolbar="/leave-behind/%d.pdf" % pid),
                        headers={"Cache-Control": "no-cache"})


@app.get("/leave-behind/{pid:int}.pdf")
def leave_behind_pdf(request: Request, pid: int):
    require_auth(request)
    from grit_analyzer.pdfgen import PdfUnavailable, html_to_pdf
    d = leave_behind_data(pid)
    try:
        pdf = html_to_pdf(lb.render(d, leave_behind_brand()))
    except PdfUnavailable as ex:
        raise HTTPException(501, detail=str(ex))
    name = re.sub(r"[^A-Za-z0-9]+", "-", d["company"]).strip("-").lower() or "lead"
    with closing(db()) as c:
        log(c, pid, "note", "leave_behind", "Printed a leave-behind sheet")
        c.commit()
    return Response(pdf, media_type="application/pdf", headers={
        "content-disposition": 'attachment; filename="leave-behind-%s.pdf"' % name})


LEAVE_BEHIND_MAX = 25   # one print run; a route bigger than this is two trips anyway


def leave_behind_ids(ids: str) -> list:
    out = []
    for x in (ids or "").split(","):
        x = x.strip()
        if x.isdigit() and int(x) not in out:
            out.append(int(x))
    if not out:
        raise HTTPException(400, "No leads picked.")
    return out[:LEAVE_BEHIND_MAX]


@app.get("/leave-behinds", response_class=HTMLResponse)
def leave_behinds(request: Request, ids: str = "", title: str = "Today's route"):
    """The route stack: every picked lead's sheet in one document, in order."""
    require_auth(request)
    pids = leave_behind_ids(ids)
    ds = [leave_behind_data(pid) for pid in pids]
    from urllib.parse import quote
    return HTMLResponse(lb.render_many(ds, leave_behind_brand(), title=title,
                                       toolbar="/leave-behinds.pdf?ids=%s&title=%s" % (",".join(map(str, pids)), quote(title))),
                        headers={"Cache-Control": "no-cache"})


@app.get("/leave-behinds.pdf")
def leave_behinds_pdf(request: Request, ids: str = "", title: str = "Today's route"):
    require_auth(request)
    from grit_analyzer.pdfgen import PdfUnavailable, html_to_pdf
    pids = leave_behind_ids(ids)
    ds = [leave_behind_data(pid) for pid in pids]
    try:
        pdf = html_to_pdf(lb.render_many(ds, leave_behind_brand(), title=title), timeout=120)
    except PdfUnavailable as ex:
        raise HTTPException(501, detail=str(ex))
    with closing(db()) as c:
        for pid in pids:
            log(c, pid, "note", "leave_behind", "Printed a leave-behind sheet (%s)" % title[:60])
        c.commit()
    name = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower() or "route"
    return Response(pdf, media_type="application/pdf", headers={
        "content-disposition": 'attachment; filename="leave-behinds-%s.pdf"' % name})


@app.get("/api/export.csv")
def export_csv(request: Request):
    require_auth(request)
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM prospects ORDER BY id").fetchall()
    buf = io.StringIO()
    cols = ["company", "domain", "phone", "city", "address", "rating", "reviews",
            "score", "grade", "gap", "say", "opener", "status", "note", "email"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(dict(r))
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="just-grit-prospects.csv"'})


@app.get("/api/analyze")
def api_analyze(request: Request, url: str = Query(..., min_length=3),
                seo: bool = True):
    """Live single-site audit JSON — powers the cockpit's Site audit panel."""
    require_auth(request)
    if is_suppressed(url):
        raise HTTPException(451, "That domain is on the suppression list.")
    d = analyze(url, deep=True)
    if seo and d.get("ok"):
        # Additive: the plain-English SEO read. Never let it break the audit.
        try:
            from seo_synopsis import synopsis_for
            d["seo"] = synopsis_for(d.get("final_url") or d.get("host") or url,
                                    (d.get("scores", {}).get("search") or {}).get("score"))
        except Exception as e:
            d["seo"] = {"ok": False, "verdict": f"SEO read unavailable ({type(e).__name__}).",
                        "paragraphs": [], "actions": [], "facts": {}, "unknowns": []}
    return d


class SaveAuditBody(BaseModel):
    url: str
    vertical: Optional[str] = "generic"


@app.post("/api/analyze/save")
def save_audit_to_crm(request: Request, body: SaveAuditBody):
    """Put the site you just audited into the pipeline.

    Deliberately does NOT store the score the browser is holding. It creates
    the row and hands it to the same background scanner every other prospect
    goes through, so the stored audit is server-derived and the name-discovery
    pass runs too. The record appears immediately and fills in within a few
    seconds — same behaviour as a Scout find.

    Re-adding a domain already in the pipeline returns the existing record
    rather than creating a duplicate, so the button is safe to press twice.
    """
    require_auth(request)
    dom = domain_of(body.url)
    if not dom or not re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", dom, re.I):
        raise HTTPException(400, "That doesn't look like a website address.")
    if is_suppressed(dom):
        raise HTTPException(451, "That domain is on the do-not-contact list.")

    with closing(db()) as c:
        row = c.execute(
            "SELECT id, company, stage FROM prospects WHERE domain=?", (dom,)
        ).fetchone()
        if row:
            return {"ok": True, "existing": True, "id": row["id"],
                    "company": row["company"], "stage": row["stage"] or "lead"}

        try:
            cur = c.execute(
                """INSERT INTO prospects
                   (place_id, company, domain, vertical, added_at, stage, stage_at)
                   VALUES (?,?,?,?,?,'lead',?)""",
                (f"audit:{dom}", dom, dom, body.vertical or "generic", now(), now()))
        except sqlite3.IntegrityError:
            row = c.execute("SELECT id, company, stage FROM prospects WHERE domain=?",
                            (dom,)).fetchone()
            if row:
                return {"ok": True, "existing": True, "id": row["id"],
                        "company": row["company"], "stage": row["stage"] or "lead"}
            raise
        pid = cur.lastrowid
        # Due today, first step is a call — same footing as any other new lead.
        c.execute("UPDATE prospects SET next_due=date('now'), next_action='call', "
                  "step=1 WHERE id=?", (pid,))
        log(c, pid, "note", note=f"Added from the analyzer after auditing {dom}.")
        c.commit()

    queue_scans([pid])
    return {"ok": True, "existing": False, "id": pid, "company": dom}


@app.get("/api/seo")
def api_seo(request: Request, url: str = Query(..., min_length=3)):
    """SEO synopsis on its own, without a full audit."""
    require_auth(request)
    if is_suppressed(url):
        raise HTTPException(451, "That domain is on the suppression list.")
    from seo_synopsis import synopsis_for
    return synopsis_for(url)


class BatchBody(BaseModel):
    urls: list = Field(..., min_length=1, max_length=250)
    fast: bool = True


@app.post("/api/batch")
def api_batch(request: Request, body: BatchBody):
    """Rank a pasted prospect list worst-first — the cockpit's Market scan."""
    require_auth(request)
    urls = [u for u in body.urls if isinstance(u, str) and u.strip()
            and not is_suppressed(u)][:250]
    rows = analyze_batch(urls, deep=not body.fast)
    return {"results": rows, "suppressed_skipped": len(body.urls) - len(urls)}


@app.get("/api/usage")
def usage_route(request: Request):
    """This month's Google request count vs the free-tier cap."""
    require_auth(request)
    cap = request_cap()
    used = usage_count()
    return {"month": month_key(), "used": used, "cap": cap,
            "remaining": max(0, cap - used) if cap > 0 else None,
            "capped": cap > 0}


# ── when the person who can say yes is actually reachable ────────────────
# These are not opening hours. They are the GAPS in them. A restaurant owner
# at 12:30 is not going to have a conversation about software - you get the
# closest warm body, a distracted no, and a lead you can never call fresh
# again. Calling in the wrong hour doesn't just fail, it spends the lead.
#
# Every one of these is judgement, not measurement - nobody has run enough
# calls through this to know what actually connects. Change them the moment
# the real answer rates say otherwise; they are here to be overwritten.
CALL_WINDOWS = {
    "restaurant":  [(14, 0, 16, 0)],                   # after lunch, before dinner service
    "auto":        [(9, 0, 11, 0), (14, 0, 16, 0)],    # past the morning drop-off rush
    "appointment": [(14, 0, 16, 0)],                   # front desk is calmest mid-afternoon
    # A chiro clinic runs in two bursts - the before-work adjustment rush and
    # the after-work one. The owner is findable in the gap between them, and
    # most of these literally close for it.
    "chiro":       [(11, 30, 13, 30), (14, 0, 15, 30)],
    "contractor":  [(7, 30, 9, 0), (16, 0, 18, 0)],    # before the job, or off the roof
    "generic":     [(10, 0, 11, 30), (14, 0, 16, 0)],
}


def _fmt_hm(h, m):
    ampm = "am" if h < 12 else "pm"
    hh = h % 12 or 12
    return ("%d:%02d%s" % (hh, m, ampm)) if m else ("%d%s" % (hh, ampm))


def call_windows(vertical: str):
    """This workspace's windows for a vertical, falling back to the defaults.

    A workspace can store its own in settings (that is what a snapshot
    carries). Falling back per-vertical rather than all-or-nothing matters:
    a clinic snapshot that only knows about `chiro` should not blank out the
    restaurant windows for a workspace that also calls restaurants.
    """
    vert = (vertical or "generic").strip()
    own = _cadence(snap.SETTING_WINDOWS, {})
    if vert in own:
        return own[vert]
    if "generic" in own and vert not in CALL_WINDOWS:
        return own["generic"]
    return CALL_WINDOWS.get(vert, CALL_WINDOWS["generic"])


def followup_gaps() -> dict:
    return _cadence(snap.SETTING_GAPS, WATSON_FOLLOWUP_GAPS)


def max_email_step() -> int:
    return _cadence(snap.SETTING_MAX_STEP, MAX_WATSON_EMAIL_STEP)


def window_label(vertical: str) -> str:
    return " / ".join("%s-%s" % (_fmt_hm(a, b), _fmt_hm(c2, d))
                      for a, b, c2, d in call_windows(vertical))


def callable_now(vertical: str, when=None) -> bool:
    """Is this a decent moment to ring them?

    Local time on purpose - the queue is read on Daniel's Mac in the same
    timezone as the businesses he is calling. Weekends are out for everyone:
    the owner is either working the floor or not working at all.
    """
    when = when or datetime.now()
    if when.weekday() >= 5:
        return False
    mins = when.hour * 60 + when.minute
    for a, b, c2, d in call_windows(vertical):
        if a * 60 + b <= mins < c2 * 60 + d:
            return True
    return False


class NewWorkspaceBody(BaseModel):
    slug: str
    name: str
    short: str = ""
    product: str = "Just Grit"
    host: str = ""
    accent: str = "#e8a33d"
    sells: str = ""
    buyer: str = ""


@app.post("/api/workspaces")
def create_workspace(request: Request, body: NewWorkspaceBody):
    """Onboard a customer without touching code.

    This route is the difference between a tool and a product. Adding a
    workspace used to mean editing workspaces.py, hand-creating a database,
    wiring a hostname and redeploying - a development task attached to every
    sale. Now it is a row and a file, created here, live immediately.

    What it deliberately does NOT do is relax isolation. The new customer gets
    their own database file, exactly like the two built-ins, for the reason
    workspaces.py gives: leaks you cannot write beat leaks you merely try hard
    to avoid.

    Owners only: a guest of one customer creating workspaces is the
    registry, and its hostnames, in a stranger's hands.
    """
    require_owner(request)
    return _create_workspace(body)


def _create_workspace(body) -> dict:
    slug = (body.slug or "").strip().lower()
    if not ws.valid_slug(slug):
        raise HTTPException(400,
            "A slug is lowercase letters, numbers and dashes, 2-31 characters - "
            "it becomes a filename, so nothing else is safe.")
    if ws.exists(slug):
        raise HTTPException(409, "There is already a workspace called %r." % slug)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Give the workspace a name.")

    dbfile = "justgrit-%s.db" % slug
    if (DATA / dbfile).exists():
        raise HTTPException(409,
            "A database file for %r already exists. Pick another slug, or move "
            "that file aside first - this will not open a database it did not "
            "create." % slug)

    hosts = [h.strip().lower() for h in (body.host or "").split(",") if h.strip()]
    # A hostname can only point at one workspace. ws.from_host returns the
    # first match in registration order, so a duplicate would be accepted
    # here, reported as live, and silently route that customer's traffic to
    # whichever older workspace already owned the name (QT-4).
    for h in hosts:
        taken = ws.from_host(h)
        if taken:
            raise HTTPException(409,
                "%s already routes to the %r workspace. A hostname can only "
                "point at one workspace." % (h, taken))
    with closing(db(ws.PRIMARY)) as c:
        c.execute("INSERT INTO workspace_registry "
                  "(slug,name,short,product,db,hosts,accent,sells,buyer,active,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                  (slug, name, (body.short or name).strip(), body.product.strip(),
                   dbfile, json.dumps(hosts), body.accent.strip(),
                   body.sells.strip(), body.buyer.strip(), now()))
        c.commit()

    ws.reload_registry()
    # Build the customer's own database with the full schema, in their context.
    token = ws.CURRENT.set(slug)
    try:
        init_db()
    finally:
        ws.CURRENT.reset(token)

    return {"ok": True, "slug": slug, "db": dbfile, "hosts": hosts,
            "next": "Add this workspace's offer copy in its Setup before drafting "
                    "outreach - until then it will refuse, so it cannot borrow "
                    "another business's pitch."}


# ── the guided start: a new business in one pass ─────────────────────────
# Phase 2 of the commercial plan (2026-09-26). Ari: "a bunch of apps stacked
# on top of each other." The answer is one flow, /start, that creates the
# workspace, fills in who sends, what they sell (through the same intake the
# Setup page uses), who gets in, and hands the owner to Getting Started with
# the sensible defaults already on. Everything it writes was already
# writable one screen at a time; this is the screens in order.

def slug_from_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:28] or "business"
    slug, n = base, 2
    while ws.exists(slug) or (DATA / ("justgrit-%s.db" % slug)).exists():
        slug = "%s-%d" % (base[:25], n); n += 1
    return slug


class StartBody(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    website: str = ""
    sells: str = ""
    buyer: str = ""
    city: str = ""
    trade: str = ""
    offers: list = []                      # [{name, trigger, costs, does}]
    sender_name: str = ""
    sender_email: str = ""
    sender_phone: str = ""
    sender_address: str = ""
    guests: str = ""                       # who at the customer may sign in, comma-separated
    never_say: str = ""
    brand_tag: str = ""


@app.post("/api/start")
def start_business(request: Request, body: StartBody):
    """Create a business and set it up in one call. Owner only - on the Mac
    the owner onboards the customer; self-serve sign-up comes with the
    cloud move (Phase 4)."""
    require_owner(request)
    name = body.name.strip()
    site = re.sub(r"^https?://", "", body.website.strip().lower()).split("/")[0]
    slug = slug_from_name(name)
    short = name.split(" ")[0] if len(name) > 18 else name
    created = _create_workspace(NewWorkspaceBody(slug=slug, name=name, short=short, product="Just Grit",
                                                 host="", accent="#e8a33d", sells=body.sells.strip(), buyer=body.buyer.strip()))
    problems = []
    tok = ws.CURRENT.set(slug)
    try:
        for k, v in (("sender_name", body.sender_name), ("sender_email", body.sender_email),
                     ("sender_phone", body.sender_phone), ("sender_address", body.sender_address),
                     ("sender_company", name), ("sender_site", site), ("default_city", body.city),
                     ("never_say", body.never_say), ("brand_tag", body.brand_tag)):
            if (v or "").strip():
                set_setting(k, v.strip())
        if body.brand_tag.strip():
            set_setting("brand_tag_on", "1")
        guests = [g.strip().lower() for g in re.split(r"[,\s]+", body.guests or "") if "@" in g]
        if guests:
            set_setting("workspace_emails", ", ".join(guests))
        if body.trade.strip():
            set_setting(FOCUS_SETTING, json.dumps([body.trade.strip()]))
        # the offers, through the same intake the Setup page uses
        draft = intake(request, IntakeBody(domain=site, sells=body.sells, buyer=body.buyer, offers=body.offers))
        problems += draft.get("notes", [])
        offers = draft["draft"].get("offers") or {}
        if offers:
            kept = parse_catalog(json.dumps({"offers": offers})).get("offers", {})
            if kept:
                set_setting(CATALOG_SETTING, json.dumps({"offers": kept}))
            else:
                problems.append("The offers didn't come through complete - add them under Setup → What you sell.")
        else:
            problems.append("No offers yet - the app won't write emails until Setup → What you sell has one.")
        set_setting("autopilot_mode", "")          # off until they say go
        with closing(db()) as c:
            campaigns_seed(c)
    finally:
        ws.CURRENT.reset(tok)
    return {"ok": True, "slug": slug, "name": name, "open": "/?ws=" + slug,
            "offers": sorted((offers or {}).keys()), "problems": problems,
            "next": "Open the workspace - Getting Started on Today lists what's left: the mailbox, "
                    "the first leads, and switching autopilot on."}


# ── sign in by email code ────────────────────────────────────────────────
# See auth.py. This is the door a stranger can open: /login asks for the
# address, /auth/login emails a code through the owner's mailbox,
# /auth/verify trades the code for a session cookie. Which workspaces that
# address may open is still require_auth's decision.
LOGIN = ROOT / "static" / "login.html"
_login_smtp_factory = None      # tests swap in a fake SMTP


def known_emails() -> set:
    """Every address that may sign in somewhere: the owners plus each
    workspace's guests."""
    out = set(allowed_emails())
    for slug in list(ws.WORKSPACES):
        try:
            with closing(db(slug)) as c:
                r = c.execute("SELECT v FROM settings WHERE k='workspace_emails'").fetchone()
            if r and r["v"]:
                out |= {e.strip().lower() for e in r["v"].replace("\n", ",").split(",") if e.strip()}
        except sqlite3.Error:
            continue
    return out


def send_login_code(email: str, code: str) -> str:
    """Email the code through the primary workspace's mailbox. Returns ''
    when it left, else a short reason for the owner."""
    tok = ws.CURRENT.set(ws.PRIMARY)
    try:
        host, user, pw = mailbox_creds()
        if not (host and user and pw):
            return "no_mailbox"
        from_addr = setting("sender_email", "").strip() or user
        from_name = setting("sender_name", "").strip() or "Just Grit"
        smtp_host = setting("smtp_host", "").strip() or mailer.smtp_host_for(host)
        smtp_port = int(setting("smtp_port", "") or mailer.DEFAULT_SMTP_PORT)
        subject, body = auth.code_email(code)
        msg = mailer.build(from_addr, from_name, email, subject, body)
        mailer.send(smtp_host, smtp_port, user, pw, msg, smtp_factory=_login_smtp_factory)
        return ""
    except mailer.MailError as e:
        print("[login] code email failed:", str(e)[:200])
        return "mail_failed"
    finally:
        ws.CURRENT.reset(tok)


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if caller_email(request):
        return Response(status_code=302, headers={"Location": "/"})
    if not LOGIN.exists():
        raise HTTPException(500, "login.html missing")
    return HTMLResponse(LOGIN.read_text(encoding="utf-8"))


class LoginBody(BaseModel):
    email: str = ""


class VerifyBody(BaseModel):
    email: str = ""
    code: str = ""


@app.post("/auth/login")
def auth_login(request: Request, body: LoginBody):
    email = auth.norm(body.email)
    if not auth.looks_like_email(email):
        raise HTTPException(400, "That doesn't look like an email address.")
    if email not in known_emails():
        raise HTTPException(403, "That address isn't on this account. Ask the owner to add you under Setup → Who can sign in.")
    with closing(auth_db()) as c:
        code, why = auth.issue_code(c, email)
    if why == "too_many":
        raise HTTPException(429, "Too many codes in a row. Wait 15 minutes and try again.")
    err = send_login_code(email, code)
    out = {"ok": True}
    if err:
        host = (request.client.host if request.client else "") or ""
        if host in ("127.0.0.1", "::1", "localhost") and not via_cloudflare(request):
            print("[login] no mailbox to send through; code for %s is %s" % (email, code))
            out["note"] = "No mailbox is connected yet, so the code was written to the server log instead of emailed."
        else:
            raise HTTPException(503, "We couldn't send the code just now. Try again in a minute, or text the owner.")
    return out


@app.post("/auth/verify")
def auth_verify(request: Request, body: VerifyBody):
    email = auth.norm(body.email)
    with closing(auth_db()) as c:
        token, why = auth.verify_code(c, email, body.code)
    if why == "expired":
        raise HTTPException(400, "That code has expired. Send a new one.")
    if why == "burned":
        raise HTTPException(400, "Too many wrong tries. Send a new code.")
    if why == "wrong":
        raise HTTPException(400, "That code doesn't match. Check the email and try again.")
    resp = Response(content=json.dumps({"ok": True, "open": "/"}), media_type="application/json")
    resp.set_cookie(auth.COOKIE, token, max_age=60 * 60 * 24 * auth.SESSION_DAYS, httponly=True,
                    samesite="lax", secure=via_cloudflare(request), path="/")
    return resp


@app.post("/auth/logout")
@app.get("/logout", include_in_schema=False)
def auth_logout(request: Request):
    tok = (request.cookies.get(auth.COOKIE) or "").strip()
    if tok:
        with closing(auth_db()) as c:
            auth.end_session(c, tok)
    resp = Response(status_code=302, headers={"Location": "/login"})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


def _wants_html(request: Request) -> bool:
    p = request.url.path
    return "text/html" in (request.headers.get("accept") or "") and not p.startswith(("/api/", "/auth/", "/webhooks/"))


DENIED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not on this account · Just Grit</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0d0d0d;color:#fff;font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;padding:16px}
.card{max-width:420px;background:#16161a;border:1px solid rgba(255,255,255,.1);border-radius:14px;padding:26px}h1{font-size:20px;margin:0 0 8px}p{color:#c3c2b7;margin:0 0 16px}
a.btn{display:inline-block;background:#e7410a;color:#fff;text-decoration:none;font-weight:650;padding:10px 18px;border-radius:10px}a.plain{color:#8f8d86;margin-left:14px}</style></head>
<body><div class="card"><h1>%(title)s</h1><p>%(text)s</p><a class="btn" href="/logout">Sign in with another email</a><a class="plain" href="mailto:daniel@thewatsonfactor.com">Ask for access</a></div></body></html>"""


@app.exception_handler(HTTPException)
async def _friendly_http_errors(request: Request, exc: HTTPException):
    """A person at a browser gets a page and a way forward, never a JSON
    blob or a bare string. The API keeps FastAPI's {"detail": ...}."""
    if _wants_html(request):
        if exc.status_code == 401:
            nxt = urllib.parse.quote(request.url.path or "/")
            return Response(status_code=302, headers={"Location": "/login?next=%s" % nxt})
        if exc.status_code == 403:
            who = caller_email(request) or ""
            title = "This email isn't on the account" if who else "Can't open this"
            text = ("%s can sign in, but not to this workspace. If you should have access, ask the owner to add you." % html.escape(who)
                    if who else html.escape(str(exc.detail)))
            return HTMLResponse(DENIED_PAGE % {"title": title, "text": text}, status_code=403)
    return Response(content=json.dumps({"detail": exc.detail}), status_code=exc.status_code,
                    media_type="application/json", headers=getattr(exc, "headers", None) or {})


START = ROOT / "static" / "start.html"


@app.get("/start", include_in_schema=False)
def start_page(request: Request):
    require_owner(request)
    if not START.exists():
        raise HTTPException(500, "start.html missing")
    return HTMLResponse(START.read_text(encoding="utf-8"))


# ── onboarding: turning a business into a catalog ────────────────────────
# The question this answers is "how would the system know how to sell for
# somebody else." It does not, and it never will by reading their website -
# a homepage says what a company sells, never what is broken at a customer
# the moment before they buy. That second thing IS the evidence rule, it is
# the only input that cannot be derived from anything public, and every owner
# can answer it off the top of their head because it is the story of every
# sale they have ever made.
#
# So intake asks it, in those words, once per offer. Everything returned here
# is built from the owner's own sentences - nothing is invented and nothing is
# asserted on their behalf - and it is returned as a DRAFT for them to edit
# rather than saved, because a machine turning "they get voicemail after six"
# into a regex is a guess that deserves a human's eye before it picks who
# gets written to.
_STOP = set("""a an the and or but if then than that this these those there here
it its it's is are was were be been being am do does did doing have has had
having i we you they he she them us our your their my me him her of in on at to
for from with without by as into over under about after before during while
when where how what which who whom why not no nor so too very can will just
get got gets getting go goes going come comes coming make makes made making
take takes took taking give gives gave say says said see sees saw know knows
knew think thinks want wants need needs use uses used more most much many some
any all every each other another same own out up down off again once only
because until also always never often sometimes usually really quite even
still yet ever thing things stuff lot lots like likes""".split())

_SYNONYMS = {
    "voicemail": r"voice ?mail|voicemail",
    "answering": r"answer(s|ed|ing)?",
    "answer": r"answer(s|ed|ing)?",
    "booking": r"book(s|ed|ing)?",
    "book": r"book(s|ed|ing)?",
    "waiting": r"wait(s|ed|ing)?",
    "wait": r"wait(s|ed|ing)?",
    "calling": r"call(s|ed|ing)?",
    "call": r"call(s|ed|ing)?",
    "reviews": r"review(s)?",
    "review": r"review(s)?",
    "website": r"web ?site|website",
}


def trigger_to_evidence(text: str, limit: int = 6) -> str:
    """Their sentence about what's wrong, as something the matcher can use.

    Deliberately crude and deliberately visible. This is a first guess shown
    in an editable field, not a clever inference - the owner reads it back and
    fixes it, which is the only way it ends up right.
    """
    words, seen = [], set()
    for raw in re.findall(r"[a-z']{3,}", (text or "").lower()):
        w = raw.strip("'")
        if len(w) < 4 or w in _STOP or w in seen:
            continue
        seen.add(w)
        words.append(_SYNONYMS.get(w, re.escape(w)))
        if len(words) >= limit:
            break
    return "|".join(words)


def _sentence(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


def _fragment(s: str, words: int = 8) -> str:
    """A subject-line tail: lowercase, no full stop, short."""
    s = re.sub(r"\s+", " ", (s or "").strip().rstrip(".!?"))
    parts = s.split(" ")
    out = " ".join(parts[:words])
    return out[0].lower() + out[1:] if out else ""


class IntakeOffer(BaseModel):
    name: str = ""
    trigger: str = ""          # what's wrong with them right before they buy
    costs: str = ""            # what that costs them
    does: str = ""             # what this business does about it


class IntakeBody(BaseModel):
    domain: str = ""
    sells: str = ""
    buyer: str = ""
    offers: list = []


@app.post("/api/intake")
def intake(request: Request, body: IntakeBody):
    """Four questions per offer in, a draft catalog out. Saves nothing.

    Returning a draft rather than writing one is the whole point: the owner
    sees the regex that will decide who gets written to, in a field they can
    change, before a single email exists.
    """
    require_auth(request)
    notes, offers = [], {}

    dom = (body.domain or "").strip().lower().replace("https://", "").replace("http://", "")
    dom = dom.split("/")[0]
    if dom:
        try:
            site = analyze(dom, deep=False)
            if not site.get("ok"):
                notes.append("Couldn't read %s (%s) — the analyzer and the website "
                             "pitch will be blind until that resolves."
                             % (dom, (site.get("error") or "no response")[:60]))
        except Exception as e:
            notes.append("Couldn't read %s (%s)." % (dom, type(e).__name__))

    for raw in (body.offers or []):
        try:
            o = IntakeOffer(**raw) if isinstance(raw, dict) else raw
        except Exception:
            continue
        name = (o.name or "").strip()
        if not name:
            continue
        trigger, costs, does = (o.trigger or "").strip(), (o.costs or "").strip(), (o.does or "").strip()
        if not (trigger and costs and does):
            notes.append("%r needs all three answers — what's wrong, what it costs "
                         "them, and what you do about it. Skipped." % name)
            continue
        ev = trigger_to_evidence(trigger)
        if not ev:
            notes.append("%r: couldn't pull keywords out of %r, so it will pitch only "
                         "when nothing more specific matches. Worth writing by hand."
                         % (name, trigger[:40]))
        offers[name] = {
            "tail": _fragment(trigger),
            "cost": _sentence(costs),
            "fix": _sentence(does),
            "evidence": ev,
            "question": "How does %s handle " + _fragment(trigger, 6) + " right now?",
        }

    # the question template needs its %s in the company slot, not ours
    for k, v in offers.items():
        v["question"] = v["question"].replace("How does %s handle",
                                              "How does %s handle", 1)

    return {"draft": {"offers": offers},
            "workspace": {"sells": (body.sells or "").strip(),
                          "buyer": (body.buyer or "").strip(),
                          "domain": dom},
            "notes": notes,
            "next": "Read the trigger words on each offer before saving — that line "
                    "decides who gets written to."}


class CatalogBody(BaseModel):
    catalog: dict


@app.get("/api/catalog")
def get_catalog(request: Request):
    """This workspace's offers. The built-ins report theirs as read-only."""
    require_auth(request)
    saved = catalog().get("offers", {})
    if ws.current() in CATALOG_WORKSPACES:
        return {"builtin": True, "editable": True,
                "offers": saved or builtin_catalog()["offers"],
                "customised": bool(saved), "ready": True,
                "note": ("" if saved else
                         "These are the built-in offers. Edit, add or remove any of them "
                         "and press Save - from then on your version is the one used.")}
    return {"builtin": False, "editable": True, "customised": bool(saved),
            "offers": saved, "ready": has_catalog()}


@app.post("/api/catalog")
def save_catalog(request: Request, body: CatalogBody):
    """Write this workspace's offers.

    Validated on the way IN, not at send time. A malformed offer that slips
    through here would surface as a broken sentence in a stranger's inbox,
    which is the one place this system must never fail quietly.
    """
    require_auth(request)
    cleaned = parse_catalog(json.dumps(body.catalog or {}))
    kept = cleaned.get("offers", {})
    asked = (body.catalog or {}).get("offers") or {}
    dropped = [k for k in asked if k not in kept]
    if not kept:
        raise HTTPException(400,
            "No usable offers. Each one needs a name plus tail, cost and fix - "
            "the three sentences an email is built from.")
    set_setting(CATALOG_SETTING, json.dumps({"offers": kept}))
    return {"ok": True, "saved": sorted(kept),
            "dropped": dropped,
            "note": ("Dropped %d incomplete offer(s): %s"
                     % (len(dropped), ", ".join(dropped))) if dropped else ""}


@app.post("/api/catalog/reset")
def reset_catalog(request: Request):
    """Built-ins only: forget the saved edits and go back to the code copy."""
    require_auth(request)
    if ws.current() not in CATALOG_WORKSPACES:
        raise HTTPException(400, "This workspace has no built-in copy to go back to.")
    set_setting(CATALOG_SETTING, "")
    return {"ok": True, "offers": sorted(builtin_catalog()["offers"])}


class FbPagesBody(BaseModel):
    user_token: str
    page_id: str = ""


@app.post("/api/facebook/pages")
def facebook_pages(request: Request, body: FbPagesBody):
    """Which Pages does this user token manage? Names and ids only - the
    page tokens stay on the server side of the exchange. This is what lets
    Setup show a picker instead of asking a business owner for a page id."""
    require_auth(request)
    tok = body.user_token.strip()
    try:
        rows = fb.pages(tok)
    except fb.FacebookError as e:
        raise HTTPException(400, str(e))
    # Pages reached through a business portfolio don't come back from
    # me/accounts. Try the portfolio's own list, the Page id typed in, and the
    # one this workspace was connected to before.
    seen = {str(p.get("id")) for p in rows}
    extra = fb.business_pages(tok)
    for pid in [body.page_id.strip(), setting("fb_page_id", "").strip()]:
        if pid and pid not in seen:
            d = fb.page_direct(tok, pid)
            if d.get("access_token"):
                extra.append(d)
    for p in extra:
        if str(p.get("id")) not in seen:
            rows.append(p); seen.add(str(p.get("id")))
    exp = fb.token_expiry(tok)
    return {"pages": [{"id": str(p.get("id")), "name": p.get("name") or str(p.get("id"))}
                      for p in rows],
            "short_lived": bool(exp.get("expires_at")) and exp["expires_at"] - time.time() < 6 * 3600}


@app.get("/api/setup_status")
def setup_status(request: Request):
    """The checklist Setup is drawn from. One line per section: done, partly,
    todo or optional, and the one thing missing - so the page can say
    '4 of 7 done' instead of showing eleven forms and hoping."""
    require_auth(request)
    s = lambda k: (setting(k, "") or "").strip()
    owner = is_owner(request)
    fbc = bool(s("fb_page_id") and s("fb_page_token"))
    igc = bool(s("ig_user_id") and (s("ig_token") or s("fb_page_token")))
    tb_ready = telnyx_settings_ready()
    biz_missing = [k for k, v in (("name", s("sender_name")), ("phone", s("sender_phone")),
                                  ("email", s("sender_email")),
                                  ("mailing address", s("sender_address"))) if not v]
    ig = imagegen_status()
    with closing(db()) as c:
        own_pics = c.execute("SELECT COUNT(*) FROM media_library WHERE removed_at IS NULL").fetchone()[0]
    pics_ok = bool(ig["ready"] or own_pics)
    sections = [
        {"key": "business", "label": "Your business", "required": True,
         "state": "done" if not biz_missing else ("partial" if len(biz_missing) < 4 else "todo"),
         "missing": ("Missing: " + ", ".join(biz_missing)) if biz_missing else ""},
        {"key": "offers", "label": "What you sell", "required": True,
         "state": "done" if has_catalog() else "todo",
         "missing": "" if has_catalog() else "Add at least one offer - emails are built from them."},
        {"key": "phone", "label": "Phone line & text-back", "required": False,
         "state": ("done" if tb_ready and textback_enabled()
                   else "partial" if tb_ready or s("telnyx_api_key") else "todo"),
         "missing": ("" if tb_ready and textback_enabled()
                     else "Text-back is off." if tb_ready
                     else "Connect your Telnyx line.")},
        {"key": "mailbox", "label": "Your mailbox", "required": False,
         "state": "done" if (s("imap_host") and s("imap_password")) else
                  "partial" if s("imap_host") else "todo",
         "missing": "" if (s("imap_host") and s("imap_password")) else
                    "Until it's connected, replies and bounces are invisible."},
        {"key": "social", "label": "Facebook & Instagram", "required": False,
         "state": "done" if (fbc and igc) else "partial" if fbc else "todo",
         "missing": "" if (fbc and igc) else "Instagram not linked." if fbc else "Not connected."},
        {"key": "pictures", "label": "Pictures", "required": False,
         "state": "done" if pics_ok else "todo",
         "missing": "" if pics_ok else ("Add your own photos under Social → Your pictures, "
                                        "or connect an image provider.")},
        {"key": "maps", "label": "Lead finder", "required": False,
         "state": "done" if google_key() else "todo",
         "missing": "" if google_key() else "A Google Maps key lets the app find businesses."},
    ]
    if owner:
        sections.append({"key": "customers", "label": "Customers", "required": False,
                         "state": "info", "missing": ""})
    req = [x for x in sections if x["required"]]
    done_req = sum(1 for x in req if x["state"] == "done")
    done_all = sum(1 for x in sections if x["state"] == "done")
    countable = [x for x in sections if x["state"] != "info"]
    return {"sections": sections, "owner": owner,
            "required_done": done_req, "required_total": len(req),
            "done": done_all, "total": len(countable),
            "ready_to_work": done_req == len(req)}


@app.get("/api/calendar")
def calendar_week(request: Request, week: str = ""):
    """The week, without the noise. Daniel, 2026-09-27: the board listed
    every email sent and every lead due, and it read as a wall. Now a day
    shows three kinds of thing:
      - one all-day "to work" card: how many leads are due, by action
      - the real events: posts going out, and conversations (a person
        replied, a call or text came in)
      - a 5:00 PM "day recap": what got done, in counts, with the names
        folded away behind a click.
    Times are the workspace's local time."""
    require_auth(request)
    tz = workspace_tz()
    now_local = datetime.now(tz)
    today = now_local.date()
    try:
        wk = socialweek.monday_of(week) if week else socialweek.monday_of(socialweek.week_of(today))
    except Exception:
        wk = socialweek.monday_of(socialweek.week_of(today))
    start = datetime(wk.year, wk.month, wk.day, tzinfo=tz)
    end = start + timedelta(days=7)
    s_utc, e_utc = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    today_s = today.strftime("%Y-%m-%d")
    items = []
    due = {d: {"total": 0, "by": {}, "names": []} for d in days}
    recap = {d: {"sent": 0, "followups": 0, "replies": 0, "bounces": 0, "unsubs": 0, "calls": 0, "texts": 0,
                 "posts": 0, "failed_posts": 0, "new_leads": 0, "moves": {}, "log": []} for d in days}

    def local(iso):
        try:
            t = datetime.fromisoformat(str(iso))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return t.astimezone(tz)
        except (ValueError, TypeError):
            return None

    def add(kind, when, label, sub="", tab="", state=""):
        items.append({"kind": kind, "date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
                      "label": label[:90], "sub": sub[:120], "tab": tab, "state": state})

    def note(t, line):
        d = t.strftime("%Y-%m-%d")
        if d in recap:
            recap[d]["log"].append({"time": t.strftime("%H:%M"), "text": line[:140]})
        return recap.get(d)

    with closing(db()) as c:
        # posts: real scheduled events, shown one by one
        for r in c.execute("SELECT when_at, state, text, networks, kind FROM social_queue WHERE when_at >= ? AND when_at < ? "
                           "AND state IN ('scheduled','publishing','published','failed')", (s_utc, e_utc)):
            t = local(r["when_at"])
            if not t:
                continue
            nets = ", ".join(json.loads(r["networks"] or "[]")) or "post"
            add("post", t, (r["text"] or "").split("\n")[0] or "Post", nets + (" · " + r["kind"] if r["kind"] else ""), "social", r["state"])
            rc = recap.get(t.strftime("%Y-%m-%d"))
            if rc and r["state"] == "published":
                rc["posts"] += 1
            elif rc and r["state"] == "failed":
                rc["failed_posts"] += 1

        # emails sent: counted, not drawn
        for r in c.execute("SELECT o.sent_at, o.step, p.company FROM outreach o JOIN prospects p ON p.id=o.prospect_id "
                           "WHERE o.state='sent' AND o.sent_at >= ? AND o.sent_at < ?", (s_utc, e_utc)):
            t = local(r["sent_at"])
            rc = t and note(t, "Emailed %s%s" % (r["company"] or "a lead", "" if (r["step"] or 1) <= 1 else " (follow-up %d)" % r["step"]))
            if rc:
                rc["sent"] += 1
                if (r["step"] or 1) > 1:
                    rc["followups"] += 1

        # follow-ups you set by hand: real events, shown one by one
        for r in c.execute("SELECT id, company, next_due, follow_time, follow_note FROM prospects "
                           "WHERE next_action='follow_up' AND next_due IS NOT NULL "
                           "AND date(next_due) >= ? AND date(next_due) < ? "
                           "AND status NOT IN ('dead','corporate')",
                           (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))):
            try:
                dd = datetime.strptime(str(r["next_due"])[:10], "%Y-%m-%d")
            except ValueError:
                continue
            hh, mm = 9, 0
            ft = r["follow_time"] or ""
            if re.match(r"^\d{2}:\d{2}$", ft):
                hh, mm = int(ft[:2]), int(ft[3:])
            add("followup", datetime(dd.year, dd.month, dd.day, hh, mm, tzinfo=tz),
                r["company"] or "Follow up", r["follow_note"] or "Follow up", "today", "")
            items[-1]["pid"] = r["id"]

        # what's due: one card per day
        for r in c.execute("SELECT next_due, next_action, company FROM prospects WHERE next_due IS NOT NULL "
                           "AND date(next_due) >= ? AND date(next_due) < ? AND status NOT IN ('booked','dead','corporate') "
                           "AND COALESCE(next_action,'') <> 'follow_up'",
                           (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))):
            d = str(r["next_due"])[:10]
            if d not in due:
                continue
            act = r["next_action"] or "call"
            label = {"email_followup": "follow-up emails", "call": "calls", "email": "emails",
                     "call_back": "call-backs"}.get(act, act.replace("_", " "))
            due[d]["total"] += 1
            due[d]["by"][label] = due[d]["by"].get(label, 0) + 1
            if len(due[d]["names"]) < 40:
                due[d]["names"].append(r["company"] or "")

        # conversations and everything else a person or the app did
        for r in c.execute("SELECT t.kind, t.outcome, t.note, t.at, p.company FROM touches t JOIN prospects p ON p.id=t.prospect_id "
                           "WHERE t.at >= ? AND t.at < ?", (s_utc, e_utc)):
            t = local(r["at"])
            if not t:
                continue
            k, o, who = r["kind"] or "", r["outcome"] or "", r["company"] or "a lead"
            rc = recap.get(t.strftime("%Y-%m-%d"))
            if not rc:
                continue
            if k == "email" and o == "reply":
                rc["replies"] += 1
                note(t, "%s wrote back" % who)
                add("reply", t, who, (r["note"] or "wrote back")[:120], "today", "reply")
            elif k == "email" and o == "reply:bounce":
                rc["bounces"] += 1
            elif k == "email" and o == "reply:unsubscribe":
                rc["unsubs"] += 1
                note(t, "%s unsubscribed" % who)
            elif k in ("call", "ai_receptionist"):
                rc["calls"] += 1
                inbound = k == "ai_receptionist" or o.startswith("inbound") or o in ("missed", "reply")
                note(t, "%s %s%s" % ("Call from" if inbound else "Called", who, (" - " + o.replace("_", " ")) if o else ""))
                if inbound or o in ("connected", "booked", "interested", "talked"):
                    add("call", t, who, (o.replace("_", " ") + " · " + (r["note"] or "")).strip(" ·"), "today", o)
            elif k == "sms":
                rc["texts"] += 1
                inbound = o.startswith("inbound") or o in ("received", "reply")
                note(t, "%s %s" % ("Text from" if inbound else "Texted", who))
                if inbound:
                    add("text", t, who, (r["note"] or "")[:120], "today", o)
            elif k == "stage" and o:
                rc["moves"][o] = rc["moves"].get(o, 0) + 1
                if o in ("warm", "review", "proposal", "customer"):
                    note(t, "%s moved to %s" % (who, {"warm": "Replied", "review": "Review booked",
                                                      "proposal": "Proposal out", "customer": "Customer"}[o]))

        for r in c.execute("SELECT added_at FROM prospects WHERE added_at >= ? AND added_at < ?", (s_utc, e_utc)):
            t = local(r["added_at"])
            rc = t and recap.get(t.strftime("%Y-%m-%d"))
            if rc:
                rc["new_leads"] += 1

    items.sort(key=lambda x: (x["date"], x["time"] or "00:00"))
    recaps = {}
    for d in days:
        if d > today_s:
            continue
        rc = recap[d]
        rc["log"].sort(key=lambda x: x["time"])
        rc["so_far"] = d == today_s and now_local.hour < 17
        rc["empty"] = not any(rc[k] for k in ("sent", "replies", "calls", "texts", "posts", "new_leads", "unsubs", "bounces", "failed_posts")) and not rc["moves"]
        rc["log"] = rc["log"][-200:]
        recaps[d] = rc
    return {"week": socialweek.week_of(wk), "days": days, "today": today_s, "items": items,
            "due": {d: v for d, v in due.items() if v["total"]}, "recaps": recaps,
            "prev": socialweek.week_of(wk - timedelta(days=7)), "next": socialweek.week_of(wk + timedelta(days=7)),
            "tz": now_local.tzname() or ""}


@app.get("/api/getting_started")
def getting_started(request: Request):
    """The six steps a new business walks through, for the card at the top
    of Today. Built on setup_status (the Setup page's own checklist) plus
    the two steps that live outside Setup: leads found, autopilot on. Gone
    from Today once every step is done. Phase 1 of the commercial plan
    (2026-09-26)."""
    require_auth(request)
    st = setup_status(request)
    by = {x["key"]: x for x in st["sections"]}
    with closing(db()) as c:
        leads = c.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
        sent = c.execute("SELECT COUNT(*) FROM outreach WHERE state='sent'").fetchone()[0]
    def sec(key, label, go, sub):
        x = by.get(key, {"state": "todo", "missing": ""})
        return {"key": key, "label": label, "done": x["state"] == "done", "partial": x["state"] == "partial",
                "note": x.get("missing", "") or sub, "go": go}
    steps = [
        sec("business", "Your business", {"tab": "setup", "sec": "business"}, "Name, phone, email and mailing address - the sign-off on every email."),
        sec("offers", "What you sell", {"tab": "setup", "sec": "offers"}, "The offers every email is built from."),
        sec("mailbox", "Your mailbox", {"tab": "setup", "sec": "mailbox"}, "So it can send for you and see who replied."),
        sec("maps", "Lead finder", {"tab": "setup", "sec": "maps"}, "A Google Maps key so it can find businesses near you."),
        {"key": "leads", "label": "First leads found", "done": leads > 0, "partial": False,
         "note": "%d lead%s on file." % (leads, "" if leads == 1 else "s") if leads else "Press Find on the Prospects tab - pick a trade and a city.",
         "go": {"tab": "prospects"}},
        {"key": "autopilot", "label": "Sending on its own", "done": bool(autopilot_mode()) or sent > 0, "partial": False,
         "note": ("Autopilot is on." if autopilot_mode() else "%d sent by hand so far." % sent) if (autopilot_mode() or sent) else
                 "Approve the first emails in the Outbox, or switch autopilot on there.",
         "go": {"tab": "outbox"}},
    ]
    done = sum(1 for x in steps if x["done"])
    return {"steps": steps, "done": done, "total": len(steps), "complete": done == len(steps),
            "business": ws.info().get("name") or ws.me().get("name") or ""}


@app.get("/api/workspaces")
def list_workspaces(request: Request):
    require_auth(request)
    return {"workspaces": ws.public(), "current": ws.current()}


# ── snapshots: a vertical, packaged ──────────────────────────────────────
# The tenth clinic should be a weekend, not a build. That only happens if the
# second one starts from the first one's configuration instead of from blank -
# which is the argument for charging an implementation fee at all. You are
# loading a proven template; if every customer is bespoke then "license" is
# custom development wearing a nicer word, and the margin never shows up.
#
# See snapshots.py for what travels and, more importantly, for why the list is
# an allow-list. Everything about a workspace that identifies it - sender,
# address, phone, keys, host, its actual leads - is absent by construction
# rather than removed by a filter.

def builtin_catalog() -> dict:
    """Watson's and HomeRepair's code copy, in catalog shape.

    Their offers live in Python because they were argued over line by line,
    which is fine until you want to hand that copy to a customer workspace.
    This is read-only - it materialises the dicts into the same shape the
    registry workspaces store, so the first snapshot can be cut from the
    business that actually has proven copy rather than from an empty one.
    """
    ev = {offer: rx for rx, offer in EVIDENCE}
    offers = {}
    for name, (tail, cost, fix) in OFFER_COPY.items():
        entry = {"tail": tail, "cost": cost, "fix": fix}
        if name in ev:
            entry["evidence"] = ev[name]
        if name in QUESTION_BY_OFFER:
            entry["question"] = QUESTION_BY_OFFER[name]
        if name in IMPACT_BY_OFFER:
            entry["impact"] = list(IMPACT_BY_OFFER[name])
        offers[name] = entry
    return {"offers": offers}


def workspace_catalog() -> dict:
    """Whatever this workspace actually sells from: what was saved in Setup,
    else the built-in copy for the two workspaces that have one."""
    saved = catalog()
    if saved.get("offers"):
        return saved
    return builtin_catalog() if ws.current() in CATALOG_WORKSPACES else {}


@app.get("/api/snapshot")
def export_snapshot(request: Request, name: str = "", vertical: str = "",
                    note: str = ""):
    """Package this workspace's configuration as a file."""
    require_auth(request)
    cat = workspace_catalog()
    if not (cat.get("offers")):
        raise HTTPException(409,
            "%s has no offers yet, so there is nothing to snapshot. Run the "
            "intake or write the catalog first." % ws.info()["name"])
    w = ws.info()
    vert = (vertical or "").strip().lower()
    windows = dict(_cadence(snap.SETTING_WINDOWS, {}))
    if vert and vert not in windows and vert in CALL_WINDOWS:
        # Snapshotting "for chiro" from a workspace that never overrode the
        # windows should still carry the chiro windows - otherwise the file
        # silently omits the one piece of timing the vertical is named for.
        windows[vert] = [list(x) for x in CALL_WINDOWS[vert]]
    body = snap.build(
        name=name or ("%s configuration" % w["short"]),
        catalog=cat,
        windows=windows,
        gaps=followup_gaps(),
        max_step=max_email_step(),
        reawaken=reawaken_days(),
        vertical=vert,
        note=note,
        source_workspace=w["slug"])
    return {"ok": True, "snapshot": body,
            "offers": sorted(body["catalog"]["offers"]),
            "warnings": snap.suspect_secrets(body),
            "excluded": "Sender identity, mailing address, phone numbers, API "
                        "keys, hostname, sending cap and every lead, contact "
                        "and email are not in this file."}


class SnapshotBody(BaseModel):
    snapshot: dict
    overwrite: bool = False


@app.post("/api/snapshot")
def import_snapshot(request: Request, body: SnapshotBody):
    """Load a snapshot into this workspace.

    Refuses to overwrite an existing catalog unless asked twice, because the
    thing being replaced is the sentence that goes in a stranger's inbox and
    there is no undo on a send.
    """
    require_auth(request)
    if ws.current() in CATALOG_WORKSPACES:
        raise HTTPException(400,
            "%s's offer copy lives in code, so importing here would do nothing "
            "visible - the catalog it would write is never read. Create a "
            "customer workspace and import into that."
            % ws.info()["name"])

    loaded = snap.parse(body.snapshot, parse_catalog)
    if not loaded:
        raise HTTPException(400,
            "That is not a readable Just Grit snapshot, or every offer in it "
            "was incomplete. Each offer needs a name plus tail, cost and fix.")

    if has_catalog() and not body.overwrite:
        raise HTTPException(409,
            "%s already has offers. Re-send with overwrite to replace them - "
            "this does not merge, it replaces." % ws.info()["short"])

    set_setting(CATALOG_SETTING, json.dumps(loaded["catalog"]))
    wrote = ["offer catalog"]
    if loaded[snap.SETTING_WINDOWS]:
        set_setting(snap.SETTING_WINDOWS, json.dumps(loaded[snap.SETTING_WINDOWS]))
        wrote.append("call windows")
    if loaded[snap.SETTING_GAPS]:
        set_setting(snap.SETTING_GAPS, json.dumps(loaded[snap.SETTING_GAPS]))
        wrote.append("follow-up gaps")
    set_setting(snap.SETTING_MAX_STEP, loaded[snap.SETTING_MAX_STEP])
    set_setting(snap.SETTING_REAWAKEN, loaded[snap.SETTING_REAWAKEN])
    _CADENCE_CACHE.clear()

    return {"ok": True, "loaded": wrote,
            "offers": sorted(loaded["catalog"]["offers"]),
            "from": loaded.get("source_workspace") or "unknown",
            "next": "Read every offer before drafting. A snapshot carries the "
                    "shape of a pitch, not the facts about this business - the "
                    "sentences still name what THIS customer does."}


class ReawakenBody(BaseModel):
    days: int = 0
    apply: bool = False


@app.post("/api/queue/reawaken")
def reawaken(request: Request, body: ReawakenBody):
    """Bring back leads that finished the ladder and have been cold since.

    Dry by default. The preview is the point: this is the only mechanism in
    the system that puts people back on a list they already fell off, and
    seeing the names before it happens is how you catch that it is about to
    re-approach somebody you actually spoke to.
    """
    require_auth(request)
    days = snap.clean_reawaken(body.days, reawaken_days()) if body.days else reawaken_days()
    with closing(db()) as c:
        if not body.apply:
            rows = c.execute("SELECT id, company, status, touched_at, "
                             "COALESCE(reawakened_at,'') AS reawakened_at "
                             "FROM prospects WHERE status='cold'").fetchall()
            would = [{"id": r["id"], "company": r["company"],
                      "cold_since": (r["touched_at"] or "")[:10]}
                     for r in rows
                     if trig.due_for_reawaken(r["status"], r["touched_at"],
                                              r["reawakened_at"], days)]
            already = sum(1 for r in rows if (r["reawakened_at"] or "").strip())
            return {"ok": True, "applied": False, "days": days,
                    "cold_total": len(rows), "would_wake": len(would),
                    "already_had_their_second_chance": already,
                    "prospects": would[:60]}
        woken = run_reawaken(c, days)
        c.commit()
    return {"ok": True, "applied": True, "days": days,
            "woke": len(woken), "prospects": woken[:60],
            "note": "They come back as calls, due today. Somebody who ignored "
                    "the whole email ladder has already answered that question."}


@app.get("/api/queue")
def queue(request: Request):
    """What you owe a touch on, today. Overdue first — those decay fastest."""
    require_auth(request)
    try:                                   # one-time cleanup of mis-filed replies
        if setting("replies_rechecked", "") != "v1":
            with closing(db()) as c0:
                recheck_replies(c0)
                c0.commit()
            set_setting("replies_rechecked", "v1")
    except Exception:
        pass
    with closing(db()) as c:
        fsql, fargs = focus_sql("prospects")
        rows = [row_to_dict(r) for r in c.execute("""
            SELECT * FROM prospects
            WHERE next_due IS NOT NULL
              AND status NOT IN ('booked','dead','cold','corporate')
              AND date(next_due) <= date('now','+7 days')
              -- a reply (call_back) always shows, whatever trade you're working
              AND (next_action='call_back' OR (1=1 """ + fsql + """))
            ORDER BY CASE WHEN next_action='call_back' THEN 0 ELSE 1 END,
                     date(next_due) ASC,
                     COALESCE(tier, 1) DESC,
                     COALESCE(lead_score, 0) DESC,
                     CASE WHEN say <> '' THEN 0 ELSE 1 END,
                     COALESCE(score, 999) ASC
        """, fargs).fetchall()]
        stats = dict(c.execute("""
            SELECT
              (SELECT COUNT(*) FROM prospects WHERE next_due IS NOT NULL
                 AND status NOT IN ('booked','dead','cold','corporate')
                 AND date(next_due) < date('now'))  AS overdue,
              (SELECT COUNT(*) FROM prospects WHERE date(next_due) = date('now')
                 AND status NOT IN ('booked','dead','cold','corporate')) AS today,
              (SELECT COUNT(*) FROM prospects WHERE status='booked') AS booked,
              (SELECT COUNT(*) FROM touches WHERE date(at) = date('now')) AS touches_today
        """).fetchone())

    # The card is what you look at with the phone already ringing, so it gets
    # the people and the last thing that happened - not just a name and a line.
    ids = [r["id"] for r in rows]
    people, last, drafts, replies = {}, {}, {}, {}
    if ids:
        marks = ",".join("?" * len(ids))
        with closing(db()) as c:
            for row in c.execute(
                    f"SELECT * FROM contacts WHERE prospect_id IN ({marks}) "
                    "ORDER BY is_primary DESC, id", ids):
                people.setdefault(row["prospect_id"], []).append(dict(row))
            # One row per prospect: the most recent touch, whatever it was.
            for row in c.execute(
                    f"""SELECT t.prospect_id, t.kind, t.outcome, t.note, t.at
                        FROM touches t
                        JOIN (SELECT prospect_id, MAX(id) AS mid FROM touches
                              WHERE prospect_id IN ({marks})
                                AND kind NOT IN ('note','stage','deal')
                              GROUP BY prospect_id) m
                          ON m.mid = t.id""", ids):
                last[row["prospect_id"]] = dict(row)
            # What they actually wrote, so nobody digs through the inbox.
            for row in c.execute(
                    f"""SELECT prospect_id, id, reply_from, reply_text, reply_detected_at, reply
                        FROM outreach WHERE prospect_id IN ({marks})
                          AND coalesce(reply_text,'') <> '' ORDER BY reply_detected_at""", ids):
                replies[row["prospect_id"]] = {
                    "outreach_id": row["id"], "from": row["reply_from"],
                    "text": (row["reply_text"] or "")[:900],
                    "at": row["reply_detected_at"], "kind": row["reply"] or ""}
            # What the Outbox is holding for them, so an EMAIL card can say
            # "the draft is waiting in the Outbox" instead of asking for an
            # email the Outbox is about to send.
            for row in c.execute(
                    f"""SELECT prospect_id, id, state, step FROM outreach
                        WHERE prospect_id IN ({marks}) AND state IN ('draft','approved')""", ids):
                drafts[row["prospect_id"]] = {"id": row["id"], "state": row["state"],
                                              "step": row["step"]}

    today_s = datetime.now(timezone.utc).date().isoformat()
    for r in rows:
        due = (r.get("next_due") or "")[:10]
        r["when"] = "overdue" if due < today_s else "today" if due == today_s else "later"
        r["why"] = step_note(int(r.get("step") or 1))
        r["contacts"] = people.get(r["id"], [])
        r["last_touch"] = last.get(r["id"])
        r["reply_in"] = replies.get(r["id"])
        r["outbox_draft"] = drafts.get(r["id"])
        r["autopilot"] = autopilot_mode()
        r["call_window"] = window_label(r.get("vertical"))
        r["callable_now"] = callable_now(r.get("vertical"))

    # Ring the ones you can actually reach first. Nothing is hidden - a queue
    # that empties itself at 12:30 looks broken, and the rest are still there
    # to work through or to knock on instead.
    rows.sort(key=lambda r: (0 if (r.get("next_action") != "call" or r["callable_now"]) else 1))
    stats["callable_now"] = sum(1 for r in rows
                                if r.get("next_action") == "call" and r["callable_now"])
    return {"queue": rows, "stats": stats, "focus": focus_trades()}


class TouchBody(BaseModel):
    outcome: str                       # no_answer | left_message | emailed | spoke | booked | dead | sent | corporate
    note: str = ""
    days: Optional[int] = None         # explicit callback, overrides the sequence


@app.post("/api/prospect/{pid}/touch")
def log_touch(request: Request, pid: int, body: TouchBody):
    require_auth(request)
    valid = {"no_answer", "left_message", "emailed", "spoke", "booked", "dead", "sent",
             "corporate"}
    if body.outcome not in valid:
        raise HTTPException(400, f"outcome must be one of {sorted(valid)}")

    with closing(db()) as c:
        row = c.execute("SELECT next_action, domain FROM prospects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        kind = row["next_action"] or "call"
        if body.outcome in ("emailed", "sent"):
            kind = "email"
        c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at) VALUES (?,?,?,?,?)",
                  (pid, kind, body.outcome, body.note, now()))
        if body.outcome in ("emailed", "sent"):
            # "Emailed" on Today used to leave the Outbox draft waiting, so
            # the same business got the Outbox's email too - and the cap
            # never counted the one sent by hand.
            mark_waiting_draft_sent(c, pid)
        summary = advance_queue(c, pid, body.outcome, body.days)
        c.commit()
        dom = row["domain"]

    # "Not interested" is a promise — honor it in the shared suppression list.
    if body.outcome in ("dead", "corporate") and dom:
        try:
            suppress(dom)
        except Exception:
            pass
    return {"ok": True, "summary": summary}


@app.post("/api/prospect/{pid}/call")
def telnyx_call(request: Request, pid: int):
    """Dial the rep's own phone; once they pick up, the webhook below bridges
    the prospect in. This never dials the prospect directly - see
    telnyx_calls.py's docstring for why that split matters."""
    require_auth(request)
    if not telnyx_settings_ready():
        raise HTTPException(400, "Add your Telnyx API key, connection ID, Telnyx number, "
                                  "and your own phone number in Setup first.")
    with closing(db()) as c:
        row = c.execute("SELECT company FROM prospects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        raw_phone = _best_phone(c, pid)
    prospect_e164 = telnyx.to_e164(raw_phone)
    if not prospect_e164:
        raise HTTPException(400, f"No usable phone number on file for {row['company'] or 'this lead'}.")
    try:
        call_control_id = telnyx.place_bridge_call(
            api_key=setting("telnyx_api_key", ""),
            connection_id=setting("telnyx_connection_id", ""),
            from_number=setting("telnyx_from_number", ""),
            rep_number=setting("telnyx_rep_number", ""),
            webhook_url=telnyx_webhook_url(),
            pid=pid,
            prospect_number=prospect_e164,
        )
    except telnyx.TelnyxError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "call_control_id": call_control_id,
            "message": "Calling your phone now - answer it to connect."}


# ─────────────────── missed-call text-back ───────────────────
# See textback.py for what this is and the line it will not cross. The
# webhook below is the only thing that writes inbound_calls, and
# tb.reply_to_missed_call is the only thing that sends.

def textback_enabled() -> bool:
    return setting("textback_enabled", "").strip() == "1"


def textback_status() -> dict:
    ready = bool(telnyx_settings_ready())
    return {"enabled": textback_enabled(), "ready": ready,
            "message": setting("textback_message", "") or tb.DEFAULT_MESSAGE,
            "forward_to": setting("textback_forward_to", ""),
            "cooldown_hours": tb.clean_cooldown(setting("textback_cooldown_hours", "")),
            "has_messaging_profile": bool(setting("telnyx_messaging_profile_id", "").strip()),
            "sms_webhook_url": telnyx_sms_webhook_url()}


def telnyx_sms_webhook_url() -> str:
    base = public_base()
    return "%s/webhooks/telnyx/sms/%s" % (base, telnyx_webhook_token()) if base else ""


def _optout_exists(c, number: str) -> bool:
    n = telnyx.to_e164(number)
    return bool(n and c.execute("SELECT 1 FROM sms_optouts WHERE number=?", (n,)).fetchone())


def _last_texted(c, number: str):
    n = telnyx.to_e164(number)
    r = c.execute("SELECT MAX(at) FROM sms_log WHERE direction='out' AND number=?",
                  (n,)).fetchone() if n else None
    return r[0] if r and r[0] else None


def _send_call_alert(to_addr: str, subject: str, body: str) -> None:
    try:
        _send_platform_email(to_addr, subject, body)
    except Exception as e:              # a failed alert must never touch the call
        print("call alert email failed: %s" % str(e)[:200])


def _call_alert(number: str, answered: bool, *, texted=None, why: str = "",
                ai_note: str = "") -> None:
    """Email the owner that somebody rang this workspace's number.

    Until now a call was recorded, texted back and put on Today - and nothing
    told Daniel it had happened. Goes out through the same mailbox the login
    codes use, to `call_alert_email` if set, else the account's sender
    address. `call_alert_off` = 1 silences it. Never raises."""
    try:
        if setting("call_alert_off", "") == "1":
            return
        to = (setting("call_alert_email", "") or _primary_setting("sender_email", "")).strip()
        if not to:
            return
        n = telnyx.to_e164(number) or (number or "")
        d = re.sub(r"\D", "", n)[-10:]
        shown = "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if len(d) == 10 else (n or "unknown number")
        who = ""
        with closing(db()) as c:
            pid = find_prospect_by_phone(c, n) if n else None
            if pid:
                r = c.execute("SELECT company FROM prospects WHERE id=?", (pid,)).fetchone()
                who = ((r["company"] if r else "") or "").strip()
        info = ws.info()
        biz = info.get("short") or info.get("name") or "Just Grit"
        when = datetime.now(workspace_tz()).strftime("%a %I:%M %p").replace(" 0", " ")
        result = "Answered by the AI receptionist" if ai_note else ("Answered" if answered else "Missed")
        lines = ["Someone called the %s number." % biz, "",
                 "Caller:  %s%s" % (shown, ("  (%s)" % who if who and not who.startswith("Inbound caller") else "")),
                 "When:    %s" % when,
                 "Result:  %s" % result]
        if ai_note:
            lines += ["", "What they said:", ai_note]
        if texted is True:
            lines += ["", "We texted them back automatically."]
        elif texted is False and why:
            lines += ["", "No text-back sent (%s)." % why]
        if not answered and not ai_note:
            lines += ["", "They are on today's list to call back."]
        link = public_base()
        if link:
            lines += ["", "Open Just Grit: %s" % link]
        subject = "%s call - %s - %s" % (result.split(" by ")[0], biz, shown)
        POOL.submit(ws.carry(_send_call_alert, to, subject, "\n".join(lines)))
    except Exception as e:
        print("call alert skipped: %s" % str(e)[:200])


def handle_inbound_call_event(event_type: str, call: dict) -> dict:
    """One inbound-call webhook event. Returns what it did, for the tests.

    Keyed on call_session_id, which Telnyx shares across every leg of one
    call - the caller's leg and, if we forward, the business's leg. So
    `call.answered` on EITHER leg marks the session answered, and only a
    hangup on the CALLER'S leg (the one recorded at call.initiated) can
    trigger a text. A forwarded leg timing out is not a missed call yet;
    the caller is still ringing, and we hang them up so their own hangup
    arrives and decides it.
    """
    sid = call.get("call_session_id") or ""
    ccid = call.get("call_control_id") or ""
    if not sid:
        return {"did": "ignored", "why": "no session id"}
    frm = call.get("from") or ""
    to = call.get("to") or ""
    with closing(db()) as c:
        if event_type == "call.initiated" and (call.get("direction") or "") == "incoming":
            c.execute("INSERT INTO inbound_calls (call_session_id, call_control_id, from_number, "
                      "to_number, direction, started_at) VALUES (?,?,?,?,'incoming',?) "
                      "ON CONFLICT(call_session_id) DO NOTHING",
                      (sid, ccid, frm, to, now()))
            c.commit()
            fwd = setting("textback_forward_to", "").strip()
            if textback_enabled() and fwd and telnyx.to_e164(fwd):
                try:
                    telnyx._post("/calls/%s/actions/transfer" % ccid,
                                 setting("telnyx_api_key", ""),
                                 {"to": telnyx.to_e164(fwd),
                                  "from": telnyx.to_e164(setting("telnyx_from_number", "")),
                                  "timeout_secs": 25,
                                  "client_state": telnyx.encode_state(v=1, leg="forward", sid=sid)})
                    c.execute("UPDATE inbound_calls SET forwarded_to=? WHERE call_session_id=?",
                              (fwd, sid)); c.commit()
                    return {"did": "forwarded", "to": fwd}
                except telnyx.TelnyxError as e:
                    return {"did": "forward_failed", "why": str(e)[:120]}
            return {"did": "recorded"}

        row = c.execute("SELECT * FROM inbound_calls WHERE call_session_id=?", (sid,)).fetchone()
        if not row:
            return {"did": "ignored", "why": "not an inbound session we recorded"}

        if event_type == "call.answered":
            c.execute("UPDATE inbound_calls SET answered_at=COALESCE(answered_at, ?) "
                      "WHERE call_session_id=?", (now(), sid)); c.commit()
            return {"did": "answered"}

        if event_type == "call.hangup":
            cause = (call.get("hangup_cause") or "")[:40]
            # Telnyx retries webhooks. `hung_up_at` is set with COALESCE
            # below, so its absence RIGHT NOW is the only honest signal that
            # this is the first time we have seen this call end - and the
            # only safe gate on doing anything once, lead included.
            first_hangup = not row["hung_up_at"]
            if ccid and ccid != (row["call_control_id"] or ""):
                # The forwarded leg ended. If nobody answered, drop the
                # caller too so their hangup arrives and the text follows.
                if not row["answered_at"]:
                    telnyx.hangup(api_key=setting("telnyx_api_key", ""),
                                  call_control_id=row["call_control_id"])
                return {"did": "forward_leg_hung_up", "cause": cause}
            c.execute("UPDATE inbound_calls SET hung_up_at=COALESCE(hung_up_at, ?), hangup_cause=? "
                      "WHERE call_session_id=?", (now(), cause, sid)); c.commit()
            if not textback_enabled():
                # Text-back off is not a reason to lose the lead. Somebody
                # still rang this business and nobody picked up.
                pid = None
                if first_hangup and not row["answered_at"]:
                    pid = inbound_lead(c, row["from_number"], kind="call", outcome="missed",
                                       note="Missed call", callback=True)
                    c.commit()
                if first_hangup:
                    _call_alert(row["from_number"], bool(row["answered_at"]),
                                texted=False, why="text-back is off")
                return {"did": "hung_up", "texted": False, "why": "text-back is off",
                        "prospect_id": pid}
            row = c.execute("SELECT * FROM inbound_calls WHERE call_session_id=?", (sid,)).fetchone()
            res = tb.reply_to_missed_call(
                dict(row),
                api_key=setting("telnyx_api_key", ""),
                from_number=setting("telnyx_from_number", ""),
                template=setting("textback_message", ""),
                business=ws.info()["short"],
                messaging_profile_id=setting("telnyx_messaging_profile_id", ""),
                last_texted_at=_last_texted(c, row["from_number"]),
                opted_out=_optout_exists(c, row["from_number"]),
                cooldown_hours=tb.clean_cooldown(setting("textback_cooldown_hours", "")),
                business_numbers=(setting("telnyx_from_number", ""),
                                  setting("textback_forward_to", ""),
                                  setting("telnyx_rep_number", "")))
            if res["sent"]:
                c.execute("UPDATE inbound_calls SET texted_at=?, text_status='sent', message_id=?, "
                          "text_body=? WHERE call_session_id=?",
                          (now(), res["message_id"], res["text"], sid))
                c.execute("INSERT INTO sms_log (direction, number, text, call_session_id, message_id, at) "
                          "VALUES ('out',?,?,?,?,?)",
                          (telnyx.to_e164(row["from_number"]), res["text"], sid, res["message_id"], now()))
            elif not row["texted_at"]:
                # Telnyx retries webhooks. A replayed hangup for a call that
                # was already texted must not turn its "sent" into a skip.
                c.execute("UPDATE inbound_calls SET text_status=? WHERE call_session_id=?",
                          ("skipped: " + res["reason"], sid))
            # A missed call IS the lead. It used to land in `inbound_calls`
            # and nowhere else - no name, no queue, nothing to work - which
            # made the one number this product exists to protect the only
            # number whose callers were invisible. Telnyx retries, so this
            # only fires on the hangup that actually recorded the call.
            pid = None
            if first_hangup and not row["answered_at"]:
                why = ("Missed call - texted back" if res["sent"]
                       else "Missed call - no text (%s)" % res["reason"])
                pid = inbound_lead(c, row["from_number"], kind="call",
                                   outcome="missed", note=why, callback=True)
            c.commit()
            if first_hangup:
                _call_alert(row["from_number"], bool(row["answered_at"]),
                            texted=bool(res["sent"]), why=("" if res["sent"] else res["reason"]))
            return {"did": "hung_up", "texted": res["sent"], "why": res["reason"],
                    "prospect_id": pid}
    return {"did": "ignored", "why": event_type}


@app.post("/webhooks/telnyx/sms/{token}", include_in_schema=False)
async def telnyx_sms_webhook(token: str, request: Request):
    """Inbound SMS on the Telnyx number. STOP is honoured here forever; any
    other reply is logged so it shows on Today and nobody has to check the
    Telnyx portal to find out a customer wrote back."""
    if token != telnyx_webhook_token():
        raise HTTPException(404, "not found")
    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}
    event = (payload.get("data") or {})
    if event.get("event_type") != "message.received":
        return {"ok": True}
    msg = event.get("payload") or {}
    frm = ((msg.get("from") or {}).get("phone_number") or "")
    text = (msg.get("text") or "")[:1000]
    n = telnyx.to_e164(frm)
    if not n:
        return {"ok": True}
    with closing(db()) as c:
        c.execute("INSERT INTO sms_log (direction, number, text, message_id, at) VALUES ('in',?,?,?,?)",
                  (n, text, msg.get("id") or "", now()))
        if tb.is_stop(text):
            c.execute("INSERT INTO sms_optouts (number, at, source) VALUES (?,?,'sms:stop') "
                      "ON CONFLICT(number) DO NOTHING", (n, now()))
            # Recorded against the lead so nobody texts them again by hand,
            # but never escalated - STOP means leave me alone.
            pid = inbound_lead(c, n, kind="sms", outcome="opted_out",
                               note="Texted STOP - do not text again")
            c.commit()
            return {"ok": True, "prospect_id": pid, "stop": True}
        # A human wrote back to the text-back. Nothing in this system is
        # warmer, and until now it stopped at a row in sms_log.
        pid = inbound_lead(c, n, kind="sms", outcome="reply",
                           note="Texted back: " + (text[:300] or "(no text)"), hot=True)
        srt = sort_inbound(c, pid, text, channel="sms")
        c.commit()
    _call_alert(phone, True, ai_note=("%s%s" % (name + ": " if name else "", note))[:600])
    return {"ok": True, "prospect_id": pid, "bucket": srt.get("bucket", "")}


@app.get("/api/textback/recent")
def textback_recent(request: Request, days: int = 30):
    """What the Today panel and the monthly report read."""
    require_auth(request)
    days = max(1, min(int(days or 30), 365))
    with closing(db()) as c:
        calls = [dict(r) for r in c.execute(
            "SELECT * FROM inbound_calls WHERE started_at >= datetime('now', ?) "
            "ORDER BY started_at DESC LIMIT 100", ("-%d days" % days,))]
        # A STOP is a reply in the SMS sense and not in any sense that
        # belongs on a report a customer pays for.
        replies = {r[0]: r[1] for r in c.execute(
            "SELECT number, MAX(at) FROM sms_log WHERE direction='in' "
            "AND at >= datetime('now', ?) "
            "AND number NOT IN (SELECT number FROM sms_optouts) GROUP BY number",
            ("-%d days" % days,))}
        optouts = c.execute("SELECT COUNT(*) FROM sms_optouts").fetchone()[0]
    for x in calls:
        x["replied_at"] = replies.get(telnyx.to_e164(x.get("from_number") or ""))
    missed = [x for x in calls if x.get("hung_up_at") and not x.get("answered_at")]
    texted = [x for x in missed if x.get("texted_at")]
    replied = [x for x in texted if x.get("replied_at") and x["replied_at"] >= x["texted_at"]]
    return {"days": days, "calls": len(calls), "answered": len(calls) - len(missed),
            "missed": len(missed), "texted": len(texted), "replied": len(replied),
            "optouts": optouts, "enabled": textback_enabled(),
            "recent": calls[:40]}


@app.post("/webhooks/telnyx/voice/{token}", include_in_schema=False)
async def telnyx_voice_webhook(token: str, request: Request):
    """Telnyx calling us back. No Cloudflare Access identity will ever be on
    this request - it's machine-to-machine - so the long random token in the
    path is what stands in for auth here (see telnyx_webhook_token). A
    Cloudflare Access bypass policy for /webhooks/* is also required on both
    hostnames, or Access's own login page eats this request before it ever
    reaches this function."""
    if token != telnyx_webhook_token():
        raise HTTPException(404, "not found")
    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}   # malformed body isn't ours to fix; just don't 500

    event = (payload.get("data") or {})
    event_type = event.get("event_type", "")
    call = event.get("payload") or {}
    state = telnyx.decode_state(call.get("client_state"))

    # An inbound call to the workspace's number - or a leg we forwarded from
    # one - is the text-back trigger's business. The rep-first outbound flow
    # below never has direction=incoming and never carries leg=forward.
    if (call.get("direction") or "") == "incoming" or state.get("leg") == "forward":
        try:
            handle_inbound_call_event(event_type, call)
        except Exception:
            pass          # a webhook must never 500 back at Telnyx
        return {"ok": True}

    if event_type == "call.answered" and state.get("leg") == "rep":
        call_control_id = call.get("call_control_id")
        prospect_number = state.get("prospect")
        pid = state.get("pid")
        if call_control_id and prospect_number:
            try:
                telnyx.transfer_to_prospect(
                    api_key=setting("telnyx_api_key", ""),
                    call_control_id=call_control_id,
                    from_number=setting("telnyx_from_number", ""),
                    prospect_number=prospect_number,
                    pid=pid,
                )
            except telnyx.TelnyxError:
                pass   # nothing left to do but let the rep's call sit there
    return {"ok": True}


class AssistantCallLog(BaseModel):
    """What the AI Assistant's Webhook tool sends. Deliberately loose (all
    optional except the phone) - it's an LLM filling this in mid-call, not a
    form a person double-checked, so this endpoint bends over backwards not
    to 400 on a call that's still worth logging."""
    caller_phone: str = ""
    caller_name: str = ""
    reason: str = ""
    urgency: str = "normal"       # "hot" -> flagged in the touch note
    notes: str = ""
    requested_time: str = ""      # a time the caller asked for, unconfirmed
    transferred: bool = False     # assistant already live-transferred this call


@app.post("/webhooks/telnyx/assistant/{token}", include_in_schema=False)
async def telnyx_assistant_webhook(token: str, request: Request):
    """Called by the Telnyx AI Assistant's own Webhook tool while it's on a
    call, so every inbound call to the Watson Factor number gets a CRM
    record - a new lead, or a touch on an existing one - without Daniel
    having to do anything. Same unguessable-token auth as the voice webhook
    above; see telnyx_webhook_token()."""
    if token != telnyx_webhook_token():
        raise HTTPException(404, "not found")
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    body = AssistantCallLog(**{k: v for k, v in (raw or {}).items()
                                if k in AssistantCallLog.model_fields})

    phone = telnyx.to_e164(body.caller_phone)
    if not phone:
        # Still worth a 200 - the assistant isn't going to retry a failed
        # tool call mid-conversation, and there's nothing else to key on.
        return {"ok": False, "error": "no usable caller_phone"}

    name = body.caller_name.strip()
    urgency = body.urgency.strip().lower()

    bits = []
    if body.reason.strip(): bits.append(body.reason.strip())
    if body.requested_time.strip(): bits.append(f"asked for: {body.requested_time.strip()}")
    if body.notes.strip(): bits.append(body.notes.strip())
    note = " — ".join(bits) or "(no details captured)"
    outcome = "transferred" if body.transferred else ("hot" if urgency == "hot" else "logged")

    with closing(db()) as c:
        # Same find-or-create every other inbound path uses, so a caller the
        # receptionist answers today and the text-back catches tomorrow is
        # one lead, not two.
        # A routine logged call (the receptionist logs wrong numbers too) is
        # a note, not a Today task - Daniel gets the text summary. A HOT one
        # goes to the top of Today; before this it was a note like the rest.
        pid = inbound_lead(c, phone, name=name, kind="ai_receptionist",
                           outcome=outcome, note=note,
                           hot=(urgency == "hot" and not body.transferred))
        srt = {}
        if not body.transferred:
            srt = sort_inbound(c, pid, " - ".join(x for x in (body.reason.strip(), body.notes.strip()) if x),
                               urgency=urgency, requested_time=body.requested_time.strip(), channel="sms")
            if srt.get("bucket") == "hot":
                put_on_the_phone(c, pid, "Hot inbound: " + srt.get("why", ""))

        # A requested time with nobody having actually confirmed it (Telnyx's
        # AI Assistant has no native calendar booking) is a dropped ball
        # waiting to happen unless it lands somewhere Daniel will see it -
        # so it becomes a same-day callback in Today, not just a buried note.
        if body.requested_time.strip() and not body.transferred:
            c.execute("UPDATE prospects SET next_due=date('now'), next_action='call' "
                      "WHERE id=?", (pid,))
        c.commit()
    return {"ok": True, "prospect_id": pid, "bucket": srt.get("bucket", "")}


class TextBody(BaseModel):
    text: str = Field(..., max_length=600)


@app.post("/api/prospect/{pid}/text")
def text_prospect(request: Request, pid: int, body: TextBody):
    """Send the drafted reply (or Daniel's edit of it) as a text. His tap
    is the approval. Never to a number that said STOP."""
    require_auth(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to send.")
    api_key, frm = setting("telnyx_api_key", ""), setting("telnyx_from_number", "")
    if not (api_key and frm):
        raise HTTPException(400, "Texting isn't set up (Setup → Phone).")
    with closing(db()) as c:
        p = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "not found")
        n = telnyx.to_e164(p["phone"] or "")
        if not n:
            raise HTTPException(400, "No phone number on this lead.")
        if _optout_exists(c, n):
            raise HTTPException(400, "They texted STOP - no more texts to this number.")
        try:
            mid = tb._send(api_key=api_key, from_number=frm, to_number=n, text=text,
                           messaging_profile_id=setting("telnyx_messaging_profile_id", ""))
        except Exception as e:
            raise HTTPException(502, "Telnyx wouldn't send it: %s" % str(e)[:200])
        c.execute("INSERT INTO sms_log (direction, number, text, message_id, at) VALUES ('out',?,?,?,?)",
                  (n, text, mid, now()))
        log(c, pid, "sms", "sent", "Texted: " + text[:300])
        c.execute("UPDATE prospects SET inbound_reply='', touched_at=? WHERE id=?", (now(), pid))
        c.commit()
    return {"ok": True, "message_id": mid}


@app.get("/api/prospect/{pid}/history")
def history(request: Request, pid: int):
    require_auth(request)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT kind, outcome, note, at FROM touches WHERE prospect_id=? ORDER BY id DESC",
            (pid,)).fetchall()]
    return {"touches": rows}


class RenameBody(BaseModel):
    name: str


@app.post("/api/prospect/{pid}/rename")
def rename(request: Request, pid: int, body: RenameBody):
    """Manual override. Some sites sit behind a bot check and never give us
    a readable name — typing it takes three seconds and it sticks."""
    require_auth(request)
    name = (body.name or "").strip()[:80]
    if not name:
        raise HTTPException(400, "Name can't be empty")
    with closing(db()) as c:
        cur = c.execute("UPDATE prospects SET company=? WHERE id=?", (name, pid))
        c.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "not found")
    return {"ok": True, "name": name}


@app.post("/api/prospect/{pid}/refresh_name")
def refresh_name(request: Request, pid: int):
    """Try the site again for a real business name."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT company, domain FROM prospects WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    found = discover_name(r["domain"])
    if not found:
        return {"ok": False, "reason": "nothing usable on the site"}
    with closing(db()) as c:
        c.execute("UPDATE prospects SET company=? WHERE id=?", (found, pid))
        c.commit()
    return {"ok": True, "name": found}


# ═══════════════════════ CRM ═══════════════════════
# One record per business, moving along a pipeline. `stage` is where the money
# is; `status` (already present) is what the outreach queue should do next.
# They are deliberately separate: a customer can still be owed a call, and a
# dead lead still needs to stay out of the queue.

STAGES = [
    ("lead",     "New lead",      "Found, not contacted yet"),
    ("working",  "Working",       "In the outreach queue"),
    ("warm",     "Replied",       "They wrote back — follow up now"),
    ("review",   "Review booked", "Paid operations review on the calendar"),
    ("proposal", "Proposal out",  "Build quoted, waiting on them"),
    ("customer", "Customer",      "Won — work in progress or on retainer"),
    ("lost",     "Lost",          "Not moving forward"),
]
STAGE_KEYS = [k for k, _, _ in STAGES]
OPEN_STAGES = ("lead", "working", "warm", "review", "proposal")


def money(v):
    try:
        return round(float(v or 0), 2)
    except Exception:
        return 0.0


def deal_rows(c, pid):
    out = [dict(r) for r in c.execute(
        "SELECT * FROM deals WHERE prospect_id=? ORDER BY id DESC", (pid,)).fetchall()]
    for d in out:
        d["kind_label"] = DEAL_LABELS.get(d.get("kind") or "", d.get("kind") or "")
    return out


def file_rows(c, pid):
    try:
        return [dict(r) for r in c.execute(
            "SELECT id, deal_id, kind, name, bytes, note, added_at FROM files "
            "WHERE prospect_id=? AND removed_at IS NULL ORDER BY id DESC", (pid,)).fetchall()]
    except sqlite3.OperationalError:
        return []


def contact_rows(c, pid):
    return [dict(r) for r in c.execute(
        "SELECT * FROM contacts WHERE prospect_id=? ORDER BY is_primary DESC, id",
        (pid,)).fetchall()]


def log(c, pid, kind, outcome="", note=""):
    c.execute("INSERT INTO touches (prospect_id, kind, outcome, note, at) VALUES (?,?,?,?,?)",
              (pid, kind, outcome, note, now()))


@app.get("/api/crm")
def crm(request: Request, q: str = "", trade: str = ""):
    """The pipeline board: every business grouped by stage, with money attached.
    `q` searches name, domain, city and trade; `trade` narrows to one trade
    (a category like "Veterinary" or a broad group like "auto")."""
    require_auth(request)
    conds, args = [], []
    if q:
        conds.append("(p.company LIKE ? OR p.domain LIKE ? OR p.city LIKE ? OR p.category LIKE ? OR p.vertical LIKE ?)")
        args += [f"%{q}%"] * 5
    if trade == "__none__":
        conds.append("COALESCE(p.category,'')=''")
    elif trade:
        conds.append("(lower(COALESCE(p.category,''))=lower(?) OR lower(COALESCE(p.vertical,''))=lower(?))")
        args += [trade, trade]
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with closing(db()) as c:
        # the trade menu counts everything on file, whatever is filtered now
        hid = hidden_trades()
        trades = [{"trade": r[0], "n": r[1]} for r in c.execute(
            "SELECT category, COUNT(*) FROM prospects WHERE COALESCE(category,'')<>'' "
            "GROUP BY category ORDER BY COUNT(*) DESC, category") if not is_hidden_trade(r[0], hid)]
        untraded = c.execute("SELECT COUNT(*) FROM prospects WHERE COALESCE(category,'')=''").fetchone()[0]
        groups = [{"trade": r[0], "n": r[1]} for r in c.execute(
            "SELECT vertical, COUNT(*) FROM prospects WHERE COALESCE(vertical,'')<>'' "
            "GROUP BY vertical ORDER BY COUNT(*) DESC")]
        rows = [dict(r) for r in c.execute(f"""
            SELECT p.*,
              (SELECT COUNT(*) FROM deals d WHERE d.prospect_id=p.id AND d.state='open')  AS open_deals,
              (SELECT IFNULL(SUM(d.amount),0) FROM deals d WHERE d.prospect_id=p.id AND d.state='open') AS open_value,
              (SELECT IFNULL(SUM(d.amount),0) FROM deals d WHERE d.prospect_id=p.id AND d.state='won')  AS won_value,
              (SELECT IFNULL(SUM(d.mrr),0)    FROM deals d WHERE d.prospect_id=p.id AND d.state='won')  AS mrr,
              (SELECT COUNT(*) FROM contacts ct WHERE ct.prospect_id=p.id) AS contact_count,
              (SELECT name FROM contacts ct WHERE ct.prospect_id=p.id
                 ORDER BY is_primary DESC, id LIMIT 1) AS primary_contact
            FROM prospects p{where}
            ORDER BY p.stage, COALESCE(p.stage_at, p.added_at) DESC""", args).fetchall()]

    board = {k: [] for k in STAGE_KEYS}
    for r in rows:
        r["findings"] = []          # keep the payload light for a board view
        board.setdefault(r.get("stage") or "lead", []).append(r)

    pipeline_value = sum(r["open_value"] for r in rows if r.get("stage") != "lost")
    won_value = sum(r["won_value"] for r in rows)
    mrr = sum(r["mrr"] for r in rows)
    return {
        "stages": [{"key": k, "label": l, "hint": h,
                    "count": len(board.get(k, [])),
                    "value": round(sum(x["won_value"] if k == "customer" else x["open_value"]
                                       for x in board.get(k, [])), 2),
                    "open_value": round(sum(x["open_value"] for x in board.get(k, [])), 2)}
                   for k, l, h in STAGES],
        "board": board,
        "trades": trades, "groups": groups, "untraded": untraded, "trade": trade, "q": q,
        "totals": {
            "businesses": len(rows),
            "customers": len(board.get("customer", [])),
            "pipeline_value": round(pipeline_value, 2),
            "won_value": round(won_value, 2),
            "mrr": round(mrr, 2),
        },
    }



# ── the Leads table ──────────────────────────────────────────────────────
# One grid over prospects + pipeline + outreach, the way a contacts table
# works in every CRM: search, sort, filter by stage or tag, pick rows, do
# one thing to all of them, CSV in and out. Find leads (the Google search)
# and Pipeline (the board) stay as views; this is the list.
LEAD_SORTS = {"company": "p.company", "city": "p.city", "trade": "p.category", "stage": "p.stage",
              "score": "p.score", "last": "last_activity", "added": "p.added_at", "emails": "emails_sent"}


def _tags(raw: str) -> list:
    seen, out = set(), []
    for t in (raw or "").replace("\n", ",").split(","):
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower()); out.append(t[:40])
    return out


@app.get("/api/leads")
def leads_table(request: Request, q: str = "", stage: str = "", tag: str = "", trade: str = "",
                sort: str = "last", dir: str = "desc", page: int = 1, per: int = 50):
    require_auth(request)
    conds, args = [], []
    if q:
        conds.append("(p.company LIKE ? OR p.domain LIKE ? OR p.city LIKE ? OR p.category LIKE ? OR p.email LIKE ? OR p.phone LIKE ? OR p.tags LIKE ?)")
        args += ["%%%s%%" % q] * 7
    if stage:
        conds.append("COALESCE(p.stage,'lead')=?"); args.append(stage)
    if tag:
        conds.append("(',' || lower(COALESCE(p.tags,'')) || ',') LIKE ?"); args.append("%%,%s,%%" % tag.lower().replace(" ", " "))
    if trade:
        conds.append("lower(COALESCE(p.category,''))=lower(?)"); args.append(trade)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    order = LEAD_SORTS.get(sort, "last_activity")
    d = "ASC" if (dir or "").lower() == "asc" else "DESC"
    per = max(10, min(int(per or 50), 200)); page = max(1, int(page or 1))
    base = """FROM prospects p{where}"""
    with closing(db()) as c:
        total = c.execute("SELECT COUNT(*) " + base.format(where=where), args).fetchone()[0]
        rows = [dict(r) for r in c.execute("""
            SELECT p.id, p.company, p.domain, p.phone, p.email, p.city, p.category, p.vertical,
                   COALESCE(p.stage,'lead') AS stage, p.status, p.score, p.grade, p.tags, p.added_at, p.next_due,
                   p.inbound_bucket,
                   (SELECT COUNT(*) FROM outreach o WHERE o.prospect_id=p.id AND o.state='sent') AS emails_sent,
                   (SELECT name FROM contacts ct WHERE ct.prospect_id=p.id ORDER BY is_primary DESC, id LIMIT 1) AS contact,
                   COALESCE(
                     (SELECT MAX(x) FROM (SELECT MAX(o.sent_at) x FROM outreach o WHERE o.prospect_id=p.id AND o.state='sent'
                                          UNION ALL SELECT MAX(t.at) FROM touches t WHERE t.prospect_id=p.id
                                          UNION ALL SELECT p.inbound_at)), p.added_at) AS last_activity
            """ + base.format(where=where) + " ORDER BY (%s IS NULL), %s %s, p.id DESC LIMIT ? OFFSET ?" % (order, order, d),
            args + [per, (page - 1) * per]).fetchall()]
        all_tags = {}
        for r in c.execute("SELECT tags FROM prospects WHERE COALESCE(tags,'')<>''"):
            for t in _tags(r[0]):
                all_tags[t] = all_tags.get(t, 0) + 1
        stages = {r[0] or "lead": r[1] for r in c.execute("SELECT COALESCE(stage,'lead'), COUNT(*) FROM prospects GROUP BY 1")}
    for r in rows:
        r["tags"] = _tags(r.get("tags"))
    return {"rows": rows, "total": total, "page": page, "per": per, "pages": max(1, -(-total // per)),
            "stages": [{"key": k, "label": lbl, "n": stages.get(k, 0)} for k, lbl, _ in STAGES],
            "tags": sorted(({"tag": k, "n": v} for k, v in all_tags.items()), key=lambda x: -x["n"])[:40]}


class LeadsBulkBody(BaseModel):
    ids: list = Field(default_factory=list)
    action: str = ""           # tag | untag | stage | pass | delete_tag
    value: str = ""


@app.post("/api/leads/bulk")
def leads_bulk(request: Request, body: LeadsBulkBody):
    require_auth(request)
    ids = [int(i) for i in body.ids if str(i).isdigit()][:500]
    if not ids:
        raise HTTPException(400, "Pick at least one lead first.")
    n = 0
    with closing(db()) as c:
        if body.action in ("tag", "untag"):
            t = (body.value or "").strip()[:40]
            if not t:
                raise HTTPException(400, "Type the tag first.")
            for pid in ids:
                r = c.execute("SELECT tags FROM prospects WHERE id=?", (pid,)).fetchone()
                if not r:
                    continue
                cur = _tags(r[0])
                new = ([x for x in cur if x.lower() != t.lower()] + ([t] if body.action == "tag" else []))
                c.execute("UPDATE prospects SET tags=? WHERE id=?", (", ".join(_tags(", ".join(new))), pid)); n += 1
        elif body.action == "stage":
            if body.value not in STAGE_KEYS:
                raise HTTPException(400, "That isn't a stage.")
            for pid in ids:
                c.execute("UPDATE prospects SET stage=?, stage_at=? WHERE id=?", (body.value, now(), pid)); n += 1
        elif body.action == "pass":
            for pid in ids:
                c.execute("UPDATE prospects SET status='dead', stage='lost', stage_at=? WHERE id=?", (now(), pid)); n += 1
        else:
            raise HTTPException(400, "Unknown action.")
        c.commit()
    return {"ok": True, "n": n}


class LeadsImportBody(BaseModel):
    csv_text: str = ""
    tag: str = ""


@app.post("/api/leads/import")
def leads_import(request: Request, body: LeadsImportBody):
    """CSV in. Columns by header name, any order, case-insensitive:
    company (required), domain|website, phone, email, city, address,
    trade|category, contact, tags, note. Matches an existing row by domain
    (then by company+city) and fills blanks; never overwrites a value you
    already have. Everything imported can be tagged in one go."""
    require_auth(request)
    text = (body.csv_text or "").lstrip("﻿")
    if not text.strip():
        raise HTTPException(400, "The file is empty.")
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        raise HTTPException(400, "No header row. The first line needs column names like company, phone, email.")
    alias = {"company": "company", "business": "company", "name": "company", "business name": "company",
             "domain": "domain", "website": "domain", "site": "domain", "url": "domain",
             "phone": "phone", "telephone": "phone", "email": "email", "e-mail": "email",
             "city": "city", "address": "address", "trade": "category", "category": "category", "type": "category",
             "contact": "contact", "contact name": "contact", "owner": "contact",
             "tags": "tags", "tag": "tags", "note": "note", "notes": "note"}
    cols = {h: alias.get((h or "").strip().lower()) for h in rdr.fieldnames}
    if "company" not in cols.values():
        raise HTTPException(400, "I couldn't find a company column. Name one of the columns company, business or name.")
    added = updated = skipped = 0
    extra_tag = (body.tag or "").strip()[:40]
    with closing(db()) as c:
        for raw in rdr:
            row = {}
            for h, k in cols.items():
                if k and raw.get(h) is not None:
                    row[k] = (raw.get(h) or "").strip()
            company = row.get("company", "")
            if not company:
                skipped += 1; continue
            dom = re.sub(r"^https?://(www\.)?", "", row.get("domain", "").lower()).split("/")[0].strip()
            tags = _tags(row.get("tags", "") + ("," + extra_tag if extra_tag else ""))
            found = None
            if dom:
                found = c.execute("SELECT * FROM prospects WHERE lower(domain)=?", (dom,)).fetchone()
            if not found:
                found = c.execute("SELECT * FROM prospects WHERE lower(company)=lower(?) AND lower(COALESCE(city,''))=lower(?)",
                                  (company, row.get("city", ""))).fetchone()
            if found:
                sets, vals = [], []
                for k in ("phone", "email", "city", "address", "category"):
                    if row.get(k) and not (found[k] or "").strip():
                        sets.append("%s=?" % k); vals.append(row[k])
                if dom and not (found["domain"] or "").strip():
                    sets.append("domain=?"); vals.append(dom)
                merged = _tags(", ".join(_tags(found["tags"]) + tags))
                if merged != _tags(found["tags"]):
                    sets.append("tags=?"); vals.append(", ".join(merged))
                if row.get("note") and not (found["note"] or "").strip():
                    sets.append("note=?"); vals.append(row["note"][:2000])
                if sets:
                    c.execute("UPDATE prospects SET %s WHERE id=?" % ", ".join(sets), vals + [found["id"]]); updated += 1
                else:
                    skipped += 1
                pid = found["id"]
            else:
                c.execute("INSERT INTO prospects (company, domain, phone, email, city, address, category, tags, note, status, stage, stage_at, added_at, scan_state) "
                          "VALUES (?,?,?,?,?,?,?,?,?,'new','lead',?,?,?)",
                          (company[:160], dom, row.get("phone", "")[:40], row.get("email", "")[:160], row.get("city", "")[:80],
                           row.get("address", "")[:200], row.get("category", "")[:80], ", ".join(tags), row.get("note", "")[:2000],
                           now(), now(), "pending" if dom else "skipped"))
                pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]; added += 1
            if row.get("contact"):
                if not c.execute("SELECT 1 FROM contacts WHERE prospect_id=? AND lower(name)=lower(?)", (pid, row["contact"])).fetchone():
                    c.execute("INSERT INTO contacts (prospect_id, name, email, phone, is_primary, added_at) VALUES (?,?,?,?,?,?)",
                              (pid, row["contact"][:120], row.get("email", "")[:160], row.get("phone", "")[:40],
                               0 if c.execute("SELECT 1 FROM contacts WHERE prospect_id=?", (pid,)).fetchone() else 1, now()))
        c.commit()
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}


@app.get("/api/leads.csv")
def leads_csv(request: Request, q: str = "", stage: str = "", tag: str = ""):
    """CSV out, same columns the import reads, so a file can go out, get
    edited in a spreadsheet, and come back."""
    require_auth(request)
    d = leads_table(request, q=q, stage=stage, tag=tag, sort="company", dir="asc", page=1, per=200)
    rows = d["rows"]
    if d["pages"] > 1:
        for pg in range(2, d["pages"] + 1):
            rows += leads_table(request, q=q, stage=stage, tag=tag, sort="company", dir="asc", page=pg, per=200)["rows"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "website", "phone", "email", "city", "trade", "contact", "stage", "tags", "score", "emails_sent", "last_activity"])
    for r in rows:
        w.writerow([r["company"], r["domain"], r["phone"], r["email"], r["city"], r["category"], r["contact"] or "",
                    r["stage"], ", ".join(r["tags"]), r["score"] if r["score"] is not None else "", r["emails_sent"], r["last_activity"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="just-grit-leads.csv"'})


@app.get("/api/prospect/{pid}")
def prospect_detail(request: Request, pid: int):
    """Everything about one business — the record you open before a call."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        d = row_to_dict(r)
        d["contacts"] = contact_rows(c, pid)
        d["deals"] = deal_rows(c, pid)
        d["files"] = file_rows(c, pid)
        d["timeline"] = [dict(t) for t in c.execute(
            "SELECT kind, outcome, note, at FROM touches WHERE prospect_id=? "
            "ORDER BY id DESC LIMIT 60", (pid,)).fetchall()]
    return d


class StageBody(BaseModel):
    stage: str
    note: str = ""


@app.post("/api/prospect/{pid}/stage")
def set_stage(request: Request, pid: int, body: StageBody):
    require_auth(request)
    if body.stage not in STAGE_KEYS:
        raise HTTPException(400, f"stage must be one of {STAGE_KEYS}")
    with closing(db()) as c:
        r = c.execute("SELECT stage, domain FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        # Losing a deal is also a promise to stop calling.
        extra = ""
        if body.stage == "lost":
            extra = ", status='dead', next_due=NULL, next_action=NULL"
            c.execute("UPDATE deals SET state='lost', closed_at=? "
                      "WHERE prospect_id=? AND state='open'", (now(), pid))
            retire_drafts(c, pid, "marked lost in the Pipeline")
        elif body.stage == "customer":
            extra = ", status='booked', next_due=NULL, next_action=NULL"
            retire_drafts(c, pid, "they're a customer now")
        elif body.stage in ("review", "proposal"):
            # A review on the calendar or a proposal out is a live
            # conversation: the cold sequence and the drafting sweep stop,
            # Today keeps them as a call.
            extra = ", status='conversation'"
            retire_drafts(c, pid, "in %s - a person is on this" % body.stage)
        c.execute(f"UPDATE prospects SET stage=?, stage_at=?{extra} WHERE id=?",
                  (body.stage, now(), pid))
        log(c, pid, "stage", body.stage, body.note or f"{r['stage']} → {body.stage}")
        c.commit()
        dom = r["domain"]
    if body.stage == "lost" and dom:
        try:
            suppress(dom)
        except Exception:
            pass
    return {"ok": True, "stage": body.stage}


class ContactBody(BaseModel):
    name: str
    role: str = ""
    email: str = ""
    phone: str = ""
    is_primary: bool = False
    note: str = ""


@app.post("/api/prospect/{pid}/contact")
def add_contact(request: Request, pid: int, body: ContactBody):
    require_auth(request)
    if not body.name.strip():
        raise HTTPException(400, "A contact needs a name.")
    with closing(db()) as c:
        if body.is_primary:
            c.execute("UPDATE contacts SET is_primary=0 WHERE prospect_id=?", (pid,))
        cur = c.execute("""INSERT INTO contacts
            (prospect_id,name,role,email,phone,is_primary,note,added_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (pid, body.name.strip(), body.role.strip(), body.email.strip(),
             body.phone.strip(), 1 if body.is_primary else 0, body.note.strip(), now()))
        # keep the business-level email in sync so the mail draft has somewhere to go
        if body.email.strip():
            c.execute("UPDATE prospects SET email=? WHERE id=? AND (email IS NULL OR email='')",
                      (body.email.strip(), pid))
        log(c, pid, "contact", "added", body.name.strip())
        c.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.delete("/api/contact/{cid}")
def del_contact(request: Request, cid: int):
    require_auth(request)
    with closing(db()) as c:
        c.execute("DELETE FROM contacts WHERE id=?", (cid,))
        c.commit()
    return {"ok": True}


class DealBody(BaseModel):
    title: str = ""
    kind: str = "build"
    amount: float = 0
    mrr: float = 0
    note: str = ""


DEAL_KINDS = {
    "review":   "Operations review",
    "build":    "Software build",
    "retainer": "Monthly retainer",
    "other":    "Other work",
}

# HomeRepair Tech sells products, not software builds, so its deal picker
# lists the offer catalog (HomeRepair_Offer_Catalog.md, effective 1 Oct 2026).
# Picking one fills the money boxes; they stay editable because a 40-door
# portfolio or an HOA is never list price.
# key, label, one-time $, monthly $, hint
HR_DEAL_PRODUCTS = [
    ("protect_essential", "Protect Essential", 49, 59,
     "4 seasonal visits. $59/mo + $49 setup (setup waived on annual, $588)."),
    ("protect_basic", "Protect Basic", 49, 29,
     "Score, HomePassport and member labor rate. $29/mo + $49 setup (annual $276)."),
    ("protect_pro", "Protect Pro (per property)", 49, 179,
     "12 concierge visits, same-day emergency dispatch. $179/mo per property."),
    ("audit", "Home Health & Safety Audit", 49, 0, "$49 - waived on an annual plan."),
    ("report", "HomePassport Property Report", 79, 0, "$79 one-time."),
    ("portfolio", "Portfolio plan (multi-property or HOA)", 0, 0,
     "Custom. Enter the one-time total and the monthly total."),
    ("repair_bid", "Repair or maintenance bid", 0, 0, "A one-time job. Enter the bid."),
    ("partner", "Partner / referral program", 0, 0, "Realtor or partner arrangement."),
    ("other", "Other work", 0, 0, ""),
]
DEAL_LABELS = dict(DEAL_KINDS)
DEAL_LABELS.update({k: lbl for k, lbl, _a, _m, _h in HR_DEAL_PRODUCTS})


@app.get("/api/deal_kinds")
def deal_kinds(request: Request):
    """What the "Add a deal" picker offers in this workspace."""
    require_auth(request)
    if ws.current() == "homerepair":
        return {"default": "protect_essential",
                "kinds": [{"key": k, "label": lbl, "amount": a, "mrr": m, "hint": h}
                          for k, lbl, a, m, h in HR_DEAL_PRODUCTS]}
    return {"default": "build",
            "kinds": [{"key": k, "label": lbl, "amount": 0, "mrr": 0, "hint": ""}
                      for k, lbl in DEAL_KINDS.items()]}


@app.post("/api/prospect/{pid}/deal")
def add_deal(request: Request, pid: int, body: DealBody):
    require_auth(request)
    kind = body.kind if body.kind in DEAL_LABELS else "other"
    title = body.title.strip() or DEAL_LABELS[kind]
    with closing(db()) as c:
        cur = c.execute("""INSERT INTO deals
            (prospect_id,title,kind,amount,mrr,state,opened_at,note)
            VALUES (?,?,?,?,?, 'open', ?, ?)""",
            (pid, title, kind, money(body.amount), money(body.mrr), now(), body.note.strip()))
        log(c, pid, "deal", "opened", f"{title} · ${money(body.amount):,.0f}")
        c.commit()
    return {"ok": True, "id": cur.lastrowid}


class DealStateBody(BaseModel):
    state: str          # open | won | lost
    note: str = ""


@app.post("/api/deal/{did}/state")
def set_deal_state(request: Request, did: int, body: DealStateBody):
    require_auth(request)
    if body.state not in ("open", "won", "lost"):
        raise HTTPException(400, "state must be open, won or lost")
    with closing(db()) as c:
        r = c.execute("SELECT * FROM deals WHERE id=?", (did,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        c.execute("UPDATE deals SET state=?, closed_at=?, note=? WHERE id=?",
                  (body.state, now() if body.state != "open" else None,
                   body.note or r["note"], did))
        log(c, r["prospect_id"], "deal", body.state, f"{r['title']} · ${money(r['amount']):,.0f}")
        # Winning anything makes them a customer. That's the whole point.
        if body.state == "won":
            c.execute("UPDATE prospects SET stage='customer', stage_at=?, status='booked', "
                      "next_due=NULL, next_action=NULL WHERE id=?", (now(), r["prospect_id"]))
            retire_drafts(c, r["prospect_id"], "deal won")
        elif body.state == "lost":
            still_open = c.execute("SELECT 1 FROM deals WHERE prospect_id=? AND state='open'",
                                   (r["prospect_id"],)).fetchone()
            if not still_open:
                sync_stage(c, r["prospect_id"], "dead")
        c.commit()
    return {"ok": True}


@app.delete("/api/deal/{did}")
def del_deal(request: Request, did: int):
    require_auth(request)
    with closing(db()) as c:
        c.execute("DELETE FROM deals WHERE id=?", (did,))
        c.commit()
    return {"ok": True}


class NoteBody(BaseModel):
    note: str


@app.post("/api/prospect/{pid}/note")
def add_note(request: Request, pid: int, body: NoteBody):
    """A free-text line on the timeline — what was said, what was promised."""
    require_auth(request)
    if not body.note.strip():
        raise HTTPException(400, "Empty note.")
    with closing(db()) as c:
        log(c, pid, "note", "", body.note.strip())
        c.execute("UPDATE prospects SET touched_at=? WHERE id=?", (now(), pid))
        c.commit()
    return {"ok": True}


# --------- a follow-up date you set by hand ---------
# "Call him back the day after the HOA meeting." The date lands on the
# Calendar as its own card and on Today when it comes due. next_action is
# 'follow_up' on purpose: it is NOT 'email_followup', which belongs to the
# automatic sequence, so nothing here ever sends an email by itself.
FOLLOWUP_ACTION = "follow_up"


class FollowBody(BaseModel):
    date: str = ""        # YYYY-MM-DD; empty clears the follow-up
    time: str = ""        # HH:MM, optional
    note: str = ""


@app.post("/api/prospect/{pid}/followup")
def set_followup(request: Request, pid: int, body: FollowBody):
    require_auth(request)
    day = (body.date or "").strip()
    with closing(db()) as c:
        r = c.execute("SELECT status, next_action, company FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if not day:
            if (r["next_action"] or "") == FOLLOWUP_ACTION:
                c.execute("UPDATE prospects SET next_due=NULL, next_action=NULL, "
                          "follow_time='', follow_note='' WHERE id=?", (pid,))
                log(c, pid, "follow_up", "cleared", "Follow-up removed")
            c.commit()
            return {"ok": True, "cleared": True}
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "Pick a date for the follow-up.")
        today = datetime.now(workspace_tz()).date()
        if d < today:
            raise HTTPException(400, "That date has already passed. Pick today or later.")
        if d > today + timedelta(days=1100):
            raise HTTPException(400, "That date is too far out.")
        if (r["status"] or "") in ("dead", "corporate"):
            raise HTTPException(400, "This lead is marked lost or unsubscribed. "
                                     "Move it back to a stage first.")
        tm = (body.time or "").strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", tm):
            tm = ""
        keep = ("booked",)
        status_sql = "" if (r["status"] or "") in keep else ", status='conversation'"
        c.execute("UPDATE prospects SET next_due=?, next_action=?, follow_time=?, follow_note=?, "
                  "touched_at=?" + status_sql + " WHERE id=?",
                  (d.isoformat(), FOLLOWUP_ACTION, tm, body.note.strip()[:200], now(), pid))
        retire_drafts(c, pid, "a follow-up date is set - a person is on this")
        log(c, pid, "follow_up", "set", "Follow up %s%s%s" % (
            d.strftime("%b %-d, %Y"), (" at " + tm) if tm else "",
            (" - " + body.note.strip()[:160]) if body.note.strip() else ""))
        c.commit()
    return {"ok": True, "date": d.isoformat(), "time": tm}


# --------- proposals, bids and contracts attached to an account ---------
FILE_KINDS = {"proposal": "Proposal", "bid": "Bid", "contract": "Contract", "other": "Other"}
FILE_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv", ".txt": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".heic": "image/heic",
}
FILE_MAX = 25 * 1024 * 1024


def _files_root() -> pathlib.Path:
    return DATA / "files"


def _file_dir(pid: int) -> pathlib.Path:
    # the workspace slug is part of the path, so the two businesses' files
    # never share a folder either
    return _files_root() / ws.current() / str(int(pid))


@app.post("/api/prospect/{pid}/file")
async def upload_file(request: Request, pid: int, name: str = "", kind: str = "proposal", note: str = ""):
    """Raw bytes in the body (same reason as /api/media: no multipart
    dependency). The name and kind ride in the query string."""
    require_auth(request)
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    clean = re.sub(r"[^A-Za-z0-9._ ()&+,'-]", "_", base)[:120].strip(" .")
    ext = os.path.splitext(clean)[1].lower()
    if not clean:
        raise HTTPException(400, "The file needs a name.")
    if ext not in FILE_TYPES:
        raise HTTPException(400, "That file type is not allowed. Use PDF, Word, Excel, "
                                 "PowerPoint, an image, CSV or a text file.")
    ln = request.headers.get("content-length", "")
    if ln.isdigit() and int(ln) > FILE_MAX:
        raise HTTPException(413, "That file is over 25 MB.")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "The file was empty.")
    if len(raw) > FILE_MAX:
        raise HTTPException(413, "That file is over 25 MB.")
    kind = kind if kind in FILE_KINDS else "other"
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM prospects WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "not found")
        folder = _file_dir(pid)
        folder.mkdir(parents=True, exist_ok=True)
        stored = uuid.uuid4().hex[:16] + ext
        (folder / stored).write_bytes(raw)
        cur = c.execute("INSERT INTO files (prospect_id, kind, name, stored, mime, bytes, note, added_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (pid, kind, clean, stored, FILE_TYPES[ext], len(raw), note.strip()[:200], now()))
        log(c, pid, "file", kind, "%s attached: %s" % (FILE_KINDS[kind], clean))
        c.execute("UPDATE prospects SET touched_at=? WHERE id=?", (now(), pid))
        c.commit()
    return {"ok": True, "id": cur.lastrowid, "name": clean}


@app.get("/api/file/{fid}")
def download_file(request: Request, fid: int, inline: int = 0):
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not r or r["removed_at"]:
        raise HTTPException(404, "No such file.")
    path = (_file_dir(r["prospect_id"]) / r["stored"]).resolve()
    if _files_root().resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "That file is missing from disk.")
    show = bool(inline) and (r["mime"] or "") in ("application/pdf", "image/png", "image/jpeg")
    return FileResponse(path, media_type=r["mime"] or "application/octet-stream",
                        filename=r["name"],
                        content_disposition_type="inline" if show else "attachment",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.delete("/api/file/{fid}")
def remove_file(request: Request, fid: int):
    """Takes it off the account. The file itself stays on disk."""
    require_auth(request)
    with closing(db()) as c:
        r = c.execute("SELECT prospect_id, name, kind FROM files WHERE id=? AND removed_at IS NULL", (fid,)).fetchone()
        if not r:
            raise HTTPException(404, "No such file.")
        c.execute("UPDATE files SET removed_at=? WHERE id=?", (now(), fid))
        log(c, r["prospect_id"], "file", "removed", "Removed %s" % r["name"])
        c.commit()
    return {"ok": True}


# --------- looking a business up so you can actually call it ---------
# Half the rows in this database came from a pasted domain, which means no
# phone, no address, no name you would say out loud. Two sources fix that:
# the business's own website (free) and Google Places (one billable request).
#
# Nothing here overwrites something a human typed. Blanks only.

# Junk that shows up in mailto: links but is never a person.
# Two more classes learned from the San Antonio pull:
#   info@sampleaddress.com   - a template placeholder the site never replaced
#   accessibility@homeriver.com - a compliance role address. On their own domain,
#   so the domain check passes it, but nobody reads it for business and mailing
#   a legal/accessibility inbox with a cold pitch is a bad first impression.
_JUNK_EMAIL = re.compile(
    r"(sentry|wixpress|example\.(com|org)|\.png|\.jpg|\.gif|@2x|"
    r"your-?email|email@|domain\.com|sample@|noreply@|no-reply@|"
    r"sampleaddress|yourdomain|yoursite|placeholder|"
    r"^(accessibility|privacy|legal|webmaster|postmaster|abuse|dmca|"
    r"unsubscribe|bounce|mailer-daemon)@)", re.I)

# 555 is reserved for fiction.
_FAKE_PHONE = re.compile(r"^\(?\d{3}\)?\s*555[\s.-]*\d{4}$")

_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.-]\s*\d{3}[\s.-]\d{4}\b")


def _tidy_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or len(set(d)) < 4:
        return ""
    formatted = f"({d[:3]}) {d[3:6]}-{d[6:]}"
    if _FAKE_PHONE.match(formatted):
        return ""
    return formatted


def scrape_contact(domain: str):
    """Read a phone and an email off the business's own homepage.

    Returns (found, reason). Their published number is better than a
    directory's: it is the one they answer.

    Uses the analyzer's own fetcher on purpose - same headers, same timeouts,
    same robots.txt manners. It also means a bot wall shows up here exactly
    as it shows up in a scan, and gets reported instead of swallowed.
    """
    out = {}
    if not domain:
        return out, "No website on file."
    try:
        from grit_analyzer.fetch import fetch_page
        res = fetch_page(domain)
    except Exception as e:
        return out, "Could not reach their site (%s)." % type(e).__name__

    if not res.robots_allowed:
        return out, "Their robots.txt asks us not to read the page, so we didn't."
    html = res.html or ""
    if res.status == 403 or "cf-mitigated" in {k.lower() for k in (res.headers or {})}:
        return out, ("Their site is behind a Cloudflare bot check right now, so "
                     "there is nothing to read. Look them up on Google instead.")
    if not html or res.status >= 400:
        return out, res.error or ("Their site answered with HTTP %s." % (res.status or "?"))

    for m in re.finditer(r'href=["\']tel:([^"\']+)', html, re.I):
        p = _tidy_phone(m.group(1))
        if p:
            out["phone"] = p
            break
    if "phone" not in out:
        for m in _PHONE_RE.finditer(re.sub(r"<[^>]+>", " ", html)):
            p = _tidy_phone(m.group(0))
            if p:
                out["phone"] = p
                break

    out.update(_emails_in(html, domain))

    # Homepage-only + mailto-only found an email on ZERO of 34 scanned
    # property-management sites. They put it on /contact, and often as plain
    # text rather than a link. If the homepage gave us nothing, follow one
    # contact-ish link and look again - one extra fetch, only when needed.
    if "email" not in out:
        for href in _contact_links(html, domain)[:2]:
            try:
                sub = fetch_page(href)
            except Exception:
                continue
            if not sub.robots_allowed or not sub.html or (sub.status or 0) >= 400:
                continue
            more = _emails_in(sub.html, domain)
            if more:
                out.update(more)
                break

    if not out:
        return out, "Read their homepage and contact page; neither lists a phone or an email."
    return out, ""


# Built with chr() so no quote character appears in the literal.
_Q = chr(34) + chr(39)
MAILTO_RE = "href=[" + _Q + "]mailto:([^" + _Q + "?]+)"
HREF_RE   = "href=[" + _Q + "]([^" + _Q + "#]+)[" + _Q + "]"
_CONTACT_WORDS = re.compile("contact|about|reach|team|staff|owner", re.I)


def _emails_in(html, domain):
    """Emails from mailto: links first, then plain text on the page.

    The docstring here always said an off-domain address is "worse than mailing
    nobody" - but the code then fell back to `clean[0]`, the first address of
    any kind on the page. Run across 36 property managers that produced three
    prospects aimed at font designers: the authors
    named in webfont license headers, scraped out of CSS the tag-stripper
    flattened into the page text.

    So: an address is trusted when it is on their own domain, or when a human
    deliberately linked it with mailto:. A bare address recovered from page
    text on somebody else's domain is never returned - that is the exact shape
    of the font-license bug, and of a site template's builder-support address.
    """
    root = (domain or "").lower().replace("www.", "").strip("/")
    linked = [urllib.parse.unquote(m.group(1)).strip().lower()
              for m in re.finditer(MAILTO_RE, html, re.I)]
    loose = [e.lower() for e in _EMAIL_RE.findall(re.sub("<[^>]+>", " ", html))]

    def ok(e):
        return ("@" in e and len(e) < 90 and not _JUNK_EMAIL.search(e)
                and not re.search(r"[.](png|jpe?g|gif|svg|webp|css|js)$", e, re.I))

    linked = [e for e in linked if ok(e)]
    loose  = [e for e in loose if ok(e)]
    on = lambda e: bool(root) and e.split("@")[-1].endswith(root)

    for bucket in (                        # best evidence first
            [e for e in linked if on(e)],      # their domain, deliberately linked
            [e for e in loose if on(e)],       # their domain, printed on the page
            [e for e in linked if not on(e)]): # someone else's, but a real mailto
        if bucket:
            return {"email": bucket[0]}
    # What's left is an off-domain address lifted from raw text. Surface it for
    # a human to look at; never hand it to the sender.
    stray = [e for e in loose if not on(e)]
    return {"email_unverified": stray[0]} if stray else {}


def _contact_links(html, domain):
    """Links that look like a contact page, on their own domain only."""
    root = (domain or "").lower().replace("www.", "")
    seen, urls = set(), []
    for m in re.finditer(HREF_RE, html, re.I):
        href = m.group(1).strip()
        if not _CONTACT_WORDS.search(href):
            continue
        if href.startswith("/"):
            href = "https://" + root + href
        elif not href.startswith("http"):
            continue
        elif root and root not in href.lower():
            continue
        if href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def _norm_name(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (n or "").lower())


def places_lookup(p: dict, key: str):
    """Find this exact business on Google. Returns (place, why_not).

    Refuses a guess. If we hold a domain, the result's website has to match
    it; otherwise the name has to match. Calling a phone number scraped off
    the wrong business is worse than having no phone number at all.
    """
    name = (p.get("company") or "").strip()
    dom = (p.get("domain") or "").strip()
    bits = [x for x in (name, p.get("city") or "", p.get("address") or "") if x]
    query = " ".join(bits) or dom
    if not query:
        return None, "Nothing to search on - no name, no city, no site."
    try:
        found = places_search(query, key, pages=1)
    except HTTPException as e:
        detail = str(e.detail)
        if "API key not valid" in detail or "API_KEY_INVALID" in detail:
            return None, ("Google is rejecting the API key as invalid. That breaks "
                          "every Google lookup in the app, not just this one - the "
                          "key needs replacing under Settings.")
        return None, detail
    if not found:
        return None, "Google has no listing matching '%s'." % query

    if dom:
        for pl in found:
            if domain_of(pl.get("websiteUri") or "") == dom:
                return pl, ""
        return None, ("Found listings, but none of them use " + dom +
                      " as their website, so we cannot be sure it is them.")
    tgt = _norm_name(name)
    for pl in found:
        got = (pl.get("displayName") or {}).get("text") or ""
        if tgt and _norm_name(got) == tgt:
            return pl, ""
    return None, "Google's closest match was not a confident enough name match."


class EnrichBody(BaseModel):
    use_google: bool = True


@app.post("/api/prospect/{pid}/enrich")
def enrich_prospect(request: Request, pid: int, body: EnrichBody):
    """Fill in the blanks on a lead: phone, email, address, rating, real name."""
    require_auth(request)
    with closing(db()) as c:
        row = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    p = dict(row)

    filled = {}
    sources = []
    notes = []

    def blank(col):
        v = p.get(col)
        if col == "phone" and isinstance(v, str) and v.strip():
            # A 555 number is reserved for fiction, so it is not contact
            # information and should not block a real lookup.
            return not _tidy_phone(v)
        return v is None or (isinstance(v, str) and not v.strip())

    # 1. their own website - free, and it is the number they answer
    if p.get("domain"):
        got, why = scrape_contact(p["domain"])
        for col in ("phone", "email"):
            if got.get(col) and blank(col):
                filled[col] = got[col]
        if got:
            sources.append("their website")
        if why:
            notes.append(why)

    # 2. Google Places - one billable request, only if it is confidently them
    if body.use_google:
        key = google_key()
        if not key:
            notes.append("No Google key set up, so only the website was read.")
        elif remaining_requests() <= 0:
            notes.append("This month's free Google lookups are used up.")
        else:
            place, why = places_lookup(p, key)
            if place:
                sources.append("Google")
                for col, val in (("phone", place.get("nationalPhoneNumber") or ""),
                                 ("address", place.get("formattedAddress") or "")):
                    if val and blank(col):
                        filled[col] = val
                if place.get("rating") and not p.get("rating"):
                    filled["rating"] = place["rating"]
                if place.get("userRatingCount") and not p.get("reviews"):
                    filled["reviews"] = place["userRatingCount"]
                if blank("city") and place.get("formattedAddress"):
                    parts = place["formattedAddress"].split(",")
                    if len(parts) >= 3:
                        filled["city"] = parts[-3].strip()
                nm = (place.get("displayName") or {}).get("text") or ""
                # Only upgrade a name that is still just the domain.
                if nm and _plausible_name(nm) and \
                   _norm_name(p.get("company") or "") == _norm_name(p.get("domain") or ""):
                    filled["company"] = nm
            else:
                notes.append(why)

    # 3. still no name worth saying out loud? read it off the page
    if p.get("domain") and "company" not in filled and \
       _norm_name(p.get("company") or "") == _norm_name(p.get("domain") or ""):
        nm = discover_name(p["domain"])
        if nm:
            filled["company"] = nm
            sources.append("their page title")

    if filled:
        cols = ", ".join("%s=?" % k for k in filled)
        with closing(db()) as c:
            c.execute("UPDATE prospects SET %s WHERE id=?" % cols,
                      list(filled.values()) + [pid])
            log(c, pid, "research", "",
                "Looked them up via " + " and ".join(sources) + ": filled in " +
                ", ".join(sorted(filled)) + ".")
            c.commit()

    # Say what is still missing and WHY, per field. The old version led with
    # whatever error happened last - so a lookup that succeeded and simply had
    # nothing to add reported "Google is rejecting the API key", which reads as
    # a failure. Worse, Places has no email field at all, so a working key was
    # never going to fill the one thing that was actually missing.
    still = []
    for col, label in (("phone", "phone"), ("email", "email"), ("address", "address")):
        if col in filled:
            continue
        v = p.get(col)
        if v is None or (isinstance(v, str) and not v.strip()):
            still.append(label)
        elif col == "phone" and not _tidy_phone(str(v)):
            still.append(label)

    explained = []
    if "email" in still:
        explained.append(
            "No email. Google Places doesn't return email addresses - it never has - "
            "so the only sources are their own website or a contact form. "
            + ("Their site doesn't publish one." if p.get("domain")
               else "There's no website on file to read."))
    if "phone" in still or "address" in still:
        want = " and ".join(x for x in ("phone", "address") if x in still)
        explained.append("Still no %s." % want)
    if not still and not filled:
        explained.append("Nothing was missing - everything on this card is already filled in.")
    elif filled:
        explained.append("Filled in %s." % ", ".join(sorted(filled)))

    # A bad API key is a setup problem, not news about this lead. Mention it
    # only when Google could actually have helped with what's missing.
    google_could_help = any(x in still for x in ("phone", "address"))
    keep = [n for n in notes if n and (google_could_help or "API key" not in n)]

    return {"ok": True, "filled": filled, "sources": sources,
            "still_missing": still, "notes": explained + keep,
            "google_key_bad": any("API key" in n for n in notes)}


# Columns a person is allowed to type in by hand on the card.
EDITABLE = {"phone", "email", "address", "city", "company"}


class FieldsBody(BaseModel):
    fields: dict


@app.post("/api/prospect/{pid}/fields")
def set_fields(request: Request, pid: int, body: FieldsBody):
    """Type in what you found yourself. Beats any API."""
    require_auth(request)
    clean = {}
    for k, v in (body.fields or {}).items():
        if k not in EDITABLE:
            raise HTTPException(400, "'%s' is not an editable field." % k)
        val = ("" if v is None else str(v)).strip()[:400]
        if k == "phone" and val:
            val = _tidy_phone(val) or val      # keep what they typed if odd
        if k == "email" and val and "@" not in val:
            raise HTTPException(400, "That does not look like an email address.")
        clean[k] = val
    if not clean:
        raise HTTPException(400, "Nothing to save.")
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM prospects WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "not found")
        cols = ", ".join("%s=?" % k for k in clean)
        c.execute("UPDATE prospects SET %s WHERE id=?" % cols,
                  list(clean.values()) + [pid])
        # The card shows - and every draft writes to - the primary CONTACT's
        # phone and email when one exists, not the prospect row's. Saving only
        # the row meant an owner could correct a bounced address on the card,
        # see "saved", and watch the next draft go to the old one (DJS-2).
        # The value on the card is the value they edited, so it is the value
        # that changes: the contact that supplied it gets the new one too.
        also = []
        for k in ("phone", "email"):
            if k in clean:
                ct = c.execute("SELECT id FROM contacts WHERE prospect_id=? "
                               "AND COALESCE(%s,'')<>'' ORDER BY is_primary DESC, id "
                               "LIMIT 1" % k, (pid,)).fetchone()
                if ct:
                    c.execute("UPDATE contacts SET %s=? WHERE id=?" % k, (clean[k], ct[0]))
                    also.append(k)
        note = "Updated " + ", ".join(sorted(clean)) + " by hand."
        if also:
            note += " (Contact's %s updated to match.)" % " and ".join(also)
        log(c, pid, "edit", "", note)
        c.commit()
    return {"ok": True, "fields": clean, "contact_updated": also}


# ─────────────────────── feedback ───────────────────────
# Two ways in. The typed kind is a suggestion box. The other kind the app
# notices on its own: when a drafted email goes out different to how it was
# written, the difference is the correction - captured without anyone having
# to stop and file it.

def _norm_lines(t):
    return [ln.strip() for ln in (t or "").splitlines() if ln.strip()]


def _line_key(ln):
    return " ".join((ln or "").lstrip("> ").split())


def resolve_covered_edits(c) -> int:
    """An edit notice is handled once every line it cut is on the stopped
    list - the drafts already won't write them. Mark those applied."""
    bans = set(banned_lines())
    done = 0
    for r in c.execute("SELECT id, before_text, after_text FROM feedback "
                       "WHERE state='open' AND kind='edit'").fetchall():
        after = set(_line_key(x) for x in _norm_lines(r["after_text"]))
        cut = [k for k in (_line_key(x) for x in _norm_lines(r["before_text"]))
               if k not in after and len(k) > 25]
        if cut and all(k in bans for k in cut):
            c.execute("UPDATE feedback SET state='applied' WHERE id=?", (r["id"],))
            done += 1
    return done


def diff_summary(before: str, after: str) -> str:
    """Plain English, not a unified diff. What was cut, what was added."""
    b, a = _norm_lines(before), _norm_lines(after)
    bs, as_ = set(b), set(a)
    removed = [ln for ln in b if ln not in as_]
    added = [ln for ln in a if ln not in bs]
    bits = []
    if removed:
        bits.append("cut %d line%s" % (len(removed), "" if len(removed) == 1 else "s"))
    if added:
        bits.append("added %d line%s" % (len(added), "" if len(added) == 1 else "s"))
    if not bits:
        return "reworded without changing any whole line"
    return " and ".join(bits)


class FeedbackBody(BaseModel):
    tab: str = ""
    rating: str = ""              # good | bad | idea
    body: str = ""
    subject_type: str = ""
    subject_id: Optional[int] = None
    label: str = ""


@app.post("/api/feedback")
def add_feedback(request: Request, body: FeedbackBody):
    require_auth(request)
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(400, "Nothing typed yet.")
    if body.rating and body.rating not in ("good", "bad", "idea"):
        raise HTTPException(400, "rating must be good, bad or idea")
    with closing(db()) as c:
        c.execute("INSERT INTO feedback (kind, tab, rating, subject_type, subject_id,"
                  " label, body, created_at) VALUES ('note',?,?,?,?,?,?,?)",
                  (body.tab[:40], body.rating, body.subject_type[:20],
                   body.subject_id, body.label[:160], text[:4000], now()))
        c.commit()
    return {"ok": True}


@app.get("/api/feedback")
def list_feedback(request: Request, state: str = "open", limit: int = 200):
    require_auth(request)
    with closing(db()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM feedback WHERE state=? ORDER BY id DESC LIMIT ?",
            (state, limit)).fetchall()]
        counts = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM feedback GROUP BY state")}
        by_tab = {r["tab"] or "(none)": r["n"] for r in c.execute(
            "SELECT tab, COUNT(*) n FROM feedback WHERE state='open' GROUP BY tab")}
        # What he keeps cutting out of the emails. If a line is removed from
        # most drafts, the line is wrong - that is a code change, not a note.
        # A line quoted in a reply ("> Worth fifteen minutes?") is the same
        # line; and one already stopped is done - it used to stay on the list
        # with a "stopped" tag forever, so the list never cleared (2026-09-27).
        bans = set(banned_lines())
        cut = {}
        for r in c.execute("SELECT before_text, after_text FROM feedback WHERE kind='edit'"):
            after = set(_line_key(x) for x in _norm_lines(r["after_text"]))
            for ln in set(_line_key(x) for x in _norm_lines(r["before_text"])):
                if ln not in after and len(ln) > 25 and ln not in bans:
                    cut[ln] = cut.get(ln, 0) + 1
        patterns = sorted(({"line": k, "cut_from": v} for k, v in cut.items() if v > 1),
                          key=lambda x: -x["cut_from"])[:8]
    return {"feedback": rows, "counts": counts, "by_tab": by_tab, "patterns": patterns,
            "banned": banned_lines()}


class FeedbackState(BaseModel):
    state: str


@app.post("/api/feedback/{fid}")
def set_feedback_state(request: Request, fid: int, body: FeedbackState):
    require_auth(request)
    if body.state not in ("open", "applied", "wontfix"):
        raise HTTPException(400, "state must be open, applied or wontfix")
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM feedback WHERE id=?", (fid,)).fetchone():
            raise HTTPException(404, "not found")
        c.execute("UPDATE feedback SET state=? WHERE id=?", (body.state, fid))
        c.commit()
    return {"ok": True}


# ─────────────── which of the five can we actually sell them ───────────────
# The workbook assigned offers by hand. This does it from evidence: what the
# live site shows, what their own reviews say, and how big they are. Every
# offer carries the reason it was picked, so a claim on a call can be backed
# up rather than asserted.

# Platforms that actually take an order and a cut. Deliberately NOT social or
# directory links - those say nothing about who owns the transaction.
ORDERING_PLATFORMS = {
    "doordash.com": "DoorDash", "order.online": "DoorDash's white-label page",
    "ubereats.com": "Uber Eats", "grubhub.com": "Grubhub",
    "toasttab.com": "Toast", "clover.com": "Clover",
    "square.site": "Square", "squareup.com": "Square",
    "chownow.com": "ChowNow", "slicelife.com": "Slice",
    "menufy.com": "Menufy", "seamless.com": "Seamless",
    "postmates.com": "Postmates", "favordelivery.com": "Favor",
    "olo.com": "Olo", "bentobox": "BentoBox ordering",
}

OFFER_RULES_NOTE = ("Website · Ordering App · AI Vision · Reviews & Rewards · AI Receptionist")


def qualify_one(d: dict) -> tuple:
    """Returns (offers, evidence) where evidence is {offer: why}."""
    ev: dict = {}
    dom = (d.get("domain") or "").strip()
    vertical = d.get("vertical") or "generic"
    rating = float(d.get("rating") or 0)
    reviews = int(d.get("reviews") or 0)
    blob = " ".join(str(d.get(k) or "") for k in ("complaint", "note", "say"))

    site = {}
    html = ""
    if dom:
        try:
            site = analyze(dom, deep=False)
        except Exception as e:
            site = {"ok": False, "error": "%s" % type(e).__name__}
        if site.get("ok"):
            try:
                import httpx
                from grit_analyzer import USER_AGENT
                with httpx.Client(follow_redirects=True, timeout=10,
                                  headers={"User-Agent": USER_AGENT}) as cl:
                    html = cl.get("https://" + dom).text[:400_000]
            except Exception:
                html = ""

    ids = {f.get("id") for f in (site.get("findings") or []) if isinstance(f, dict)}

    # Everything below scores a WEBSITE, because The Watson Factor sells
    # websites. Running it for HomeRepair would stamp property managers with
    # "AI Receptionist" and "Ordering App" - the same leak the say-line had,
    # arriving through qualify instead of find.
    if ws.current() == "homerepair":
        # Daniel, 2026-09-28: "portfolio health score is probably the last
        # thing that they would want." What a property manager buys is less
        # work: tenants send the request with a photo, he taps approve, we
        # dispatch inside his dollar limit - plus the preventive visits, the
        # quarterly report and third-party move-in/move-out records.
        offers = list(HR_OFFERS.get(homerepair_segment(d), HR_OFFERS["property_manager"]))
        why = "Segment: %s. Reachable%s." % (homerepair_segment(d), " by phone" if d.get("phone") else "")
        return (offers, {o: why for o in offers})

    # ── Website ── nothing to point at, or what's there is broken
    if not dom:
        ev["Website"] = "No website at all — invisible when someone searches the trade."
    elif not site.get("ok"):
        err = (site.get("error") or "no response")
        # A dead site and a broken certificate are different sales conversations.
        if re.search(r"certificate|SSL|TLS", err, re.I):
            ev["Website"] = ("Their security certificate is broken — every visitor gets a "
                             "full-page browser warning before they see anything.")
        elif re.search(r"could not connect|name or service|timed out|refused", err, re.I):
            ev["Website"] = ("Their site doesn't load at all right now. Anyone who clicks "
                             "thinks they're closed.")
        else:
            ev["Website"] = ("Their site failed to load when we checked it: %s" % err[:70])

    # ── Ordering App ── they're renting their own customers
    # THIRD_PARTY is a map of "where a business's presence lives" and includes
    # Facebook and Instagram. Using it here produced "their site sends orders to
    # a Facebook page", which is a footer social link, not an ordering platform -
    # and a claim like that on a call gets you corrected by the owner.
    hits = sorted({label for needle, label in ORDERING_PLATFORMS.items()
                   if needle in html.lower()}) if html else []
    if vertical == "restaurant" and hits:
        ev["Ordering App"] = ("Their own site hands ordering to %s — commission on every "
                              "order, and the customer belongs to the platform."
                              % ", ".join(hits[:3]))
    elif vertical == "restaurant" and dom and site.get("ok") and not hits:
        # No known third-party platform detected on their own site either -
        # that's not evidence they're commission-free, it's evidence they have
        # no ordering system of their own to point to. Same fact pattern as
        # "no website": the absence is the finding. This is what actually
        # makes the branded-app pitch fire for restaurants in practice - the
        # explicit-platform-detected case above is rare because most sites
        # don't link out to doordash.com etc. in a way the HTML scan catches.
        ev["Ordering App"] = ("Checked their own site for an ordering system and didn't find "
                              "one - no way to know from here if that's third-party apps doing "
                              "the work or no online ordering at all.")

    # ── AI Receptionist ── the call has nowhere to land
    if re.search(r"unanswered|voicemail|never call|didn.t call|no callback|won.t answer",
                 blob, re.I):
        ev["AI Receptionist"] = "Reviews mention calls going unanswered."
    elif "no_phone" in ids:
        ev["AI Receptionist"] = "No phone number anywhere on the homepage."
    elif {"no_form", "form_only_contact"} & ids:
        ev["AI Receptionist"] = "No contact form — calling is the only way in, so a missed call is a lost job."

    # ── AI Vision ── service happens on camera and it's going wrong
    ON_CAMERA = {"restaurant", "auto", "appointment"}
    if re.search(r"understaff|check-?back|checks dropped|wait|slow service|inattentive|"
                 r"ignored|rude|damage|scratch|dust|scuff", blob, re.I):
        ev["AI Vision"] = "Their own reviews describe service going wrong in the room."
    elif vertical in ON_CAMERA and rating and rating < 4.3 and reviews >= 40:
        ev["AI Vision"] = ("%.1f stars across %d reviews in a business where service happens "
                           "in front of people." % (rating, reviews))

    # ── Reviews & Rewards ── below the line people actually check
    if rating and rating < 4.2 and reviews >= 15:
        ev["Reviews & Rewards"] = ("%.1f stars — under the 4.2 most people won't call below."
                                   % rating)
    elif rating and reviews and reviews < 25:
        ev["Reviews & Rewards"] = ("Only %d reviews. Not enough for anyone to trust the "
                                   "average." % reviews)

    order = ["Website", "Ordering App", "AI Vision", "AI Receptionist", "Reviews & Rewards"]
    offers = [o for o in order if o in ev]
    return offers, ev


class QualifyBody(BaseModel):
    ids: Optional[list] = None
    limit: int = 6


@app.post("/api/find_emails")
def find_emails(request: Request, body: QualifyBody):
    """Go get email addresses for everyone who has a site but no address.

    Google Places has never returned an email - it is not a field. The
    background scan does not look either: it runs analyze(deep=False), which
    scores the homepage and stops. So a freshly swept list has a phone for
    everyone and an email for nobody until something actually reads their
    contact page. This is that something."""
    require_auth(request)
    with closing(db()) as c:
        rows = c.execute(
            "SELECT id, company, domain FROM prospects "
            "WHERE COALESCE(domain,'') <> '' AND COALESCE(email,'') = '' "
            "AND status NOT IN ('booked','dead','cold','corporate') ORDER BY id LIMIT ?",
            (max(1, min(body.limit or 40, 200)),)).fetchall()
    targets = [(r["id"], r["company"], r["domain"]) for r in rows]

    found, blank, problems = [], 0, []
    for pid, company, dom in targets:
        try:
            got, why = scrape_contact(dom)
        except Exception as e:
            problems.append({"company": company, "why": type(e).__name__})
            continue
        email = (got or {}).get("email", "")
        if not email:
            blank += 1
            if why:
                problems.append({"company": company, "why": why})
            continue
        with closing(db()) as c:
            # Never overwrite something a human typed.
            c.execute("UPDATE prospects SET email=? WHERE id=? AND COALESCE(email,'')=''",
                      (email, pid))
            c.commit()
        found.append({"company": company, "email": email})

    return {"checked": len(targets), "found": len(found), "none_listed": blank,
            "emails": found, "problems": problems[:12]}


@app.post("/api/research")
def research_sites(request: Request, body: QualifyBody):
    """Read each prospect's site for what helps you sell them.

    Not "is their website any good" - that is the question the analyzer asks,
    and it is The Watson Factor's question. This asks who runs the company,
    who to ask for, how many doors they manage, what software they already
    run, and whether a tenant has any way to report a problem after 5pm.

    Nothing is guessed. A person is written down only when the page pairs a
    real name with a real title, or when a mailbox is unmistakably a human.
    """
    require_auth(request)
    with closing(db()) as c:
        rows = c.execute(
            "SELECT id, company, domain FROM prospects "
            "WHERE COALESCE(domain,'') <> '' "
            "AND status NOT IN ('booked','dead','cold','corporate') ORDER BY id LIMIT ?",
            (max(1, min(body.limit or 40, 200)),)).fetchall()
    targets = [(r["id"], r["company"], r["domain"]) for r in rows]

    done, problems = [], []
    for pid, company, dom in targets:
        try:
            d = rsrch.research(dom, fetch_page)
        except Exception as e:
            problems.append({"company": company, "why": type(e).__name__})
            continue
        if d.get("why") and not d["emails"] and not d["people"]:
            problems.append({"company": company, "why": d["why"]})
            continue

        sig = d.get("signals") or {}
        gap, say = rsrch.say_line(sig, d["people"])
        best = d["emails"][0] if d["emails"] else ""

        with closing(db()) as c:
            # Blanks only - never overwrite something a human typed.
            if best:
                c.execute("UPDATE prospects SET email=? WHERE id=? AND COALESCE(email,'')=''",
                          (best, pid))
            if gap:
                c.execute("UPDATE prospects SET gap=?, say=? WHERE id=?", (gap, say, pid))

            existing = {(r["name"] or "").lower()
                        for r in c.execute("SELECT name FROM contacts WHERE prospect_id=?", (pid,))}
            added = 0
            for i, p in enumerate(d["people"][:4]):
                if p["name"].lower() in existing:
                    continue
                c.execute("INSERT INTO contacts"
                          "(prospect_id,name,role,email,phone,is_primary,note,added_at) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (pid, p["name"], p.get("role", ""), p.get("email", ""), "",
                           1 if (i == 0 and not existing) else 0,
                           "found on their website", now()))
                added += 1
            c.commit()

        done.append({"company": company, "people": len(d["people"]),
                     "added": added, "email": best,
                     "units": sig.get("units"), "portal": sig.get("portal"),
                     "after_hours": sig.get("after_hours"),
                     "ask_for": d["people"][0]["name"] if d["people"] else "",
                     "pages": d.get("pages_read", 0)})

    with_people = sum(1 for x in done if x["people"])
    with_units  = sum(1 for x in done if x["units"])
    return {"checked": len(targets), "read": len(done),
            "found_people": with_people, "found_units": with_units,
            "results": done, "problems": problems[:12]}


@app.post("/api/qualify")
def qualify(request: Request, body: QualifyBody):
    """Work out what each lead can actually be sold, from live evidence."""
    require_auth(request)
    catalog_ready("qualify leads")
    with closing(db()) as c:
        if body.ids:
            marks = ",".join("?" * len(body.ids))
            rows = c.execute("SELECT * FROM prospects WHERE id IN (%s)" % marks,
                             body.ids).fetchall()
        else:
            # Anything reachable that we haven't proved an offer for yet.
            # Emailable and unqualified first - those are the ones about to be
            # written to, so they're the ones whose offer needs to be right.
            rows = c.execute(
                """SELECT p.* FROM prospects p
                   WHERE p.status NOT IN ('booked','dead','cold','corporate')
                     AND (p.email <> '' OR p.phone <> '')
                     AND COALESCE(p.offer_evidence,'') = ''
                   ORDER BY CASE WHEN p.email <> '' THEN 0 ELSE 1 END,
                            CASE WHEN NOT EXISTS (SELECT 1 FROM outreach o
                                 WHERE o.prospect_id = p.id
                                   AND o.state IN ('sent','approved')) THEN 0 ELSE 1 END,
                            COALESCE(p.tier,1) DESC, COALESCE(p.lead_score,0) DESC
                   LIMIT ?""", (max(1, min(body.limit, 60)),)).fetchall()

    done = []
    for r in rows:
        d = row_to_dict(r)
        offers, ev = qualify_one(d)
        with closing(db()) as c:
            c.execute("UPDATE prospects SET offers=?, offer_evidence=?, qualified_at=? WHERE id=?",
                      (", ".join(offers), json.dumps(ev), now(), d["id"]))
            if offers:
                log(c, d["id"], "note", "",
                    "Qualified: " + "; ".join("%s — %s" % (k, v) for k, v in ev.items()))
            c.commit()
        done.append({"id": d["id"], "company": d.get("company"),
                     "offers": offers, "evidence": ev})
    return {"ok": True, "checked": len(done), "results": done,
            "none_found": [x["company"] for x in done if not x["offers"]]}


# ─────────────── intel: what you found, applied to the lead ───────────────
# The feedback box started as a suggestion box, which was the wrong shape. When
# you go and dig up the Google Maps listing a lead was missing, that is not a
# complaint about the software - it is the missing data, and it should land on
# the record. This reads what you paste and applies whatever it recognises.

_MAPS_RE  = re.compile(r"https?://(?:www\.)?google\.[a-z.]+/maps/[^\s]+", re.I)
_URL_RE   = re.compile(r"https?://[^\s<>\"]+", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_BOUNCE_RE = re.compile(
    r"delivery to the following recipient failed|permanent(?:ly)? fail|"
    r"address not found|user unknown|mailbox (?:unavailable|full)|"
    r"550[- ]|recipient rejected|does not exist|undeliverable", re.I)


def parse_intel(text: str) -> dict:
    """Pull the useful facts out of whatever got pasted."""
    t = text or ""
    maps = _MAPS_RE.search(t)
    emails = [e.lower() for e in _EMAIL_RE.findall(t)]
    phones = []
    for m in _PHONE_RE.finditer(re.sub(r"<[^>]+>", " ", t)):
        p = _tidy_phone(m.group(0))
        if p and p not in phones:
            phones.append(p)
    sites = [u for u in _URL_RE.findall(t)
             if not _MAPS_RE.match(u) and domain_of(u)
             and not re.search(r"google\.[a-z.]+", u, re.I)]
    return {
        "maps_url": maps.group(0) if maps else "",
        "emails": emails,
        "phones": phones,
        "sites": sites,
        "is_bounce": bool(_BOUNCE_RE.search(t)),
        "text": t.strip(),
    }


class IntelBody(BaseModel):
    text: str
    tab: str = ""


@app.post("/api/prospect/{pid}/intel")
def add_intel(request: Request, pid: int, body: IntelBody):
    """Apply what you found to this lead. Returns what it actually did."""
    require_auth(request)
    got = parse_intel(body.text)
    if not got["text"]:
        raise HTTPException(400, "Nothing pasted.")

    did, skipped = [], []
    with closing(db()) as c:
        row = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        p = dict(row)
        sets, args = [], []

        def blank(col):
            v = p.get(col)
            return v is None or (isinstance(v, str) and not v.strip())

        # A bounce is the opposite of new information: it retires an address.
        if got["is_bounce"] and got["emails"]:
            for e in got["emails"]:
                if e == (p.get("email") or "").lower():
                    sets.append("email=?"); args.append("")
                    did.append("cleared %s — it bounced" % e)
                    c.execute("UPDATE outreach SET state='skipped', "
                              "note='address bounced' WHERE prospect_id=? AND to_addr=? "
                              "AND state IN ('draft','approved')", (pid, e))
                    c.execute("UPDATE contacts SET email='' WHERE prospect_id=? AND lower(email)=?",
                              (pid, e))
        else:
            for e in got["emails"]:
                if blank("email"):
                    sets.append("email=?"); args.append(e)
                    did.append("set email to %s" % e)
                    break
                elif e != (p.get("email") or "").lower():
                    skipped.append("already have %s, kept it" % p["email"])
                    break

        if got["maps_url"]:
            sets.append("maps_url=?"); args.append(got["maps_url"][:600])
            did.append("saved their Google Maps listing")
            # The URL carries the coordinates and the place id; keep the id so a
            # future Places lookup hits the right business instead of guessing.
            m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", got["maps_url"], re.I)
            if m and (blank("place_id") or str(p.get("place_id", "")).startswith(("audit:", "wb:"))):
                sets.append("place_id=?"); args.append("cid:" + m.group(1))
                did.append("pinned the exact listing")

        for ph in got["phones"]:
            if blank("phone") or not _tidy_phone(str(p.get("phone") or "")):
                sets.append("phone=?"); args.append(ph)
                did.append("set phone to %s" % ph)
                break

        for u in got["sites"]:
            d = domain_of(u)
            if d and blank("domain"):
                sets.append("domain=?"); args.append(d)
                did.append("set website to %s" % d)
                break

        if sets:
            c.execute("UPDATE prospects SET %s WHERE id=?" % ", ".join(sets), args + [pid])
        # Whatever it couldn't parse is still worth keeping on the record.
        log(c, pid, "note", "", "You added: " + got["text"][:900])
        c.execute("INSERT INTO feedback (kind, tab, rating, subject_type, subject_id,"
                  " label, body, state, created_at)"
                  " VALUES ('intel',?, 'good', 'prospect', ?, ?, ?, 'applied', ?)",
                  (body.tab[:40], pid, p.get("company") or "",
                   (("Applied: " + "; ".join(did)) if did else "Kept as a note — nothing to parse")
                   + "\n\n" + got["text"][:1500], now()))
        c.commit()

    return {"ok": True, "applied": did, "skipped": skipped,
            "parsed": {k: v for k, v in got.items() if k != "text"}}


@app.post("/api/rescan_pending")
def rescan_pending(request: Request, limit: int = 400):
    """Scan everything the import left marked pending.

    The workbook import wrote scan_state='pending' on 230 rows and never queued
    the work, so the Scout Report reported 230 sites 'scanning' forever. Rows
    with no website were never scannable at all - those are now 'no_site',
    which is not a gap in our data, it IS the Website pitch.
    """
    require_auth(request)
    with closing(db()) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM prospects WHERE scan_state='pending' AND domain <> '' "
            "ORDER BY COALESCE(tier,1) DESC, COALESCE(lead_score,0) DESC LIMIT ?",
            (limit,)).fetchall()]
    queue_scans(ids)
    return {"ok": True, "queued": len(ids)}


@app.post("/api/rescan_js_sites")
def rescan_js_sites(request: Request, limit: int = 400):
    """Re-scan prospects graded off an empty JavaScript shell.

    Before the analyzer rendered JS, a React/Vite site scored as 'no phone, no
    H1' because we read the empty page. That pair is the shell's signature, so
    those rows are queued again and graded on the rendered page.
    """
    require_auth(request)
    ids = []
    with closing(db()) as c:
        for r in c.execute("SELECT id, stack FROM prospects WHERE scan_state='done' "
                           "AND domain <> '' LIMIT 5000").fetchall():
            try:
                keys = set((json.loads(r["stack"] or "{}")).get("keys") or [])
            except Exception:
                continue
            if {"no_h1", "no_phone"} <= keys:
                ids.append(r["id"])
            if len(ids) >= limit:
                break
    queue_scans(ids)
    return {"ok": True, "queued": len(ids)}


@app.get("/healthz", include_in_schema=False)
def healthz():
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) n FROM prospects").fetchone()["n"]
    return {"ok": True, "prospects": n}


# ══════════════════════ usage communications ══════════════════════════
# "Executing digital usage communications across email automation and
# in-product communications." Every workspace is a customer; this reads
# what each one has and hasn't used and says one useful thing - a card in
# the product, or an email when they aren't opening the product. The
# decisions are pure and live in usage.py; this section owns the reads,
# the writes and the send.
#
# Storage follows the isolation rule: a customer's messages live in THAT
# customer's database file, next to the data they were built from. The
# switch (off / in-app / in-app + email) and the holdout are account-level
# and live in the primary file, because they are the owner's decision
# about how the product talks - not the customer's.
import usage

USAGE_LOOP_EVERY = 600
_USAGE_STATE: dict = {}          # slug -> {"last_run", "last_error", "last_email"}


def _usage_tables(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS usage_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            play TEXT, channel TEXT,           -- in_app | email
            state TEXT,                        -- active | done | expired | sent | error | holdout
            title TEXT DEFAULT '', body TEXT DEFAULT '',
            cta TEXT DEFAULT '', tab TEXT DEFAULT '',
            to_addr TEXT DEFAULT '', message_id TEXT DEFAULT '', error TEXT DEFAULT '',
            stage TEXT DEFAULT '', adoption INTEGER DEFAULT 0,
            created_at TEXT, shown_at TEXT, sent_at TEXT, clicked_at TEXT,
            dismissed_at TEXT, converted_at TEXT, closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_um_play ON usage_messages(play, channel);
        CREATE INDEX IF NOT EXISTS ix_um_state ON usage_messages(state);
        -- One row per day: the trend line under the adoption score.
        CREATE TABLE IF NOT EXISTS usage_daily (day TEXT PRIMARY KEY, snap TEXT, at TEXT);
    """)


def _primary_setting(k, default=""):
    with closing(db(ws.PRIMARY)) as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _set_primary_setting(k, v):
    with closing(db(ws.PRIMARY)) as c:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, str(v)))
        c.commit()


def usage_mode() -> str:
    m = (_primary_setting("usage_mode", "") or "").strip()
    return m if m in usage.MODES else ""


def usage_holdout() -> int:
    try:
        return max(0, min(50, int(_primary_setting("usage_holdout_pct", "0") or 0)))
    except Exception:
        return 0


def _one(c, sql, args=()):
    try:
        r = c.execute(sql, args).fetchone()
        return (r[0] if r and r[0] is not None else 0)
    except sqlite3.OperationalError:
        return 0


def usage_snapshot(c, slug: str, now_dt: Optional[datetime] = None) -> dict:
    """Everything usage.py decides from, read from this workspace's own file.
    Every figure is a count of something that actually happened - no page
    views, no opens."""
    now_dt = now_dt or datetime.now(timezone.utc)
    d7 = (now_dt - timedelta(days=7)).isoformat()
    d30 = (now_dt - timedelta(days=30)).isoformat()
    s = {"slug": slug}
    for w, since in (("7", d7), ("30", d30)):
        s["scans_" + w] = _one(c, "SELECT COUNT(*) FROM prospects WHERE scanned_at >= ?", (since,))
        s["drafts_" + w] = _one(c, "SELECT COUNT(*) FROM outreach WHERE created_at >= ?", (since,))
        s["sent_" + w] = _one(c, "SELECT COUNT(*) FROM outreach WHERE state='sent' AND sent_at >= ?", (since,))
        s["autopilot_sent_" + w] = _one(c, "SELECT COUNT(*) FROM outreach WHERE state='sent' "
                                           "AND sent_via='autopilot' AND sent_at >= ?", (since,))
        s["replies_" + w] = _one(c, "SELECT COUNT(*) FROM outreach WHERE reply IN "
                                    "('human','positive','negative') AND replied_at >= ?", (since,))
        s["images_" + w] = _one(c, "SELECT COUNT(*) FROM imagegen_log WHERE ok=1 AND at >= ?", (since,))
        s["textbacks_" + w] = _one(c, "SELECT COUNT(*) FROM inbound_calls WHERE texted_at >= ?", (since,))
        s["posts_" + w] = _one(c, "SELECT COUNT(*) FROM social_queue WHERE state='published' "
                                  "AND published_at >= ?", (since,))
    s["manual_sent_30"] = s["sent_30"] - s["autopilot_sent_30"]
    s["scans_total"] = _one(c, "SELECT COUNT(*) FROM prospects WHERE COALESCE(scanned_at,'')<>''")
    s["images_total"] = _one(c, "SELECT COUNT(*) FROM imagegen_log WHERE ok=1")
    s["competitors"] = _one(c, "SELECT COUNT(*) FROM competitors WHERE removed_at IS NULL")
    s["calls_missed_7"] = _one(c, "SELECT COUNT(*) FROM inbound_calls WHERE started_at >= ? "
                                  "AND COALESCE(answered_at,'')=''", (d7,))
    s["drafts_waiting"] = _one(c, "SELECT COUNT(*) FROM outreach WHERE state='draft'")
    oldest = _one(c, "SELECT MIN(created_at) FROM outreach WHERE state='draft'")
    s["oldest_draft_days"] = round(usage.days_since(oldest, now_dt) or 0, 1) if oldest else 0
    s["due_today"] = _one(c, "SELECT COUNT(*) FROM prospects WHERE next_due IS NOT NULL "
                             "AND status NOT IN ('booked','dead','cold','corporate') "
                             "AND date(next_due) <= date('now')")
    s["replies_unworked"] = _one(c, """SELECT COUNT(*) FROM outreach o
        WHERE o.reply IN ('human','positive') AND COALESCE(o.replied_at,'')<>''
          AND NOT EXISTS (SELECT 1 FROM touches t WHERE t.prospect_id=o.prospect_id
                          AND t.at > o.replied_at)""")
    last = [_one(c, q) for q in (
        "SELECT MAX(at) FROM touches",
        "SELECT MAX(sent_at) FROM outreach WHERE state='sent'",
        "SELECT MAX(decided_at) FROM outreach",
        "SELECT MAX(added_at) FROM prospects",
        "SELECT MAX(created_at) FROM social_queue",
        "SELECT MAX(at) FROM imagegen_log")]
    stamps = sorted((str(x) for x in last if x), key=lambda x: usage._dt(x) or now_dt)
    s["last_active_at"] = stamps[-1] if stamps else None
    s["last_seen_at"] = setting("usage_last_seen", "") or None

    host, user, pw = mailbox_creds()
    s["mailbox"] = bool(pw)
    s["autopilot_mode"] = setting("autopilot_mode", "")
    s["telnyx_ready"] = bool(telnyx_settings_ready())
    s["textback_on"] = textback_enabled()
    s["social_connected"] = bool(setting("metricool_token", "").strip()
                                 or setting("fb_page_token", "").strip()
                                 or setting("ig_token", "").strip())
    created = ""
    if slug != ws.PRIMARY:
        try:
            created = _primary_setting_row("SELECT created_at FROM workspace_registry WHERE slug=?", (slug,))
        except Exception:
            created = ""
    age = usage.days_since(created, now_dt) if created else None
    s["age_days"] = None if age is None else round(age, 1)
    return s


def _primary_setting_row(sql, args=()):
    with closing(db(ws.PRIMARY)) as c:
        r = c.execute(sql, args).fetchone()
    return r[0] if r and r[0] else ""


def _usage_history(c) -> list:
    return [dict(r) for r in c.execute("SELECT * FROM usage_messages ORDER BY id")]


def _usage_recipients() -> list:
    """The customer's own logins on this workspace. Account owners are not
    customers; Watson and HomeRepair have none, so they never get email."""
    if setting("usage_email_optout", "") == "1":
        return []
    return sorted(workspace_emails())


def _usage_ctx(now_dt) -> dict:
    """Who the email is from (the platform - the primary workspace's sender)
    and who it's about (this workspace)."""
    me_ = ws.info()
    first = (setting("sender_name", "") or "").strip().split(" ")[0] or "there"
    return {"business": me_["name"], "first_name": first, "now": now_dt,
            "link": public_base() or ("https://" + me_["hosts"][0] if me_["hosts"] else ""),
            "sender": _primary_setting("sender_name", "") or "Just Grit",
            "signoff": (_primary_setting("sender_name", "") or "").strip().split(" ")[0],
            "company": _primary_setting("sender_company", "") or "The Watson Factor",
            "address": _primary_setting("sender_address", "")}


def _usage_email_blockers() -> list:
    out = []
    if usage_mode() != "email":
        out.append("Email is off - in-app only.")
    if not _primary_setting("imap_password", "").strip():
        out.append("The owner mailbox isn't connected (Setup -> Your mailbox on The Watson Factor).")
    if not _primary_setting("sender_address", "").strip():
        out.append("No mailing address on file - the law wants one on every email.")
    return out


def _send_platform_email(to_addr, subject, body, smtp_factory=None, imap_factory=None) -> str:
    host = _primary_setting("imap_host", "").strip()
    user = _primary_setting("imap_user", "").strip() or _primary_setting("sender_email", "").strip()
    pw = _primary_setting("imap_password", "").strip()
    if not (host and user and pw):
        raise mailer.MailError("the owner mailbox isn't connected")
    from_addr = _primary_setting("sender_email", "").strip() or user
    smtp_host = _primary_setting("smtp_host", "").strip() or mailer.smtp_host_for(host)
    smtp_port = int(_primary_setting("smtp_port", "") or mailer.DEFAULT_SMTP_PORT)
    msg = mailer.build(from_addr, _primary_setting("sender_name", ""), to_addr, subject, body)
    mid = mailer.send(smtp_host, smtp_port, user, pw, msg, smtp_factory=smtp_factory)
    try:
        folder = _primary_setting("sent_folder", "").strip()
        if folder:
            mailer.append_sent(host, user, pw, msg, folder, imap_factory=imap_factory)
    except Exception as e:
        print("usage: no Sent copy: %s" % e)
    return mid


def usage_pass(slug: str, now_dt: Optional[datetime] = None, deliver: bool = True,
               emails: bool = True, smtp_factory=None, imap_factory=None) -> dict:
    """One tick for one workspace, with the workspace already set: snapshot,
    credit conversions, retire stale cards, raise at most one card, send at
    most one email."""
    now_dt = now_dt or datetime.now(timezone.utc)
    st = _USAGE_STATE.setdefault(slug, {"last_run": "", "last_error": "", "last_email": ""})
    st["last_run"] = now_dt.isoformat(timespec="seconds")
    out = {"slug": slug, "mode": usage_mode(), "converted": 0, "card": None,
           "email": None, "email_reason": ""}
    stamp = now_dt.isoformat(timespec="seconds")
    with closing(db(slug)) as c:
        _usage_tables(c)
        snap = usage_snapshot(c, slug, now_dt)
        c.execute("INSERT INTO usage_daily(day, snap, at) VALUES(?,?,?) ON CONFLICT(day) "
                  "DO UPDATE SET snap=excluded.snap, at=excluded.at",
                  (now_dt.date().isoformat(), json.dumps(snap, default=str), stamp))
        hist = _usage_history(c)

        # Credit what worked, then close cards whose job is done or whose time is up.
        for mid in usage.goals_met(hist, snap, now_dt):
            c.execute("UPDATE usage_messages SET converted_at=? WHERE id=?", (stamp, mid))
            out["converted"] += 1
        for r in hist:
            if r["channel"] != "in_app" or r["state"] != "active":
                continue
            play = usage.PLAY_BY_ID.get(r["play"])
            done = bool(play and play["goal"] and play["goal"](snap, now_dt))
            old = (usage.days_since(r["created_at"], now_dt) or 0) > usage.IN_APP_TTL_DAYS
            if done or old:
                c.execute("UPDATE usage_messages SET state=?, closed_at=? WHERE id=?",
                          ("done" if done else "expired", stamp, r["id"]))
        c.commit()
        if not out["mode"] or not deliver:
            return out
        hist = _usage_history(c)
        summ = usage.summarize(snap, now_dt)

        play = usage.plan_in_app(snap, hist, now_dt)
        if play:
            card = usage.render_in_app(play, snap)
            hold = usage.in_holdout(slug, play["id"], usage_holdout())
            c.execute("INSERT INTO usage_messages (play, channel, state, title, body, cta, tab, "
                      "stage, adoption, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (play["id"], "in_app", "holdout" if hold else "active", card["title"],
                       card["body"], card["cta"], card["tab"], summ["stage"], summ["adoption"], stamp))
            c.commit()
            out["card"] = {"play": play["id"], "holdout": hold, **card}
            hist = _usage_history(c)

        if not emails:
            return out
        blockers = _usage_email_blockers()
        recips = _usage_recipients()
        local = now_dt.astimezone(workspace_tz())
        if blockers:
            out["email_reason"] = " ".join(blockers)
        elif not recips:
            out["email_reason"] = "No customer logins on this workspace to email."
        elif not mailer.in_send_window(local):
            out["email_reason"] = "Outside sending hours."
        else:
            play, why = usage.plan_email(snap, hist, now_dt)
            out["email_reason"] = why
            if play:
                msg = usage.render_email(play, snap, _usage_ctx(now_dt))
                hold = usage.in_holdout(slug, play["id"], usage_holdout())
                to = recips[0]
                row = c.execute("INSERT INTO usage_messages (play, channel, state, title, body, to_addr, "
                                "stage, adoption, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (play["id"], "email", "holdout" if hold else "queued", msg["subject"],
                                 msg["body"], ", ".join(recips), summ["stage"], summ["adoption"], stamp)).lastrowid
                c.commit()
                if not hold:
                    try:
                        mid = ""
                        for to in recips[:3]:
                            mid = _send_platform_email(to, msg["subject"], msg["body"],
                                                       smtp_factory=smtp_factory, imap_factory=imap_factory)
                        c.execute("UPDATE usage_messages SET state='sent', sent_at=?, message_id=? WHERE id=?",
                                  (stamp, mid, row))
                        st["last_email"] = stamp
                    except Exception as e:
                        c.execute("UPDATE usage_messages SET state='error', error=? WHERE id=?",
                                  (str(e)[:300], row))
                        st["last_error"] = str(e)[:300]
                    c.commit()
                out["email"] = {"play": play["id"], "subject": msg["subject"], "holdout": hold,
                                "to": recips}
    return out


def _usage_loop(every: int = USAGE_LOOP_EVERY):
    while True:
        time.sleep(every)
        for slug in list(ws.WORKSPACES):
            tok = ws.CURRENT.set(slug)
            try:
                usage_pass(slug)
            except Exception as e:
                _USAGE_STATE.setdefault(slug, {})["last_error"] = "%s: %s" % (type(e).__name__, e)
                print("usage [%s]: %s: %s" % (slug, type(e).__name__, e))
            finally:
                ws.CURRENT.reset(tok)


@app.on_event("startup")
def _start_usage_loop():
    if os.environ.get("JUST_GRIT_NO_LOOP"):
        return
    threading.Thread(target=_usage_loop, daemon=True, name="usage-comms").start()


# ── the customer's side: one card ──

@app.get("/api/usage/nudge")
def usage_nudge(request: Request):
    """The card for whoever is looking at this workspace. Opening the app is
    itself a signal (last seen), recorded at most every ten minutes."""
    require_auth(request)
    slug = ws.current()
    now_dt = datetime.now(timezone.utc)
    seen = setting("usage_last_seen", "")
    if not seen or (usage.days_since(seen, now_dt) or 0) * 1440 > 10:
        set_setting("usage_last_seen", now_dt.isoformat(timespec="seconds"))
    if not usage_mode():
        return {"nudge": None}
    with closing(db()) as c:
        _usage_tables(c)
        card = usage.active_in_app(_usage_history(c), now_dt)
    if not card:
        # First visit since the loop last ran: decide now rather than make them wait.
        usage_pass(slug, now_dt, emails=False)       # a page load never sends mail
        with closing(db()) as c:
            card = usage.active_in_app(_usage_history(c), now_dt)
    if not card:
        return {"nudge": None}
    if not card.get("shown_at"):
        with closing(db()) as c:
            c.execute("UPDATE usage_messages SET shown_at=? WHERE id=?",
                      (now_dt.isoformat(timespec="seconds"), card["id"]))
            c.commit()
    return {"nudge": {k: card[k] for k in ("id", "play", "title", "body", "cta", "tab")}}


class NudgeAct(BaseModel):
    action: str = ""


@app.post("/api/usage/nudge/{mid}")
def usage_nudge_act(request: Request, mid: int, body: NudgeAct):
    require_auth(request)
    act = (body.action or "").strip().lower()
    if act not in ("click", "dismiss"):
        raise HTTPException(400, "action must be click or dismiss")
    stamp = now()
    with closing(db()) as c:
        _usage_tables(c)
        r = c.execute("SELECT * FROM usage_messages WHERE id=? AND channel='in_app'", (mid,)).fetchone()
        if not r:
            raise HTTPException(404, "No such card.")
        if act == "click":
            # Clicking is interest, not success - the card stays until the goal is met.
            c.execute("UPDATE usage_messages SET clicked_at=COALESCE(clicked_at, ?) WHERE id=?", (stamp, mid))
        else:
            c.execute("UPDATE usage_messages SET dismissed_at=?, state='dismissed', closed_at=? WHERE id=?",
                      (stamp, stamp, mid))
        c.commit()
    return {"ok": True}


# ── the owner's side: the Adoption tab ──

def _usage_workspace_row(slug, now_dt) -> dict:
    tok = ws.CURRENT.set(slug)
    try:
        with closing(db(slug)) as c:
            _usage_tables(c)
            snap = usage_snapshot(c, slug, now_dt)
            hist = _usage_history(c)
        summ = usage.summarize(snap, now_dt)
        card = usage.active_in_app(hist, now_dt)
        nxt, why = usage.plan_email(snap, hist, now_dt)
        blockers = _usage_email_blockers()
        recips = _usage_recipients()
        eligible = []
        for p in sorted(usage.PLAYS, key=lambda p: p["priority"]):
            ok, reason = usage.eligible(p, snap, [h for h in hist if h["play"] == p["id"]], now_dt)
            eligible.append({"play": p["id"], "label": p["label"], "ok": ok, "reason": reason,
                             "channels": list(p["channels"])})
        return {"slug": slug, "name": ws.info(slug)["name"], "short": ws.info(slug)["short"],
                "accent": ws.info(slug)["accent"], "customer": slug not in ("watson", "homerepair"),
                **summ, "last_seen_at": snap.get("last_seen_at"),
                "snapshot": snap, "card": card and {k: card[k] for k in ("play", "title", "shown_at",
                                                                         "clicked_at", "created_at")},
                "next_email": (nxt["id"] if nxt else None),
                "email_reason": " ".join(blockers) if blockers else
                                ("No customer logins to email." if not recips else why),
                "recipients": recips, "optout": setting("usage_email_optout", "") == "1",
                "plays": eligible, "history": hist[-40:],
                "trend": [dict(r) for r in _usage_trend(slug)]}
    finally:
        ws.CURRENT.reset(tok)


def _usage_trend(slug):
    with closing(db(slug)) as c:
        _usage_tables(c)
        rows = c.execute("SELECT day, snap FROM usage_daily ORDER BY day DESC LIMIT 30").fetchall()
    out = []
    for r in reversed(rows):
        try:
            s = json.loads(r["snap"])
            out.append({"day": r["day"], "adoption": usage.adoption_score(s),
                        "ai_actions_7": usage.ai_actions(s, "7")})
        except Exception:
            continue
    return out


@app.get("/api/usage/overview")
def usage_overview(request: Request):
    require_owner(request)
    now_dt = datetime.now(timezone.utc)
    rows, allmsgs = [], []
    for slug in list(ws.WORKSPACES):
        try:
            w = _usage_workspace_row(slug, now_dt)
        except Exception as e:
            w = {"slug": slug, "name": ws.info(slug)["name"], "error": str(e)}
        rows.append(w)
        for h in w.get("history", []):
            allmsgs.append({**h, "workspace": w.get("short") or slug})
    allmsgs.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return {"mode": usage_mode(), "modes": list(usage.MODES), "holdout_pct": usage_holdout(),
            "email_blockers": _usage_email_blockers(),
            "rules": {"email_fallback_days": usage.EMAIL_FALLBACK_DAYS,
                      "min_email_gap_days": usage.MIN_EMAIL_GAP_DAYS,
                      "max_emails_per_week": usage.MAX_EMAILS_PER_WEEK,
                      "in_app_ttl_days": usage.IN_APP_TTL_DAYS,
                      "conversion_window_days": usage.CONVERSION_WINDOW_DAYS,
                      "min_n_for_lift": usage.MIN_N_FOR_LIFT},
            "plays": [{"id": p["id"], "label": p["label"], "channels": list(p["channels"]),
                       "priority": p["priority"], "has_goal": bool(p["goal"])}
                      for p in sorted(usage.PLAYS, key=lambda p: p["priority"])],
            "workspaces": rows, "funnel": usage.funnel(allmsgs), "recent": allmsgs[:40],
            "state": _USAGE_STATE}


class UsageSettings(BaseModel):
    mode: Optional[str] = None
    holdout_pct: Optional[int] = None
    optout_slug: Optional[str] = None
    optout: Optional[bool] = None


@app.post("/api/usage/settings")
def usage_settings(request: Request, body: UsageSettings):
    require_owner(request)
    if body.mode is not None:
        m = body.mode.strip().lower()
        if m == "off":
            m = ""
        if m not in usage.MODES:
            raise HTTPException(400, "mode must be off, in_app or email")
        _set_primary_setting("usage_mode", m)
    if body.holdout_pct is not None:
        if not 0 <= body.holdout_pct <= 50:
            raise HTTPException(400, "holdout must be 0-50%")
        _set_primary_setting("usage_holdout_pct", body.holdout_pct)
    if body.optout_slug is not None:
        if not ws.exists(body.optout_slug):
            raise HTTPException(404, "No such workspace.")
        tok = ws.CURRENT.set(body.optout_slug)
        try:
            set_setting("usage_email_optout", "1" if body.optout else "")
        finally:
            ws.CURRENT.reset(tok)
    return usage_overview(request)


@app.post("/api/usage/run")
def usage_run(request: Request):
    """One pass over every workspace now. Email still respects the clock."""
    require_owner(request)
    out = []
    for slug in list(ws.WORKSPACES):
        tok = ws.CURRENT.set(slug)
        try:
            out.append(usage_pass(slug))
        except Exception as e:
            out.append({"slug": slug, "error": str(e)})
        finally:
            ws.CURRENT.reset(tok)
    return {"ok": True, "results": out}


@app.get("/api/usage/preview")
def usage_preview(request: Request, slug: str, play: str):
    """Render any play's email and card for any workspace from its real
    numbers, eligible or not - for reading before switching email on."""
    require_owner(request)
    if not ws.exists(slug) or play not in usage.PLAY_BY_ID:
        raise HTTPException(404, "No such workspace or play.")
    p = usage.PLAY_BY_ID[play]
    now_dt = datetime.now(timezone.utc)
    tok = ws.CURRENT.set(slug)
    try:
        with closing(db(slug)) as c:
            _usage_tables(c)
            snap = usage_snapshot(c, slug, now_dt)
        ok, why = usage.eligible(p, snap, [], now_dt)
        return {"play": play, "label": p["label"], "eligible": ok, "reason": why,
                "card": usage.render_in_app(p, snap) if "in_app" in p else None,
                "email": usage.render_email(p, snap, _usage_ctx(now_dt)) if "email" in p else None}
    finally:
        ws.CURRENT.reset(tok)


class UsageTest(BaseModel):
    slug: str
    play: str


@app.post("/api/usage/test_email")
def usage_test_email(request: Request, body: UsageTest):
    """Send one preview to the owner's own address. Never to a customer, never
    recorded as a message, so it can't touch caps or the funnel."""
    email = require_owner(request)
    prev = usage_preview(request, body.slug, body.play)
    if not prev["email"]:
        raise HTTPException(400, "That play has no email.")
    to = email if "@" in (email or "") else _primary_setting("sender_email", "")
    if not to:
        raise HTTPException(400, "No owner address to send the test to.")
    try:
        mid = _send_platform_email(to, "[TEST] " + prev["email"]["subject"], prev["email"]["body"])
    except mailer.MailError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "to": to, "message_id": mid}
