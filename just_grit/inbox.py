# -*- coding: utf-8 -*-
"""Reading the inbox, so the scoreboard stops being a guess.

Ninety-four cold emails went out of this system before anything here existed,
and every one of them came back with `reply` empty - not because nobody
answered, but because the only way to record an answer was for a human to
remember to tap a button. `send_health()` duly reported a 0.0% bounce rate on
that, which is worse than no number at all: it is a confident number computed
from a column nobody ever wrote to. A dead `info@` address and a prospect who
simply wasn't interested looked identical, so there was no way to tell a copy
problem from a list problem, and therefore no way to improve either.

This module reads the actual mailbox and matches what comes back to the row
that went out. It classifies into the taxonomy webapp.py already defines
(REPLY_KINDS) rather than inventing a parallel one, and it deliberately
refuses to guess sentiment: a bounce and an out-of-office are machine-
readable facts, but whether a human's two-line answer is "positive" or
"negative" is a judgement call, and a scoreboard fed by a guessing machine
is the same lie in a new place. Those get stored with their text and wait for
one tap.

Nothing here sends, deletes, or moves mail. It opens the mailbox read-only.
"""
import email
import email.policy
import imaplib
import re
from email.utils import parseaddr, parsedate_to_datetime


# ── the quoted-original problem ──────────────────────────────────────────
# Every reply quotes the email it is replying to, and our own footer contains
# the sentence: Reply "stop" and I'll remove you permanently. Classify the raw
# body of a reply and *every single reply* matches the unsubscribe rule,
# because our own words are sitting in it. So the quote comes off first, and
# everything below only ever looks at what the human actually typed.
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^\s*_{5,}\s*$", re.M),
    re.compile(r"^\s*From:\s*.+$", re.I | re.M),
    re.compile(r"^\s*Sent from my \w+", re.I | re.M),
    re.compile(r"^\s*—\s*$", re.M),          # our own footer separator
]


def strip_quoted(body: str) -> str:
    """What the person actually typed, without our email underneath it."""
    text = body or ""
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    text = text[:cut]
    # Drop the ">" quote style too, which does not always carry a marker line.
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


# ── classification ───────────────────────────────────────────────────────
# Order matters and the most machine-certain signals sit at the top, the same
# way EVIDENCE is ordered in webapp.py. A delivery failure is a fact stated by
# a mail server; "not interested" is a human being, and those are not the same
# kind of claim.
_BOUNCE_FROM = re.compile(
    r"^(mailer-daemon|postmaster|mail-daemon|maildaemon|bounce[sd]?-?|"
    r"no-?reply@(.*\.)?(mail|smtp|mx)\.)", re.I)
_BOUNCE_SUBJECT = re.compile(
    r"undeliverable|undelivered mail|delivery (status notification|has failed|failure|"
    r"incomplete)|returned mail|failure notice|mail delivery (failed|subsystem)|"
    r"message not delivered|address not found|recipient (address )?rejected|"
    r"could not be delivered", re.I)
_OOO = re.compile(
    r"out of (the )?office|on (vacation|holiday|leave|pto|annual leave)|"
    r"away from (my|the) (desk|office)|(maternity|paternity|parental) leave|"
    r"will be (back|returning) (on|in)\b|currently (out|away|unavailable)|"
    r"limited access to (my )?email|returning to the office", re.I)
# "Thank you for reaching out" is deliberately NOT in here. A ticketing bot
# says it, but so does the warmest lead in the system ("Thank you for reaching
# out. Yes - can you call me Tuesday?"), and filing that person as a machine
# means no callback, no needs-a-tap, and a follow-up chain that stops dead.
# The bot version always carries a second, machine-certain signal (a case
# number, "we have received your", "do not reply"), and those still match.
_AUTO = re.compile(
    r"automated (response|reply|message)|auto-?(reply|responder|response)|"
    r"do not reply to this|this (mailbox|inbox) is not monitored|"
    r"we('ve| have) received your|your (message|request|inquiry) (has been|was) received|"
    r"(ticket|case) #?\d+", re.I)
# The footer tells them to reply "stop", so a one-word answer is the common
# shape here. Anchored at the start for the bare-word case, plus the phrasings
# people actually use when they mean it.
_UNSUB = re.compile(
    r"\A\s*(stop(?!\s+(by|in|over|at|into)\b)|unsubscribe|remove|remove me|no thanks|no thank you|opt.?out)\b|"
    r"please (remove|unsubscribe|take me off|stop)|"
    r"(do not|don'?t) (contact|email|write|message) (me|us)|"
    r"take (me|us) off|no longer wish|remove (me|us) from", re.I)



