# -*- coding: utf-8 -*-
"""Reading the mailbox correctly, including the trap that would have made it
useless: our own footer says Reply "stop" and I'll remove you permanently, and
that sentence is quoted inside every single reply. Classify the raw body and
every reply on earth looks like an unsubscribe.

No network. fetch() is the only function here that touches IMAP and it isn't
exercised - everything below is the pure logic it feeds.
"""
import sys
sys.path.insert(0, '.')
import inbox as I

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

OUR_FOOTER = '''
Daniel Watson
The Watson Factor Development
(210) 810-7476 - thewatsonfactor.dev

-
3217 Wild Iris, New Braunfels, TX 78130
Reply "stop" and I'll remove you permanently.
'''

# ── the quoted-original trap ─────────────────────────────────────────────
reply_with_quote = '''Sure, send it over.

On Mon, Sep 14, 2026 at 5:12 PM Daniel Watson <daniel@thewatsonfactor.dev> wrote:
> I build a tool that goes through a local business the way a customer would
> Reply "stop" and I'll remove you permanently.
'''
check("quoted original is stripped off",
      I.strip_quoted(reply_with_quote), "Sure, send it over.")
check("a real reply quoting our footer is NOT an unsubscribe",
      I.classify("drew@gym.com", "Re: two-page check", reply_with_quote), "human")

# ── genuine unsubscribe ──────────────────────────────────────────────────
check("bare 'stop' is an unsubscribe",
      I.classify("owner@cafe.com", "Re: your reviews", "stop\n" + OUR_FOOTER), "unsubscribe")
check("'please remove me' is an unsubscribe",
      I.classify("o@cafe.com", "Re:", "Please remove me from your list."), "unsubscribe")
check("'no thanks' is an unsubscribe",
      I.classify("o@cafe.com", "Re:", "No thanks."), "unsubscribe")

# ── bounces ──────────────────────────────────────────────────────────────
check("mailer-daemon sender is a bounce",
      I.classify("MAILER-DAEMON@mail.privateemail.com", "Undelivered Mail Returned to Sender", "..."),
      "bounce")
check("bounce detected by subject alone",
      I.classify("noreply@somehost.net", "Delivery Status Notification (Failure)", "..."), "bounce")
check("'Address not found' is a bounce",
      I.classify("x@y.com", "Address not found", "..."), "bounce")

dsn = '''Reporting-MTA: dns; mail.privateemail.com

Final-Recipient: rfc822; info@deadrestaurant.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <info@deadrestaurant.com>: Recipient address rejected
Original-From: daniel@thewatsonfactor.dev
'''
check("the failed address is pulled out of the DSN",
      I.bounced_address(dsn, our_own={"daniel@thewatsonfactor.dev"}), "info@deadrestaurant.com")
check("our own sender address is never treated as the bounced one",
      I.bounced_address("Original-From: daniel@thewatsonfactor.dev\nno other address here",
                        our_own={"daniel@thewatsonfactor.dev"}), "")

# ── out of office vs auto-reply: the distinction the scoreboard needs ────
check("out of office is out_of_office, not a reply",
      I.classify("o@clinic.com", "Automatic reply: your email",
                 "I am out of the office until Sept 30 with limited access to email."),
      "out_of_office")
check("Auto-Submitted header alone is enough",
      I.classify("o@clinic.com", "Re: hello", "Thanks!",
                 headers={"Auto-Submitted": "auto-replied"}), "auto_reply")
check("ticketing autoresponder is auto_reply",
      I.classify("support@shop.com", "Re: [Case #88213]",
                 "Thank you for contacting us. We have received your message."), "auto_reply")

# ── the whole point: a human answer is never auto-scored ─────────────────
check("a flat no is 'human', not auto-scored negative",
      I.classify("o@cafe.com", "Re:", "We're not looking for anything right now."), "human")
check("an interested reply is 'human', not auto-scored positive",
      I.classify("o@cafe.com", "Re:", "Interesting - what would this cost?"), "human")
# The courtesy opener a ticketing bot uses is also how real people start a
# reply. The bot always carries a second machine signal; the person never does.
check("'Thank you for reaching out' + a callback ask is 'human'",
      I.classify("jane@gmail.com", "Re: quick question",
                 "Thank you for reaching out. Yes, I'd like to see the write-up - "
                 "can you call me Tuesday?"), "human")
check("'Thank you for your email, but...' is 'human', not a machine",
      I.classify("o@cafe.com", "Re: quick question",
                 "Thank you for your email, but we're not interested."), "human")
check("'Thanks for reaching out' and 'Thank you for reaching out' agree",
      I.classify("o@cafe.com", "Re:", "Thanks for reaching out - what does it cost?"),
      I.classify("o@cafe.com", "Re:", "Thank you for reaching out - what does it cost?"))

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
