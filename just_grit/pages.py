# -*- coding: utf-8 -*-
"""The pages a commercial app has to have: terms, privacy, help, what's
new, status. Plain HTML on the shared tokens, no sign-in needed, one
shell. Legal name and contact come from settings so a customer
workspace on its own hostname shows its own operator later.

The terms and privacy text are a starting draft written for a small
software business in Texas. They are not legal advice; Daniel's lawyer
reads them before a stranger pays.
"""
import html, re

SHELL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s · %(product)s</title><link rel="stylesheet" href="/grit.css">
<style>
  header{padding:16px 22px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--hair)}
  .mark{display:inline-block;border:2px solid var(--grit);border-radius:6px;padding:2px 8px;font-weight:900;font-style:italic;font-size:15px}
  .mark span{color:var(--grit-hot)} header small{color:var(--ink-muted);font-size:var(--t-xs);letter-spacing:.1em;text-transform:uppercase}
  header nav{margin-left:auto;display:flex;gap:14px;flex-wrap:wrap} header nav a{color:var(--ink-muted);text-decoration:none;font-size:var(--t-sm)} header nav a[aria-current]{color:var(--ink)}
  main{max-width:760px;margin:0 auto;padding:32px 16px 80px}
  main h1{margin-bottom:4px} .asof{color:var(--ink-muted);font-size:var(--t-sm);margin:0 0 24px}
  main h2{font-size:var(--t-lg);margin:28px 0 8px} main p,main li{color:var(--ink-2);font-size:var(--t-md)} main li{margin:4px 0}
  main a{color:var(--grit-hot)} .ok{color:var(--good)} .warn{color:var(--warning)} .bad{color:var(--bad)}
  .row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--hair)} .row b{color:var(--ink)}
  footer{padding:14px 22px;font-size:var(--t-xs);color:var(--ink-faint)}
</style></head><body>
<header><a href="/" style="text-decoration:none;color:inherit"><span class="mark">JUST <span>GRIT</span></span></a><small>Sales engine</small>
<nav>%(nav)s</nav></header>
<main>%(body)s</main>
<footer>%(product)s is operated by %(entity)s · %(contact)s</footer>
</body></html>"""

PAGES = [("/help", "Help"), ("/changelog", "What's new"), ("/status", "Status"), ("/terms", "Terms"), ("/privacy", "Privacy")]


def shell(title: str, body: str, path: str, product: str, entity: str, contact: str) -> str:
    nav = " ".join('<a href="%s"%s>%s</a>' % (p, ' aria-current="page"' if p == path else "", html.escape(t)) for p, t in PAGES)
    nav += ' <a href="/login">Sign in</a>'
    return SHELL % {"title": html.escape(title), "product": html.escape(product), "entity": html.escape(entity),
                    "contact": html.escape(contact), "nav": nav, "body": body}


def md(text: str) -> str:
    """The little bit of markdown these pages use: # headings, - lists,
    blank-line paragraphs, **bold**, [text](url)."""
    out, para, lst = [], [], []

    def flush_p():
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para))); para.clear()

    def flush_l():
        if lst:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(x) for x in lst)); lst.clear()

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[(.+?)\]\((https?://[^\s)]+|/[^\s)]*|mailto:[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    for line in text.strip("\n").split("\n"):
        if line.startswith("# "):
            flush_p(); flush_l(); out.append("<h1>%s</h1>" % inline(line[2:]))
        elif line.startswith("## "):
            flush_p(); flush_l(); out.append("<h2>%s</h2>" % inline(line[3:]))
        elif line.startswith("- "):
            flush_p(); lst.append(line[2:])
        elif not line.strip():
            flush_p(); flush_l()
        else:
            flush_l(); para.append(line.strip())
    flush_p(); flush_l()
    return "\n".join(out)


TERMS = """# Terms of service

_Effective {date}. A plain-English draft; the version a lawyer has read replaces it._

## What this is
{product} ("the app") is software operated by {entity} ("we"). It finds local businesses, drafts emails and social posts in your name, sends them when you or a rule you set says so, and keeps track of replies. By opening an account you agree to these terms.

