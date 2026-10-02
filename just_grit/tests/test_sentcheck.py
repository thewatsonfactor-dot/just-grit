# -*- coding: utf-8 -*-
"""The scoreboard only ever knew what was DRAFTED. The Outbox hands a draft
to mailto:, Apple Mail sends whatever Daniel turned it into, and the reply
(or the silence) gets filed under a variant whose copy may never have left.
This is the check that closes that gap: read the Sent folder, decide per send
whether the draft or a rewrite went out, and move the rewrites to their own
row. These tests are the decision itself, then the database side of it.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-sentcheck-")
os.environ["JUST_GRIT_DATA"] = TMP
sys.path.insert(0, '.')
import sentcheck as S
import webapp as W, workspaces as ws
from contextlib import closing
W.DATA = pathlib.Path(TMP)

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

DRAFT_S = "Blanco Cafe - something in your reviews"
DRAFT_B = """Daniel here, from The Watson Factor in New Braunfels.

More than one of your reviews says the same thing: "waited 40 minutes and nobody came by." That is not a staffing problem, it is a phone problem - the calls that come in while the floor is slammed go nowhere, and the person who called books somewhere else.

We put a receptionist on the line that answers every call, takes the reservation, and texts you the ones that need a human. Blanco Cafe would have it running in a week.

Worth fifteen minutes to see it?

Daniel Watson
(830) 715-2300
Reply "stop" and I'll remove you permanently."""

# ── 1. wrapping and a signature are not edits ────────────────────────────
def flow(text, width=72):
    """What Apple Mail does to plain text: re-wrap at ~72 columns."""
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append(""); continue
        line = ""
        for w in para.split(" "):
            if len(line) + len(w) + 1 > width:
                out.append(line + " "); line = w
            else:
                line = (line + " " + w).strip()
        out.append(line)
    return "\n".join(out)

sent = flow(DRAFT_B) + "\n\n--\nDaniel Watson\nThe Watson Factor\nthewatsonfactor.dev\n"
v = S.compare(DRAFT_S, DRAFT_B, DRAFT_S, sent)
check("identical text, re-wrapped by the mail client, is 'as drafted'", v["edited"], False)
check("  (every draft word survived)", v["coverage"], 1.0)
check("  (the '-- ' signature block is stripped, nothing counted as inserted)",
      v["inserted"], 0)

# Apple Mail does NOT put a '-- ' separator before a signature by default,
# so the signature is just extra words after the last draft word.
sent = flow(DRAFT_B) + "\n\nDaniel Watson | The Watson Factor | thewatsonfactor.dev | (830) 715-2300\n"
v = S.compare(DRAFT_S, DRAFT_B, DRAFT_S, sent)
check("a bare appended signature is trailing, not an edit", v["edited"], False)
check("  (counted as trailing words, zero inserted)", v["trailing"] > 0 and v["inserted"] == 0, True)

v = S.compare(DRAFT_S, DRAFT_B, DRAFT_S, DRAFT_B)
check("byte-identical is 'as drafted' with the reason saying so", v["reason"], "went out as drafted")

# ── 2. a typo fix is minor, not a rewrite ────────────────────────────────
typo = DRAFT_B.replace("go nowhere", "go unanswered")
v = S.compare(DRAFT_S, DRAFT_B, DRAFT_S, typo)
check("one word swapped is still the variant's copy", v["edited"], False)
check("  (but the change is named)", v["reason"].startswith("minor:"), True)

# ── 3. a rewritten paragraph IS an edit ──────────────────────────────────
rewritten = DRAFT_B.replace(
    "We put a receptionist on the line that answers every call, takes the reservation, "
    "and texts you the ones that need a human. Blanco Cafe would have it running in a week.",
    "I build phone systems for restaurants around here. Mine picks up when your people "
    "can't, books the table, and sends you a text for anything it can't handle. Happy to "
    "show you on a call sometime this week if you're open to it.")
v = S.compare(DRAFT_S, DRAFT_B, DRAFT_S, rewritten)
check("a rewritten middle paragraph is 'edited'", v["edited"], True)
check("  (coverage dropped below the floor)", v["coverage"] < S.MIN_COVERAGE, True)
check("  (words were both cut and added, and the reason says so)",
      "cut" in v["reason"] and "added" in v["reason"], True)

