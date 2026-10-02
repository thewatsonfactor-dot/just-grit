# -*- coding: utf-8 -*-
"""The slop test - does a draft read like a bot wrote it?

Daniel, 2026-09-25, handing over Anthropic's Small Business plugin: "use
what would be helpful for Just Grit." Its outreach playbook's slop test
(banned openers, filler, ceilings, em dashes, neat bullets) now runs on
every Outbox draft, live as he edits, plus his own never-say list from
Setup - which also keeps those words out of social posts. No network.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-slop-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import slop
import sequences as S
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

BAD = ("Hi Dana,\n\nI hope this email finds you well! I wanted to reach out because I came across Ridgeline "
       "Property Group and was really impressed by the portfolio you've built in the commercial space.\n\n"
       "At Okonkwo Mechanical, we specialize in providing comprehensive HVAC solutions tailored to the unique "
       "needs of multi-building property managers. We'd love to explore how we might be able to leverage our "
       "expertise to help optimize your facilities operations — truly — and unlock value — for you.\n\n"
       "- one\n- two\n- three\n\n"
       "Would you be open to a brief call? Let me know if you have any questions!\n\nBest regards,\nRay")
GOOD = ("Hi Dana,\n\nSaw you filed for the Kellogg Street building. Congratulations.\n\n"
        "Straight answer on why I'm writing: we cover six other property managers in the area on single "
        "service plans across all their buildings. Usually works out cheaper than per-building contracts.\n\n"
        "Worth 10 minutes Thursday?\n\nThanks, Ray")

w = slop.check("intro", BAD, 1)
check("the playbook's 'before' email fails every check", len(w) >= 5)
check("  the opener", any(w0.startswith('opens with "i hope this email finds you well"') for w0 in w))
check("  the compliment", any("compliment" in w0 for w0 in w))
check("  the filler, named", any('"reach out"' in w0 and '"leverage"' in w0 for w0 in w))
check("  the em dashes", any("em dashes" in w0 for w0 in w))
check("  the three neat bullets", any("three neat bullets" in w0 for w0 in w))
check("the 'after' email passes clean", slop.check("Kellogg Street", GOOD, 1), [])
check("word count ignores the greeting placeholder and the footer",
      slop.word_count("{{greeting}}\n\nOne two three.\n\n—\nHomeRepair Tech LLC\n1 Main St\n\nYou're receiving this because"), 3)
check("a company name in the greeting is not filler",
      slop.check("x", "Hi Team Housing Solutions team,\n\nStraight answer: yes.\n\nThanks, Daniel", 2), [])
check("a follow-up that rambles is told the ceiling",
      any("reads best under 90" in x for x in slop.check("x", "Hi,\n\n" + "word " * 120, 2)))
check("a first email gets more room", slop.check("x", "Hi,\n\n" + "word " * 120, 1), [])
check("the owner's never-say list is enforced",
      slop.check("x", "Hi Ann,\n\nHappy to swing by and knock out the honey-do list.\n\nDaniel", 1, ["honey-do"]),
      ['uses "honey-do" - words you don\'t say out loud.'])
check("an automation disclaimer is flagged", any("automated" in x for x in slop.check("x", "This is an automated response.", 1)))
check("'Re: following up' subjects are flagged", any("subject" in x for x in slop.check("Re: following up", "Hi,\n\nNew fact.", 2)))
check("never_list splits on commas and lines", slop.never_list("circle back, touch base\nleverage\n\n "), ["circle back", "touch base", "leverage"])

# ── every template we ship passes ──
bad = []
seqs = {"pm:" + k: [{"step": 1, "day": 0, **v}] + list(S.B_SHARED) for k, v in S.B1_BY_SIGNAL.items()}
seqs.update({"hoa": S.SEQ_HOA, "commercial": S.SEQ_COMMERCIAL, "realtor": S.SEQ_REALTOR})
for name, steps in seqs.items():
    for st in steps:
        body = st["body"].replace("{{greeting}}", "Hi Misty Oaks board,").replace("{{realtor_ask}}", "Worth a look? Reply and I will set you up.")
        for subj in st["subjects"]:
            w = slop.check(subj.replace("{{city}}", "San Antonio").replace("{{community_short}}", "Misty Oaks"), body, st["step"])
            if w:
                bad.append((name, st["step"], w))
check("every HomeRepair sequence passes the slop test (%d steps)" % sum(len(v) for v in seqs.values()), bad, [])
check("  follow-ups are under 90 words", max(slop.word_count(st["body"]) for v in seqs.values() for st in v if st["step"] in (2, 4)) <= 90)
check("  the day-11 touch asks for nothing", all("?" not in st["body"].replace("{{realtor_ask}}", "") for v in seqs.values() for st in v if st["step"] == 3))
check("  and every sequence has one", all(any(st["step"] == 3 for st in v) for v in seqs.values()))

# ── the Watson Factor side: every variant x offer, realistic finding ──
W.DATA = pathlib.Path(TMP)
wtok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.set_setting("sender_name", "Daniel Watson"); W.set_setting("sender_address", "3217 Wild Iris, New Braunfels, TX 78130")
wbad = []
for offer in W.OFFER_COPY:
    for vert in ("restaurant", "auto", "contractor", "generic"):
        d = {"company": "Blanco Cafe", "domain": "blancocafe.com", "vertical": vert, "city": "New Braunfels, TX",
             "opener": "there's no phone number on your homepage - on a phone, there's no way to call you", "offer_evidence": ""}
        W.pick_pitch = lambda d, o=offer: (o, "I pulled up blancocafe.com the way a customer on a phone would, and " + d["opener"] + ".")
        for v in W.VARIANT_ORDER:
            e = W.compose_email(d, [], variant=v, to_addr="info@blancocafe.com")
            w = slop.check(e["subject"], e["body"], 1, signature="Daniel Watson", ceiling=W.slop_ceiling(offer, 1))
            if w:
                wbad.append((offer, vert, v, w))
check("every Watson Factor first email (5 variants x 5 offers x 4 trades) passes the slop test", wbad, [])
check("  the picture-painting receptionist email gets room for its story, nothing else does",
      (W.slop_ceiling("AI Receptionist", 1), W.slop_ceiling("AI Receptionist", 2), W.slop_ceiling("Website", 1)), (220, 0, 0))
e = W.compose_email({"company": "Blanco Cafe, LLC", "domain": "blancocafe.com", "vertical": "restaurant", "opener": "", "offer_evidence": ""}, [], variant="finding", to_addr="info@blancocafe.com")
check("  no name: greeted as the business, not 'Hi there'", e["body"].startswith("Hi Blanco Cafe team,"))
check("  and the 'I also do' line is one sentence, never bullets", "\n- " in e["body"], False)
ws.CURRENT.reset(wtok)

# ── the routes ──
W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"
W.set_setting("legal_entity", "HomeRepair Tech LLC"); W.set_setting("sender_address", "1 Main St")
with W.closing(W.db()) as c:
    c.execute("INSERT INTO prospects (company, domain, city, category, email, status, added_at) VALUES "
              "('Edwards PM','edwardspm.com','San Antonio, TX','property_manager','leads@edwardspm.com','working',?)", (W.now(),))
    c.execute("INSERT INTO outreach (prospect_id, offer, variant, subject, body, subject_original, body_original, state, step, created_at) "
              "VALUES (1,'x','seq:property_manager:slow_response','Re: following up',?,?,?,'draft',2,?)",
              (BAD, "s", BAD, W.now()))
    c.commit()
rows = W.list_outreach(Req())["outreach"]
check("the Outbox list carries the warnings for each waiting draft", len(rows[0]["slop"]) >= 5)
r = W.outreach_slop(Req(), W.SlopBody(subject="Kellogg Street", body=GOOD, step=1))
check("the live check returns clean for good copy", (r["clean"], r["warnings"]), (True, []))
W.save_settings(Req(), W.Settings(never_say="honey-do, circle back"))
check("the never-say box saves", W.me(Req())["never_say"], "honey-do, circle back")
r = W.outreach_slop(Req(), W.SlopBody(subject="x", body="Hi Ann,\n\nWe can knock out the honey-do list.\n\nDaniel", step=1))
check("  and the live check uses it", r["warnings"], ['uses "honey-do" - words you don\'t say out loud.'])
check("  and so do the social posts (it joins the caption writer's banned words)",
      "honey-do" in W.caption_rules()[1] and "free" in W.caption_rules()[1])
ws.CURRENT.reset(tok)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