## Your account
You must be 18 and acting for a real business. You are responsible for who you add to your workspace and for what they do in it. Keep your sign-in email under your control; anyone who can read that inbox can sign in.

## What you may send
Every email, text and post goes out under your name and your business. You are the sender. You agree not to use the app to send anything unlawful, deceptive, or to people who have asked you to stop. The app enforces do-not-contact lists, unsubscribe requests and sending windows; you agree not to work around them. You must have the right to use any list you import.

## Third-party services
Emails send through your own mailbox; posts publish through your own Facebook and Instagram; calls and texts go through the phone provider you connect; lead lookups use Google. Their terms apply to that use. We are not responsible for what they do, charge or refuse.

## Paying
Plans are billed monthly in advance. A free trial ends on the date shown when you start it; if you have not cancelled, the plan you chose begins. If a payment fails we retry, then give you a grace period, then make the workspace read-only until it is settled. Cancel any time from Account; the plan runs to the end of the period you paid for. No refunds for partial months.

## Your data
Your leads, drafts, settings and history are yours. You can export them at any time from Setup, and you can delete your workspace, which deletes them. See the [privacy policy](/privacy) for what we keep and why.

## What we promise, and don't
We work to keep the app running and your data safe, and we back it up nightly. We do not promise a particular number of leads, replies or customers. The app is provided as is; to the extent the law allows, our liability to you is limited to what you paid us in the twelve months before the claim. We are not liable for lost profits or indirect losses.

## Ending things
You can close your account at any time. We can suspend an account that breaks these terms or puts other customers at risk, and we will tell you why.

## Changes
We may update these terms; the date at the top changes when we do, and material changes are announced in the app at least 14 days before they apply.

## Law and contact
Texas law applies; disputes are heard in the courts of Comal County, Texas. Questions: {contact}.
"""

PRIVACY = """# Privacy policy

_Effective {date}. A plain-English draft; the version a lawyer has read replaces it._

## What we collect
- **Your account:** your email address and sign-in history.
- **Your business:** what you enter in Setup - name, phone, mailing address, offers, sign-off, words you never say.
- **Leads:** businesses the app finds for you or you import - names, websites, phone numbers, public email addresses, reviews, and what the app noticed about their websites.
- **Messages:** the emails, texts and posts drafted and sent in your name, replies that come back to your connected mailbox, missed-call texts, and notes from your phone receptionist.
- **Connections:** tokens for the mailbox, Facebook and Instagram pages, phone provider and Google key you connect. They are stored to do the job you connected them for and never shown back to you or anyone else.

## Why
To find leads, write and send in your name when you say so, follow up, publish posts, answer your phone, and show you what is working. Nothing is sold, rented or used to train anything.

## Who else sees it
- The services you connect (your mail provider, Meta, Telnyx, Google, your image provider) receive exactly what is needed to send, post, call or look up - the message, the picture, the search.
- Our hosting and backup providers store it encrypted.
- Nobody else, unless the law requires it, in which case we tell you when we are allowed to.

## The people you contact
Leads are businesses, reached at public business addresses and numbers. Anyone who replies stop, unsubscribes or is marked do-not-contact is never written to again from your workspace; that list is kept so the promise holds. A business can ask us to be removed from every workspace by writing to {contact}.

## How long
As long as your account is open. Delete your workspace and its database is removed; nightly backups age out within 30 days. Sign-in records are kept 90 days.

## Your choices
Export everything from Setup at any time. Disconnect any service from Setup. Delete the workspace from Setup. Ask us questions at {contact}.

## Security
Sign-in is by one-time email code; sessions are httponly cookies. Each business has its own database. Secrets you paste are stored server-side and never returned by the app. Backups are nightly and kept 30 days.

## Changes
The date at the top changes when this does; material changes are announced in the app.

{entity}, Texas. {contact}
"""

HELP = """# Help