# A machine that is not shy about being a machine. A property's leasing bot
# ("Message from Knock Rentals ... Now Leasing! tour our community") carries no
# Auto-Submitted header and none of the ticket-number words above, so it read
# as a person - and was then filed against every email we sent that week.
_ROBOT_FROM = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|leasing_office|notifications?|notify|"
    r"mailer|system|autoresponder|auto-?reply|bounce)", re.I)
_ROBOT_BODY = re.compile(
    r"\bnow leasing\b|tour our community|message from knock|this is an automated|"
    r"please do not reply|do not respond to this|"
    r"if you would like to respond to this notification|"
    r"&nbsp;|&#39;|&amp;|&quot;", re.I)

# The footer says reply "stop", but people write it their own way: "quit
# emailing me", "Hi, stop", "I said stop", "leave us alone", "not interested".
# Missing one is the worst error this module can make - the person is put on
# the call-back list after telling us to go away - so these count too.
# Deliberately narrow on the loose words: "stop by Tuesday" is an invitation.
_UNSUB_LOOSE = re.compile(
    r"\b(stop|quit|cease|quit it with)\s+(the\s+)?(emailing|e-mailing|sending|contacting|messaging|writing|"
    r"texting|calling|spamming|these|this|all this)\b|"
    r"\bleave (me|us) alone\b|\bopt(ed)?[ -]?out\b|\btake (me|us) off\b|"
    r"\b(remove|delete) (me|us|my (email|address|name))\b|"
    r"\bno more (emails?|messages?|e-mails?)\b", re.I)
# A soft "no" only counts when the whole reply is short. "We're not interested
# in a website but the receptionist sounds good" is a yes wearing a no.
_SOFT_NO = re.compile(
    r"\b(not|n't) interested\b|\bnot (a )?(fit|for us|for me)\b|"
    r"\bwe('re| are) all set\b|\bwe don'?t need (this|any|anything)\b", re.I)
_STOP_WORD = re.compile(r"\bstop\b(?!\s+(by|in|over|at|into|the (shop|office|store)))", re.I)


def _stop_in_plain_words(typed: str) -> bool:
    t = re.sub(r"\s+", " ", (typed or "").strip())
    if not t:
        return False
    words = len(t.split())
    if _UNSUB_LOOSE.search(t) and words <= 40:
        return True
    if _SOFT_NO.search(t) and words <= 12:
        return True
    # A very short message with "stop" in it ("Hi, stop", "I said stop") means it.
    return words <= 6 and bool(_STOP_WORD.search(t))


def classify(from_addr: str, subject: str, body: str, headers: dict = None) -> str:
    """Which of webapp.REPLY_KINDS this is - or 'human' for 'ask Daniel'.

    'human' is not a REPLY_KIND on purpose. It is this module declining to
    put a number on the scoreboard that it had to guess at.
    """
    headers = {k.lower(): (v or "") for k, v in (headers or {}).items()}
    frm = (from_addr or "").lower()
    subj = subject or ""
    typed = strip_quoted(body)

    # 1. A mail server saying the address is dead. The most useful signal
    #    there is, because it is a list problem and no amount of better copy
    #    fixes it.
    if _BOUNCE_FROM.search(frm.split("@")[0] + "@" + frm.split("@")[-1]) \
            or _BOUNCE_SUBJECT.search(subj) \
            or "delivery-status" in headers.get("content-type", "").lower():
        return "bounce"

    # 2. RFC 3834: the machine is telling us it is a machine. Believe it
    #    before reading any words.
    auto_sub = headers.get("auto-submitted", "").lower()
    if auto_sub and auto_sub != "no":
        return "out_of_office" if _OOO.search(subj + "\n" + typed) else "auto_reply"
    if headers.get("x-autoreply") or headers.get("x-autorespond") \
            or headers.get("precedence", "").lower() in ("bulk", "auto_reply", "junk"):
        return "out_of_office" if _OOO.search(subj + "\n" + typed) else "auto_reply"

    # 3. Words, now that the quoted original is gone.
    if _OOO.search(subj + "\n" + typed):
        return "out_of_office"
    if _UNSUB.search(typed) or _stop_in_plain_words(typed):
        return "unsubscribe"
    if _AUTO.search(subj + "\n" + typed):
        return "auto_reply"
    if _ROBOT_FROM.search(frm.split("@")[0]) or _ROBOT_BODY.search(subj + "\n" + typed):
        return "auto_reply"

    # 4. A person wrote something. Whether it is a yes or a no is not this
    #    module's call to make.
    return "human"


# ── bounces: whose address actually failed ───────────────────────────────
# The bounce arrives FROM mailer-daemon, so the sender tells us nothing about
# which prospect died. The failed address is inside the report.
_FINAL_RCPT = re.compile(r"^(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*([^\s<>]+@[^\s<>]+)",
                         re.I | re.M)