# ── 4. the subject is part of the test cell ─────────────────────────────
v = S.compare(DRAFT_S, DRAFT_B, "Quick question about Blanco Cafe", DRAFT_B)
check("a changed subject alone makes it 'edited' (each variant has its own subject style)",
      v["edited"], True)
v = S.compare(DRAFT_S, DRAFT_B, "Re: " + DRAFT_S, DRAFT_B)
check("  (but a Re:/Fwd: prefix is not a change)", v["edited"], False)

# ── 5. the reader's quoted history is not part of the sent text ─────────
followup = "Did this land with you?\n\nDaniel\n\nOn Sep 12, 2026, Daniel Watson wrote:\n> " + \
           DRAFT_B.replace("\n", "\n> ")
v = S.compare("Re: " + DRAFT_S, "Did this land with you?\n\nDaniel", DRAFT_S, followup)
check("quoted history under a follow-up is stripped before comparing", v["edited"], False)
check("  (strip_history keeps only the top)",
      "reviews" not in S.strip_history(followup).lower(), True)

# ── 6. a whole new email, sharing only the sign-off ──────────────────────
new = "Hey - saw you on Google Maps, wanted to say hi. I do phones for restaurants. " \
      "Free to talk this week?\n\nDaniel Watson\n(830) 715-2300"
v = S.compare(DRAFT_S, DRAFT_B, DRAFT_S, new)
check("a from-scratch email is 'edited'", v["edited"], True)

# ── 7. which send is this? ───────────────────────────────────────────────
rows = [{"id": 1, "sent_at": "2026-09-01T15:00:00+00:00"},
        {"id": 2, "sent_at": "2026-09-08T15:00:00+00:00"},   # the follow-up
        {"id": 3, "sent_at": "2026-09-20T15:00:00+00:00"}]
check("the Sent message on the 8th is the follow-up, not the first email",
      S.pick_nearest(rows, "2026-09-08T15:40:00-05:00")["id"], 2)
check("a message far outside the window matches nothing (no guessing)",
      S.pick_nearest(rows, "2026-08-01T15:00:00+00:00"), None)
check("no parseable Date falls back to the most recent send",
      S.pick_nearest(rows, "")["id"], 3)
check("no candidates -> None", S.pick_nearest([], "2026-09-08T15:00:00+00:00"), None)
check("timezone offsets are honoured (3 days minus a bit still matches)",
      S.pick_nearest(rows, "2026-09-03T14:00:00+00:00")["id"], 1)

# ── 8. finding the Sent folder without guessing ──────────────────────────
gmail = ['(\\HasNoChildren) "/" "INBOX"',
         '(\\HasChildren \\Noselect) "/" "[Gmail]"',
         '(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
         '(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']
check("RFC 6154 \\Sent attribute wins", S.find_sent_folder(gmail), "[Gmail]/Sent Mail")
namecheap = ['(\\HasNoChildren) "." "INBOX"',
             '(\\HasNoChildren) "." "Sent"',
             '(\\HasNoChildren) "." "Drafts"']
check("no attribute: a folder literally named Sent", S.find_sent_folder(namecheap), "Sent")
o365 = ['(\\HasNoChildren) "/" "Inbox"', '(\\HasNoChildren) "/" "Sent Items"']
check("Outlook's 'Sent Items' (name with a space, quoted)",
      S.find_sent_folder(o365), "Sent Items")
check("nothing plausible -> None, not the first folder",
      S.find_sent_folder(['(\\HasNoChildren) "/" "INBOX"', '(\\HasNoChildren) "/" "Archive"']),
      None)
check("a garbage line does not crash the parser", S.parse_list_line("nonsense"), None)

