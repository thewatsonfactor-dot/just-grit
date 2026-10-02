# -*- coding: utf-8 -*-
"""Sending email from the app itself, for autopilot.

Every outreach email so far left through a `mailto:` link and Apple Mail -
Daniel read each one and pressed Send. Autopilot needs the app to send
without him, which means SMTP through the same Namecheap mailbox the
inbox scan already reads (same host, same login: mail.privateemail.com,
port 587 with STARTTLS or 465 with TLS).

Three things this module is careful about:

  * The message is a plain-text, single-part email with a proper Date and
    Message-ID, sent through the customer's own authenticated mailbox. That
    is exactly what a person's mail client produces and is why the spam
    test scored 10/10 - nothing here changes that shape.
  * A copy is APPENDed to the mailbox's Sent folder over IMAP, so the
    email shows up in Mail like every other one and the Sent-folder check
    on the scoreboard still works.
  * Nothing goes out at 2am on a Sunday. `in_send_window` is the one rule
    every caller must pass through.

The network is injected (`smtp_factory`, `imap_factory`) so all of it is
testable with nothing on the wire.
"""
from __future__ import annotations

import email.utils
import imaplib
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage

DEFAULT_SMTP_PORT = 587
SEND_DAYS = {0, 1, 2, 3, 4}          # Monday..Friday
SEND_START_HOUR = 8                  # 8:00 local
SEND_END_HOUR = 17                   # up to 16:59 local


class MailError(RuntimeError):
    pass


def smtp_host_for(imap_host: str) -> str:
    """Namecheap, Google, Zoho, Fastmail all use the same hostname for IMAP
    and SMTP, or one that follows the imap→smtp rename. Good enough as a
    default; Setup can override."""
    h = (imap_host or "").strip().lower()
    if h.startswith("imap."):
        return "smtp." + h[5:]
    return h


def in_send_window(local_dt: datetime, days=SEND_DAYS, start=SEND_START_HOUR,
                   end=SEND_END_HOUR) -> bool:
    return local_dt.weekday() in days and start <= local_dt.hour < end


def build(from_addr: str, from_name: str, to_addr: str, subject: str, body: str,
          in_reply_to: str = "", references: str = "", domain: str = "") -> EmailMessage:
    """A plain-text message shaped the way a mail client shapes one."""
    if not (from_addr and to_addr and subject and body):
        raise MailError("An email needs a from, a to, a subject and a body.")
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((from_name or "", from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=domain or from_addr.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    msg.set_content(body)
    return msg


def send(host: str, port: int, user: str, password: str, msg: EmailMessage,
         smtp_factory=None, timeout: int = 30) -> str:
    """Deliver through the customer's mailbox. Returns the Message-ID.
    587 → STARTTLS; 465 → TLS from the start; anything else is tried as
    STARTTLS-if-offered."""
    if not (host and user and password):
        raise MailError("No mailbox to send through - connect one in Setup.")
    port = int(port or DEFAULT_SMTP_PORT)
    factory = smtp_factory
    if factory is None:
        factory = (lambda h, p: smtplib.SMTP_SSL(h, p, timeout=timeout)) if port == 465 \
            else (lambda h, p: smtplib.SMTP(h, p, timeout=timeout))
    try:
        s = factory(host, port)
    except OSError as e:
        raise MailError("Could not reach %s:%s (%s)." % (host, port, e))
    try:
        s.ehlo()
        if port != 465:
            try:
                s.starttls()
                s.ehlo()
            except smtplib.SMTPNotSupportedError:
                raise MailError("%s:%s does not offer encryption - refusing to send a "
                                "password in the clear." % (host, port))
        try:
            s.login(user, password)
        except smtplib.SMTPAuthenticationError:
            raise MailError("The mailbox refused the login. Check the address and password "
                            "under Setup → Your mailbox.")
        refused = s.send_message(msg)
        if refused:
            raise MailError("The server refused the recipient: %s" % ", ".join(refused))
    except smtplib.SMTPException as e:
        raise MailError("Sending failed: %s" % str(e)[:200])
    finally:
        try:
            s.quit()
        except Exception:
            pass
    return msg["Message-ID"]


def append_sent(host: str, user: str, password: str, msg: EmailMessage,
                folder: str, imap_factory=None, port: int = 993) -> bool:
    """Put a copy in the Sent folder so it shows in Mail. Best effort: a
    failure here must never un-send an email that already went."""
    if not folder:
        return False
    factory = imap_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p))
    try:
        M = factory(host, port)
        try:
            M.login(user, password)
            typ, _ = M.append(_quote(folder), "\\Seen", imaplib.Time2Internaldate(time.time()),
                              msg.as_bytes())
            return typ == "OK"
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except Exception:
        return False


def _quote(folder: str) -> str:
    return '"%s"' % folder.replace("\\", "\\\\").replace('"', '\\"') if " " in folder else folder
