# -*- coding: utf-8 -*-
"""The owner's voice: learned from their emails, applied to every draft,
and kept current from the edits they make in the Outbox. From the Small
Business plugin's shared voice profile (26 Sep 2026)."""
import os, pathlib, sys, tempfile, json
TMP = tempfile.mkdtemp(prefix="jg-voice-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import voice
import webapp as W, workspaces as ws, inbox

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

RAY = ["Hi Dana,\n\nStraight answer: yes, we can cover all six buildings on one plan. It'd actually be cheaper than the two you have now.\n\nHappy to swing by Thursday and walk through it. Let me know what works.\n\nThanks,\nRay",
       "Hi Tom,\n\nSaw the permit on Kellogg. Congrats.\n\nStraight answer: we do the whole building on one contract. Happy to swing by and look at it with you. No rush on this.\n\nThanks,\nRay",
       "Hi Priya,\n\nQuick one. The filter change is due next week and I'll have a guy near you Tuesday. Let me know what works.\n\nThanks,\nRay\n(830) 555-0100\n\nOn Tue, Priya wrote:\n> can you come look at the unit"]
p = voice.learn(RAY)
check("greeting form, sign-off, name", (p["greeting"], p["signoff"], p["name_line"]), ("Hi {first},", "Thanks,", "Ray"))
check("short sentences, contractions, no exclamations", (p["length"], p["contractions"], p["exclamations"]), ("short", True, "never"))
check("phrases he really uses", "straight answer" in p["phrases"] and "happy to swing by" in " ".join(p["phrases"]))
check("never-uses come from absence", "reach out" in p["never"] and "circle back" in p["never"])
check("quoted reply text is not learned", "come look at the unit" not in " ".join(p["phrases"]))
check("fewer than three samples is refused, not guessed", "error" in voice.learn(RAY[:2]))
dash = voice.learn(["Dana —\n\nYes to all six. Cheaper too.\n\nCheers\nRay", "Tom —\n\nSaw the permit. Nice.\n\nCheers\nRay", "Priya —\n\nFilter's due next week.\n\nCheers\nRay"])
check("a 'Name —' greeting is learned as a form", (dash["greeting"], dash["signoff"]), ("{first} —", "Cheers"))

body = "Hi Ann,\n\nThe number is missing from your home page. On a phone there's no way to call you.\n\nWorth fifteen minutes?\n\nDaniel Watson\nThe Watson Factor\n(830) 555-0100"
out = voice.apply(body, dash, "Ann", "Daniel Watson")
check("apply: greeting takes the owner's form, sign-off goes in before the name",
      (out.startswith("Ann —\n"), "\n\nCheers\nDaniel Watson\n" in out), (True, True))
out2 = voice.apply(body, p, "Ann", "Daniel Watson")
check("  'Hi {first},' form keeps the comma", out2.startswith("Hi Ann,\n"))
team = voice.apply("Hi Blanco Cafe team,\n\nStraight answer.\n\nDaniel Watson", dash, "", "Daniel Watson")
check("  a team greeting is left alone (no first name to put in the form)", team.startswith("Hi Blanco Cafe team,"))
check("  applying twice doesn't double the sign-off", voice.apply(out, dash, "Ann", "Daniel Watson").count("Cheers"), 1)
check("no profile = no change", voice.apply(body, {}, "Ann", "Daniel Watson"), body)
longb = "Hi Ann,\n\n" + " ".join(["word"] * 40) + ".\n\nDaniel Watson"
check("a sentence far longer than he writes is flagged", "40-word sentence" in (voice.long_sentence_warning(longb, p) or ""))
check("  a normal one isn't", voice.long_sentence_warning(body, p), None)

# edits → suggestions
pairs = [("Hi A,\n\nHappy to swing by and see it.\n\nDaniel", "Hi A,\n\nHappy to come by and see it.\n\nDaniel"),
         ("Hi B,\n\nWe can swing by Tuesday.\n\nDaniel", "Hi B,\n\nWe can come by Tuesday.\n\nDaniel"),
         ("Hi D,\n\nI'll swing by Friday.\n\nDaniel", "Hi D,\n\nI'll come by Friday.\n\nDaniel"),
         ("Hi C,\n\nI'd love to help.\n\nDaniel", "Hi C,\n\nHappy to help.\n\nDaniel")]
sug = voice.suggestions(pairs, [], [])
check("a phrase cut three times is suggested; one cut once isn't", [x["phrase"] for x in sug], ["swing by"])
check("  already on never-say → not suggested", voice.suggestions(pairs, ["swing by"], []), [])
check("  dismissed → not suggested", voice.suggestions(pairs, [], ["swing by"]), [])

# through the app
W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set(ws.PRIMARY); W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("sender_name", "Daniel Watson"); W.set_setting("sender_address", "1 Main St")
class Req:
    headers = {}; cookies = {}; query_params = {}
    class client: host = "127.0.0.1"
try:
    W.voice_learn(Req(), W.VoiceLearnBody(samples=RAY[:1])); check("app refuses one sample", False)
except W.HTTPException as e:
    check("app refuses one sample with a sentence", "at least three" in e.detail)
r = W.voice_learn(Req(), W.VoiceLearnBody(samples=RAY))
check("learned and stored", (r["profile"]["greeting"], W.voice_profile()["signoff"]), ("Hi {first},", "Thanks,"))
check("  /api/voice shows the card", "Greeting:" in W.voice_get(Req())["summary"])
d = {"company": "Blanco Cafe", "domain": "blancocafe.com", "vertical": "restaurant", "city": "New Braunfels, TX",
     "opener": "there's no phone number on your homepage", "offer_evidence": "", "first": "Ann"}
e = W.compose_email(d, [{"name": "Ann Carter", "email": "ann@blancocafe.com", "is_primary": 1}], variant="finding", to_addr="ann@blancocafe.com")
v = W.voiced(e["body"])
check("a Watson draft carries the sign-off before the name", "\nThanks,\nDaniel Watson\n" in v)
check("  and passes the slop test still", W.slop.check(e["subject"], v, 1, signature="Daniel Watson", ceiling=W.slop_ceiling(e["offer"], 1)), [])
# mailbox path: fake fetch_sent
inbox.fetch_sent = lambda h, u, pw, since_days=60, limit=120: ("Sent", [{"to": "dana@ridgeline.com", "body": RAY[0]}, {"to": "tom@x.com", "body": RAY[1]},
                                                                          {"to": "priya@y.com", "body": RAY[2]}, {"to": "daniel@thewatsonfactor.com", "body": "note to self"}])
W.set_setting("imap_host", "imap.x"); W.set_setting("imap_user", "daniel@thewatsonfactor.com"); W.set_setting("imap_password", "p")
r = W.voice_learn(Req(), W.VoiceLearnBody(from_mailbox=True))
check("Sent folder: outbound to outsiders only, the note to self skipped", r["profile"]["sources"], "3 sent emails")
# edit suggestions through the app
with W.closing(W.db()) as c:
    c.execute("INSERT INTO prospects (company, domain, added_at) VALUES ('X','x.com',?)", (W.now(),))
    for o, e2 in pairs:
        c.execute("INSERT INTO outreach (prospect_id, offer, subject, body, body_original, state, created_at) VALUES (1,'o','s',?,?,'sent',?)", (e2, o, W.now()))
    c.commit()
s = W.voice_suggestions(Req())
check("the Outbox gets the suggestion", [x["phrase"] for x in s["suggestions"]], ["swing by"])
W.voice_suggest_act(Req(), W.VoiceSuggestBody(phrase="swing by", action="add"))
check("  one tap adds it to never-say", "swing by" in W.setting("never_say"))
check("  and it stops being suggested", W.voice_suggestions(Req())["suggestions"], [])
W.voice_forget(Req())
check("forget clears it", W.voice_profile(), {})
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
