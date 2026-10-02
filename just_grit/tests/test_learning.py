# -*- coding: utf-8 -*-
"""The system learns, tries different angles, and works the trades you pick.

Daniel: "the system should learn from what's working and what's not, and
try different angles. If there is no response from the first product email
we follow up and offer different products, maybe send a one-sheeter. If
that one guy said stop, they're getting the emails but the content isn't
creating interest. And I should be able to select trades I want to work -
just doctors, or car dealerships."

Four things, each pinned here:
  * Focus: pick trades; Today and the Outbox's NEW emails stick to them;
    replies and follow-ups already in flight still show.
  * Touch 2 pitches a different product; touch 3 links the one-sheet.
  * The lines he keeps cutting can be banned with one tap, and drafts stop
    using them (with a plainer ask if the ask was the casualty).
  * The opener test moves toward whatever gets replies, once there is enough
    data to say.
"""
import json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-learn-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws
from contextlib import closing

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
tok = ws.CURRENT.set(ws.PRIMARY)
W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("sender_name", "Daniel Watson"); W.set_setting("sender_email", "d@x.dev")
W.set_setting("sender_phone", "(830) 715-2300"); W.set_setting("sender_site", "thewatsonfactor.dev")
W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130"); W.set_setting("daily_email_cap", "20")
W.public_base = lambda: "https://justgrit.thewatsonfactor.dev"