# ── 9. the database side ─────────────────────────────────────────────────
tok = ws.CURRENT.set(ws.PRIMARY)
try:
    W.init_db()
    with closing(W.db()) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(outreach)")]
        check("migration adds copy_check / sent_subject / sent_body",
              all(k in cols for k in ("copy_check", "sent_subject", "sent_body")), True)

        c.execute("INSERT INTO prospects (company, domain, status) VALUES ('Blanco Cafe','blancocafesa.com','new')")
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO prospects (company, domain, status) VALUES ('Nolis','nolisvite.com','new')")
        pid2 = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        def send(pid, to, variant, subj, body, at):
            c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, "
                      "subject_original, body_original, state, created_at, sent_at, variant, step)"
                      " VALUES (?,?,?,?,?,?,?,'sent',?,?,?,1)",
                      (pid, "receptionist", to, subj, body, subj, body, at, at, variant))
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]

        a = send(pid, "owner@blancocafesa.com", "finding", DRAFT_S, DRAFT_B, "2026-09-10T15:00:00+00:00")
        b = send(pid2, "hello@nolisvite.com", "question", "quick question about Nolis", DRAFT_B, "2026-09-11T15:00:00+00:00")
        # one that has ALREADY been judged, by an Outbox edit
        d = send(pid2, "chef@nolisvite.com", "proof", "built this for a company here in town", DRAFT_B, "2026-09-12T15:00:00+00:00")
        c.execute("UPDATE outreach SET copy_check='edited' WHERE id=?", (d,))

        msgs = [
            {"to": "owner@blancocafesa.com", "subject": DRAFT_S, "date": "2026-09-10T15:03:00+00:00",
             "body": flow(DRAFT_B) + "\n\n--\nDaniel Watson\n"},
            {"to": "hello@nolisvite.com", "subject": "quick question about Nolis",
             "date": "2026-09-11T15:02:00+00:00", "body": rewritten},
            {"to": "chef@nolisvite.com", "subject": "built this for a company here in town",
             "date": "2026-09-12T15:02:00+00:00", "body": rewritten},
            {"to": "stranger@example.com", "subject": "lunch?", "date": "2026-09-12T15:02:00+00:00",
             "body": "lunch friday?"},
        ]
        out = W.verify_sent_copy(c, msgs)
        check("two unverified sends were judged", out["checked"], 2)
        check("  one as drafted", out["as_drafted"], 1)
        check("  one edited", out["edited"], 1)
        check("  a Sent message to nobody we wrote to - or to an already-judged row - is "
              "'unmatched', not an error", out["unmatched"], 2)
        check("  an already-judged row is left alone by default",
              c.execute("SELECT copy_check FROM outreach WHERE id=?", (d,)).fetchone()[0], "edited")

        ra = c.execute("SELECT copy_check, sent_body FROM outreach WHERE id=?", (a,)).fetchone()
        check("the as-drafted row is marked as such", ra[0], "as_drafted")
        check("  and what actually left is stored (minus the signature block)",
              "reviews" in ra[1] and "The Watson Factor\n" not in ra[1].split("--")[-1], True)
        rb = c.execute("SELECT copy_check FROM outreach WHERE id=?", (b,)).fetchone()
        check("the rewritten row is marked edited", rb[0], "edited")
        fb = c.execute("SELECT label, body FROM feedback WHERE kind='edit' AND subject_id=?", (b,)).fetchall()
        check("  and gets the same 'edit' feedback the Outbox writes", len(fb), 1)
        check("  labelled with the prospect and offer", "Nolis" in fb[0][0], True)
        check("  saying it was rewritten in the mail app", "mail app" in fb[0][1], True)

        # run it again: nothing new should be written, nothing duplicated
        out2 = W.verify_sent_copy(c, msgs)
        check("a second pass over the same Sent folder judges nothing twice", out2["checked"], 0)
        check("  and does not duplicate the feedback row",
              c.execute("SELECT COUNT(*) FROM feedback WHERE kind='edit' AND subject_id=?", (b,)).fetchone()[0], 1)

        # recheck=True re-judges, still without duplicating feedback
        out3 = W.verify_sent_copy(c, msgs, only_unverified=False)
        check("recheck re-judges every matched row", out3["checked"], 3)
        check("  feedback still not duplicated",
              c.execute("SELECT COUNT(*) FROM feedback WHERE kind='edit' AND subject_id=?", (b,)).fetchone()[0], 1)

        # next_variant no longer credits the rewritten send to its cell
        counts_seen = W.next_variant(c)
        check("next_variant treats the rewritten 'question' send as not sent",
              counts_seen in ("question", "artifact", "impact"), True)
        c.commit()

    # the scoreboard files it under 'yours'
    class Req:
        headers = {}; cookies = {}
        class client: host = "127.0.0.1"
    W.require_auth = lambda r: None
    sb = W.scoreboard(Req())
    by = {r["variant"]: r for r in sb["rows"]}
    check("scoreboard: 'finding' keeps its as-drafted send", by["finding"]["sent"], 1)
    check("scoreboard: 'question' loses the rewritten one", by["question"]["sent"], 0)
    check("scoreboard: 'proof' loses the Outbox-edited one", by["proof"]["sent"], 0)
    check("scoreboard: both rewrites land in 'yours'", sb["yours"]["sent"], 2)
    check("scoreboard: totals are stated", sb["checked"], {"as_drafted": 1, "edited": 2, "unverified": 0})
    check("scoreboard: with nothing unverified the note says so",
          sb["copy_note"].startswith("Every send"), True)

    # ── 10. the Outbox path Daniel actually uses: edit, Approve (opens the
    #        mail app), send there, come back and Mark sent ────────────────
    with closing(W.db()) as c:
        c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, "
                  "subject_original, body_original, state, created_at, variant, step)"
                  " VALUES (?,?,?,?,?,?,?,'draft',?,?,1)",
                  (pid, "receptionist", "gm@blancocafesa.com", DRAFT_S, DRAFT_B, DRAFT_S, DRAFT_B,
                   "2026-09-15T15:00:00+00:00", "artifact"))
        e = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    W.update_outreach(Req(), e, W.OutreachBody(subject=DRAFT_S, body=rewritten, state="approved"))
    with closing(W.db()) as c:
        check("an Outbox edit at Approve time is a known edit right away",
              c.execute("SELECT copy_check FROM outreach WHERE id=?", (e,)).fetchone()[0], "edited")
    W.update_outreach(Req(), e, W.OutreachBody(state="sent"))
    with closing(W.db()) as c:
        r = c.execute("SELECT copy_check, state, sent_body FROM outreach WHERE id=?", (e,)).fetchone()
        check("  and it survives Mark sent", (r[0], r[1]), ("edited", "sent"))
        check("  with the edited text stored as what was sent", "phone systems" in r[2], True)
    sb = W.scoreboard(Req())
    by = {r["variant"]: r for r in sb["rows"]}
    check("  so 'artifact' does not get credited with a send", by["artifact"]["sent"], 0)
    check("  and 'yours' now holds three", sb["yours"]["sent"], 3)

    # an UNEDITED Outbox approve+send stays unverified - the mail app could
    # still have changed it, and only the Sent folder knows
    with closing(W.db()) as c:
        c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, "
                  "subject_original, body_original, state, created_at, variant, step)"
                  " VALUES (?,?,?,?,?,?,?,'draft',?,?,1)",
                  (pid, "receptionist", "bar@blancocafesa.com", DRAFT_S, DRAFT_B, DRAFT_S, DRAFT_B,
                   "2026-09-16T15:00:00+00:00", "impact"))
        f = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    W.update_outreach(Req(), f, W.OutreachBody(subject=DRAFT_S, body=DRAFT_B, state="approved"))
    W.update_outreach(Req(), f, W.OutreachBody(state="sent"))
    with closing(W.db()) as c:
        check("an unedited Outbox send is NOT assumed as-drafted - it stays unverified",
              c.execute("SELECT COALESCE(copy_check,'') FROM outreach WHERE id=?", (f,)).fetchone()[0], "")
    sb = W.scoreboard(Req())
    check("  and the board says one send is unchecked", sb["checked"]["unverified"], 1)
    check("  in that variant's row", {r["variant"]: r["unverified"] for r in sb["rows"]}["impact"], 1)
    check("  with no mailbox connected, the note explains what that costs",
          "Apple Mail" in sb["copy_note"], True)
finally:
    ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