## The five-minute tour
- **Today** shows what is due: emails waiting for you, replies sorted into call-now / reply-and-book / one-question / not-a-fit, and the Getting Started steps until they're done.
- **Calendar** is everything on one week: posts, emails sent, follow-ups due, texts.
- **Outbox** is where every email waits for you. Approve, edit or skip. Switch autopilot on and it sends inside your sending windows, never twice to the same business inside 48 hours.
- **Leads** is the list: search, sort, tag, import a spreadsheet, export one. "Find more" pulls businesses from Google Maps; "Board view" is the pipeline by stage.
- **Social** builds five posts a week with your logo and phone on every picture, shows what went out and what it did.
- **Setup** is everything about your business, in plain words. Nothing there needs a developer.

## Things people ask
- **Why didn't an email go out?** Look at the draft's line in the Outbox: it says which rule held it (outside the sending window, 48-hour rule, no mailbox, over today's cap, or waiting on you).
- **Why does a draft have a warning?** The slop test flags lines that read like a bot wrote them. It warns; it never rewrites your words.
- **Does any AI write my emails?** No. Emails and texts are written by code from your offers and the facts the app found. AI is used only for pictures, and for captions if you add a key for that.
- **Someone replied "stop".** They go on the do-not-contact list automatically and nothing more is sent from your workspace.
- **How do I get my data out?** Setup → Your data → Export. It's a zip of CSV files you can open in Excel.
- **How do I add a teammate?** Setup → Who can sign in. They sign in with a code sent to that email.

## Getting help
Email {contact}. Say which workspace and, if it's about a message, which business. We answer on business days. The [status page](/status) shows whether anything is down right now.
"""


def render_page(kind: str, product: str, entity: str, contact: str, date: str, extra: str = "") -> str:
    src = {"terms": TERMS, "privacy": PRIVACY, "help": HELP}[kind]
    body = md(src.format(product=product, entity=entity, contact=contact, date=date))
    body = body.replace("<p>_", '<p class="asof">').replace("._</p>", ".</p>")
    return shell({"terms": "Terms", "privacy": "Privacy", "help": "Help"}[kind], body + extra, "/" + kind, product, entity, contact)


def render_changelog(text: str, product: str, entity: str, contact: str) -> str:
    return shell("What's new", md(text), "/changelog", product, entity, contact)


def render_status(h: dict, product: str, entity: str, contact: str) -> str:
    """The public status page from /health's dict. Says what a customer
    needs (is it up, are jobs running, when was the last backup) and
    nothing about how it's built."""
    def row(k, ok, text):
        cls = "ok" if ok is True else "bad" if ok is False else "warn"
        return '<div class="row"><b>%s</b><span class="%s">%s</span></div>' % (html.escape(k), cls, html.escape(text))
    loops = h.get("loops") or {}
    stale = set(h.get("stale") or [])
    names = {"social-queue": "Publishing posts", "social-autopilot": "Building the week's posts",
             "email-autopilot": "Sending approved emails", "outbox-refill": "Refilling the Outbox", "competitor-watch": "Competitor watch"}
    rows = [row("App", bool(h.get("ok")), "Up" if h.get("ok") else "Trouble"),
            row("Database", bool(h.get("db")), "OK" if h.get("db") else "Not answering")]
    for k, label in names.items():
        if k in loops:
            age = loops[k]
            rows.append(row(label, k not in stale, "ran %s ago" % _ago(age)))
    if h.get("backup_age_h") is not None:
        rows.append(row("Last backup", h["backup_age_h"] < 30, "%.0f hours ago" % h["backup_age_h"] if h["backup_age_h"] < 48 else "%.0f days ago" % (h["backup_age_h"] / 24)))
    body = "<h1>Status</h1><p class=\"asof\">Checked just now · version %s</p>%s<p style=\"margin-top:20px\">If something here is red and you're stuck, email %s.</p>" % (
        html.escape(str(h.get("version", ""))), "".join(rows), html.escape(contact))
    return shell("Status", body, "/status", product, entity, contact)


def _ago(sec) -> str:
    sec = int(sec or 0)
    if sec < 90:
        return "%d seconds" % sec
    if sec < 5400:
        return "%d minutes" % round(sec / 60)
    return "%.1f hours" % (sec / 3600)