_ADDR_NEAR_FAIL = re.compile(
    r"(?:failed|undeliverable|unknown|rejected|not found|no such user)[^\n]{0,80}?"
    r"<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?", re.I)
_ANY_ADDR = re.compile(r"<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?")


def bounced_address(raw_text: str, our_own: set = None) -> str:
    """The address that failed, from the delivery report.

    `our_own` is the set of addresses that are us - our sender address turns
    up in every bounce report (it is the original From) and matching on it
    would mark our own mailbox dead.
    """
    ours = {a.lower() for a in (our_own or set())}
    text = raw_text or ""
    for rx in (_FINAL_RCPT, _ADDR_NEAR_FAIL):
        for m in rx.finditer(text):
            a = m.group(1).strip().lower().strip("<>.,;")
            if a and a not in ours and not _BOUNCE_FROM.search(a):
                return a
    for m in _ANY_ADDR.finditer(text):
        a = m.group(1).strip().lower().strip("<>.,;")
        if a and a not in ours and not _BOUNCE_FROM.search(a) \
                and "mailer-daemon" not in a and "postmaster" not in a:
            return a
    return ""


def norm_addr(s: str) -> str:
    """Just the address, lowercased - display names and <> stripped."""
    _, addr = parseaddr(s or "")
    return (addr or "").strip().lower()


def body_text(msg) -> str:
    """The plain-text body, falling back to stripping tags out of the HTML."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return part.get_content()
    except Exception:
        pass
    try:
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            html = part.get_content()
            html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
            return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))
    except Exception:
        pass
    try:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


# ── the one function that touches the network ────────────────────────────
def fetch(host: str, user: str, password: str, since_days: int = 30,
          folder: str = "INBOX", port: int = 993, limit: int = 400) -> list:
    """Recent messages, as plain dicts. Opens the mailbox READ-ONLY.

    Read-only is not politeness - it means this can never mark something
    unread that Daniel already read, or worse, move or delete a real customer
    email because a regex misfired.
    """
    import datetime
    out = []
    M = imaplib.IMAP4_SSL(host, port)
    try:
        M.login(user, password)
        M.select(folder, readonly=True)
        since = (datetime.date.today() - datetime.timedelta(days=max(1, since_days)))
        typ, data = M.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        if typ != "OK":
            return out
        ids = (data[0] or b"").split()[-limit:]
        for i in ids:
            typ, raw = M.fetch(i, "(BODY.PEEK[])")     # PEEK = do not mark read
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            try:
                when = parsedate_to_datetime(msg.get("Date", "")).isoformat()
            except Exception:
                when = ""
            out.append({
                "from": norm_addr(msg.get("From", "")),
                "from_raw": str(msg.get("From", "")),
                # First To recipient. Unused by the inbox scan; the Sent-folder
                # check matches on it (that is the prospect we wrote to).
                "to": norm_addr((str(msg.get("To", "") or "").split(",") or [""])[0]),
                "subject": str(msg.get("Subject", "") or ""),
                "date": when,
                "body": body_text(msg),
                "raw": raw[0][1].decode("utf-8", "replace")[:20000],
                "headers": {k: str(v) for k, v in msg.items()},
            })
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass
    return out


# ── the Sent folder ──────────────────────────────────────────────────────
def list_folders(host: str, user: str, password: str, port: int = 993) -> list:
    """The raw LIST response lines, decoded. sentcheck.find_sent_folder()
    turns them into a folder name; this only fetches."""
    M = imaplib.IMAP4_SSL(host, port)
    try:
        M.login(user, password)
        typ, data = M.list()
        if typ != "OK":
            return []
        out = []
        for item in data or []:
            if isinstance(item, tuple):          # literal-form names come as tuples
                item = b" ".join(p for p in item if isinstance(p, bytes))
            if isinstance(item, bytes):
                out.append(item.decode("utf-8", "replace"))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def fetch_sent(host: str, user: str, password: str, since_days: int = 30,
               port: int = 993, limit: int = 400) -> tuple:
    """(folder_name, messages) from whichever folder the server calls Sent.

    Raises LookupError, not a guess, when no Sent folder can be identified:
    reading the wrong folder would compare every draft against somebody
    else's mail and mark all of them edited.
    """
    from sentcheck import find_sent_folder
    folder = find_sent_folder(list_folders(host, user, password, port))
    if not folder:
        raise LookupError("no Sent folder found on this mailbox")
    # imaplib wants the name quoted when it has spaces or brackets
    quoted = '"%s"' % folder.replace("\\", "\\\\").replace('"', '\\"')
    return folder, fetch(host, user, password, since_days=since_days,
                         folder=quoted, port=port, limit=limit)