def prospect(company, email, domain, **kw):
    with closing(W.db()) as c:
        cols = {"place_id": "pl:" + domain, "company": company, "email": email, "domain": domain,
                "status": "new", "stage": "lead", "added_at": W.now(), "offer_evidence": "x",
                "next_action": "call", "next_due": "2026-09-22"}
        cols.update(kw)
        c.execute("INSERT INTO prospects (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), list(cols.values()))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit()
    return pid

dent = prospect("Bright Smile Dental", "info@brightsmile.com", "brightsmile.com", category="Dental", vertical="appointment")
wash = prospect("Sudsy Car Wash", "info@sudsy.com", "sudsy.com", category="Car Wash / Detail", vertical="auto",
                complaint="Multiple reviews say the wash left scratches")
taco = prospect("Taco Haven", "info@tacohaven.com", "tacohaven.com", category="Full-Service Restaurant", vertical="restaurant",
                complaint="Long waits for a table on weekends", offers="Ordering App, AI Vision")
replier = prospect("Wrote Back Co", "r@wroteback.com", "wroteback.com", category="Roofing", vertical="contractor",
                   status="conversation", next_action=W.CALLBACK_ACTION)

# ── focus ───────────────────────────────────────────────────────────────
f = W.focus_get(Req())
check("focus starts empty - everything", f["trades"], [])
check("  and offers every trade on file with counts",
      sorted((o["key"], o["n"]) for o in f["options"] if o["kind"] == "category"),
      [("Car Wash / Detail", 1), ("Dental", 1), ("Full-Service Restaurant", 1), ("Roofing", 1)])
check("  broad groups too", "auto" in [o["key"] for o in f["options"] if o["kind"] == "vertical"], True)

names = lambda: sorted(r["company"] for r in W.queue(Req())["queue"])
check("with no focus Today shows everyone", names(), ["Bright Smile Dental", "Sudsy Car Wash", "Taco Haven", "Wrote Back Co"])
W.focus_set(Req(), W.FocusBody(trades=["Dental"]))
check("working Dental: Today shows the dentist - and the reply that came in, whatever its trade",
      names(), ["Bright Smile Dental", "Wrote Back Co"])
check("  the queue says what it's working", W.queue(Req())["focus"], ["Dental"])
W.focus_set(Req(), W.FocusBody(trades=["auto"]))
check("a broad group works too", names(), ["Sudsy Car Wash", "Wrote Back Co"])

with closing(W.db()) as c:
    d = W.draft_batch(c); c.commit()
    drafted = [r[0] for r in c.execute("SELECT p.company FROM outreach o JOIN prospects p ON p.id=o.prospect_id WHERE o.state='draft'")]
check("the Outbox only writes to the trade you're working", drafted, ["Sudsy Car Wash"])
check("  and says so", d["focus"], ["auto"])
W.focus_set(Req(), W.FocusBody(trades=[]))
with closing(W.db()) as c:
    W.draft_batch(c); c.commit()
    drafted = sorted(r[0] for r in c.execute("SELECT p.company FROM outreach o JOIN prospects p ON p.id=o.prospect_id WHERE o.state='draft'"))
check("back to everything: the rest get written", drafted, ["Bright Smile Dental", "Sudsy Car Wash", "Taco Haven"])

# ── touch 2 is a different product, touch 3 carries the one-sheet ───────
with closing(W.db()) as c:
    first = dict(c.execute("SELECT * FROM outreach WHERE prospect_id=?", (taco,)).fetchone())
    row = dict(c.execute("SELECT * FROM prospects WHERE id=?", (taco,)).fetchone())
check("the first email to the taco place pitched its best-fit offer", first["offer"] in ("Ordering App", "AI Vision"), True)
two = W.compose_watson_followup(row, [], 2, first["subject"], first["body"], "2026-09-11T10:00:00")
check("touch 2 pitches a DIFFERENT product", (two["offer"] != first["offer"], two["offer"] != ""), (True, True))
check("  says so plainly", "Different idea this time" in two["body"], True)
check("  in that product's own words", W.offer_copy_map()[two["offer"]][2][:40] in two["body"], True)
check("  still quotes the first email underneath", "I wrote:" in two["body"] and "> " in two["body"], True)
three = W.compose_watson_followup(row, [], 3, first["subject"], first["body"], "2026-09-11T10:00:00")
check("touch 3 is the last note with the one-sheet link",
      ("Last note" in three["body"], "https://justgrit.thewatsonfactor.dev/welcome/offers" in three["body"]), (True, True))
one = W.compose_watson_followup(dict(row, offers=""), [], 2, first["subject"], first["body"], "2026-09-11T10:00:00")
with closing(W.db()) as c:
    drow = W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=?", (dent,)).fetchone())
check("a dentist never gets AI Vision pitched as the second idea (no cameras on a floor)",
      W.compose_watson_followup(drow, [], 2, "s", W.offer_copy_map()["Reviews & Rewards"][2], "")["offer"] != "AI Vision", True)
with closing(W.db()) as c:
    wrow = W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=?", (wash,)).fetchone())
check("  a car wash can - the analyzer already sells them cameras",
      W.second_offer(wrow, W.offer_copy_map()["Reviews & Rewards"][2])[0], "AI Vision")

# the one-sheet itself
page = W.one_sheet().body.decode()
import html as _h
check("the one-sheet lists every offer", all(_h.escape(o) in page for o in W.workspace_catalog()["offers"]), True)
check("  with the phone and the name", ("830) 715-2300" in page, "Daniel Watson" in page), (True, True))
check("  and never in a search index", 'name="robots" content="noindex"' in page, True)

# ── lines you keep cutting → stop using them ────────────────────────────
ASK = "Worth fifteen minutes? I'm in New Braunfels and happy to show you. If it's not for you, say so and I'll leave you alone."
with closing(W.db()) as c:
    prow = W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=?", (dent,)).fetchone())
check("the ask is in a fresh draft", ASK in W.compose_email(prow, [], variant="finding", to_addr="info@brightsmile.com")["body"], True)
r = W.ban_line(Req(), W.BanBody(line="> " + ASK))
check("one tap bans the line (quote marks stripped)", r["banned"], [ASK])
check("  the Feedback tab shows it", W.list_feedback(Req())["banned"], [ASK])
with closing(W.db()) as c:
    prow = W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=?", (dent,)).fetchone())
e = W.compose_email(prow, [], variant="finding", to_addr="info@brightsmile.com")
check("drafts stop using the banned line", ASK in e["body"], False)
check("  but the email still asks for something", "reply and I'll send one page" in e["body"], True)
check("  and the legal footer is untouched", "Reply \"stop\"" in e["body"] and "3217 Wild Iris" in e["body"], True)
W.ban_line(Req(), W.BanBody(line=ASK, undo=True))
e = W.compose_email(prow, [], variant="finding", to_addr="info@brightsmile.com")
check("'use it again' puts it back", ASK in e["body"], True)

# ── the opener test learns ──────────────────────────────────────────────
with closing(W.db()) as c:
    c.execute("DELETE FROM outreach")
    for v in W.VARIANT_ORDER:
        for i in range(W.VARIANT_MIN_SAMPLE):
            c.execute("INSERT INTO outreach (prospect_id, offer, to_addr, subject, body, state, created_at, sent_at, variant, step, reply) "
                      "VALUES (?,?,?,?,?,'sent',?,?,?,1,?)", (taco, "o", "a@b.com", "s", "b", W.now(), W.now(), v,
                                                            "positive" if (v == "question" and i < 5) else ""))
    c.commit()
    picks = [W.next_variant(c) for _ in range(200)]
check("once every opener has %d sends, the one that gets replies is picked most" % W.VARIANT_MIN_SAMPLE,
      picks.count("question") > 90, True)
check("  but never exclusively - the test keeps running", len(set(picks)) > 1, True)
with closing(W.db()) as c:
    c.execute("UPDATE outreach SET reply=''"); c.commit()
    picks = {W.next_variant(c) for _ in range(30)}
check("with no replies at all it stays least-sent-first", len(picks), 1)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
