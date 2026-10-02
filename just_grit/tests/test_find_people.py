# -*- coding: utf-8 -*-
"""Find the person, and offer HOAs and commercial buildings.

Daniel, 2026-09-25: 49 HomeRepair emails out, 0 back - 89 of 94 opened
"Hi there" to info@ inboxes. Build the pass that finds a real person,
fix "at no cost", and start prospecting HOAs and commercial buildings.
No network: sites are faked.
"""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-people-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import research as R
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── the name gate ──
for junk in ("and co-founder", "Ramon Co-Owner", "both co-owner", "Revenue Drew", "Corporate Operations",
             "Sited Communities", "Hill Country", "Property Manager", "Read More"):
    check("'%s' is not a person" % junk, R.clean_person_name(junk), "")
for real in ("Kelly Dougherty", "Ann Carter", "Mary J. Lopez", "Jose de la Cruz"):
    check("'%s' is" % real, R.clean_person_name(real), real)

# ── the pass ──
W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
W.set_setting("legal_entity", "HomeRepair Tech LLC"); W.set_setting("sender_address", "1 Main St, New Braunfels TX")
class Req:
    headers = {}; cookies = {}
    class client: host = "127.0.0.1"

SITES = {
    "edwardspm.com": "<html><a href='/about'>About</a><p>Edwards Property Management</p><a href='mailto:leads@edwardspm.com'>x</a></html>",
    "edwardspm.com/about": "<html><h2>Meet the team</h2><p>Sarah Edwards, Broker/Owner</p><p>Contact: sarah@edwardspm.com</p></html>",
    "mistyoaks.org": "<html><p>Misty Oaks Homeowners Association</p><p>Contact the board at board@mistyoaks.org</p></html>",
    "stoneoakoffice.com": "<html><a href='/team'>Our team</a></html>",
    "stoneoakoffice.com/team": "<html><p>Owner: Revenue Drew</p><p>Corporate Operations - Director</p></html>",
}
class Res:
    def __init__(self, html): self.html, self.status, self.robots_allowed, self.error = html, 200, True, ""
def fake_fetch(url):
    key = url.replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
    if key not in SITES:
        raise RuntimeError("no such page")
    return Res(SITES[key])

with W.closing(W.db()) as c:
    for company, dom, cat, email in [("Edwards Property Management, LLC", "edwardspm.com", "property_manager", "leads@edwardspm.com"),
                                     ("Misty Oaks Homeowners Association, Inc.", "mistyoaks.org", "hoa_board", "board@mistyoaks.org"),
                                     ("Stone Oak Office Partners LLC", "stoneoakoffice.com", "commercial", "info@stoneoakoffice.com")]:
        c.execute("INSERT INTO prospects (company, domain, city, category, email, status, added_at) VALUES (?,?,?,?,?,'working',?)",
                  (company, dom, "San Antonio, TX", cat, email, W.now()))
    # the regex's old mistakes, already in the table
    c.execute("INSERT INTO contacts (prospect_id, name, role, note, added_at) VALUES (3,'Revenue Drew','Owner','found on their website',?)", (W.now(),))
    c.execute("INSERT INTO contacts (prospect_id, name, role, note, added_at) VALUES (1,'Owner','','typed by Daniel',?)", (W.now(),))
    c.commit()
    ids = [r[0] for r in c.execute("SELECT id FROM prospects ORDER BY id")]
    for pid in ids:
        d = W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone())
        e = W.compose_email(d, [] if pid != 1 else W.contact_rows(c, 1))
        c.execute("INSERT INTO outreach (prospect_id, offer, variant, subject, body, subject_original, body_original, state, step, created_at) "
                  "VALUES (?,?,?,?,?,?,?,'draft',1,?)", (pid, e["offer"], e["variant"], e["subject"], e["body"], e["subject"], e["body"], W.now()))
    # a step-2 follow-up too, the kind Daniel was looking at
    d = W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=2").fetchone())
    e2 = W.compose_homerepair_followup(d, [], "hoa", None, 2)
    c.execute("INSERT INTO outreach (prospect_id, offer, variant, subject, body, subject_original, body_original, state, step, created_at) "
              "VALUES (2,?,?,?,?,?,?,'draft',2,?)", (e2["offer"], e2["variant"], e2["subject"], e2["body"], e2["subject"], e2["body"], W.now()))
    # one Daniel edited: his words, never touched
    c.execute("UPDATE outreach SET body='Hi there,\n\nmy own words' WHERE prospect_id=3")
    c.commit()
    rows = {r["prospect_id"]: r for r in c.execute("SELECT prospect_id, subject, body, variant FROM outreach WHERE step=1")}
check("before: the company greeting, not 'Hi there'", rows[1]["body"].splitlines()[0], "Hi Edwards Property Management team,")
check("  a board is greeted as a board, by the short name", rows[2]["body"].splitlines()[0], "Hi Misty Oaks board,")
check("  and its subject uses the short name", rows[2]["subject"], "budget season at Misty Oaks")
check("a commercial building gets the commercial sequence", rows[3]["variant"], "seq:commercial:board")
check("  greeted as the company", W.compose_email(W.row_to_dict(c.execute("SELECT * FROM prospects WHERE id=3").fetchone()) if False else {"company": "Stone Oak Office Partners LLC", "category": "commercial"}, [])["body"].splitlines()[0],
      "Hi Stone Oak Office Partners team,")
check("the property-manager email links to the PM page by default", "https://homerepair.tech/pm/" in rows[1]["body"])
check("no email says 'at no cost' or 'free'",
      any(("no cost" in r["body"] or " free" in r["body"].lower()) for r in rows.values()), False)

with W.closing(W.db()) as c:
    st0 = W.people_summary(c)
    cands = [r["company"] for r in W.people_candidates(c)]
check("the Outbox knows how many have no person behind them", (st0["waiting"], st0["hi_there"], st0["hi_team"], st0["with_name"]), (4, 1, 3, 0))
check("candidates are the drafted leads with no clean name", cands,
      ["Edwards Property Management, LLC", "Misty Oaks Homeowners Association, Inc.", "Stone Oak Office Partners LLC"])

st = W.find_people("homerepair", fetch=fake_fetch)
check("the pass runs to the end", (st["step"], st["error"]), ("done", ""))
check("  bad names we found earlier are removed; Daniel's own stay", st["purged"], 1)
with W.closing(W.db()) as c:
    contacts = {r["prospect_id"]: r for r in c.execute("SELECT * FROM contacts WHERE note='found on their website'")}
    rows = {(r["prospect_id"], r["step"]): r for r in c.execute("SELECT prospect_id, step, subject, body FROM outreach")}
    typed = c.execute("SELECT name FROM contacts WHERE prospect_id=1 AND note='typed by Daniel'").fetchone()
    found1 = [(r["name"], r["role"], r["email"]) for r in c.execute("SELECT name, role, email FROM contacts WHERE prospect_id=1 AND note='found on their website'")]
    stone = [r[0] for r in c.execute("SELECT name FROM contacts WHERE prospect_id=3")]
check("a site with no person listed adds nobody", contacts.get(2), None)
check("  Daniel's typed contact is untouched, even though it isn't a name", typed["name"], "Owner")
check("  the team page gives a real person and a direct address", found1, [("Sarah Edwards", "Broker/Owner", "sarah@edwardspm.com")])
check("  and her draft now opens with her name", rows[(1, 1)]["body"].splitlines()[0], "Hi Sarah,")
check("  Stone Oak's junk 'people' were not re-added", stone, [])
check("waiting drafts get the new greeting, including the step-2 follow-up",
      (rows[(2, 1)]["body"].splitlines()[0], rows[(2, 2)]["body"].splitlines()[0]), ("Hi Misty Oaks board,", "Hi Misty Oaks board,"))
check("  the one Daniel edited keeps his words", rows[(3, 1)]["body"], "Hi there,\n\nmy own words")
check("  the report says what happened", (st["checked"], st["named"], st["emails"], st["redrafted"]), (3, 1, 1, 1))

check("a person at another organisation is not our contact",
      (R.someone_elses("claudiajean.ramirez@sanantonio.gov", "hiddenforesthoa.org"),
       R.someone_elses("board@hiddenforesthoa.org", "hiddenforesthoa.org"),
       R.someone_elses("jsilman@satx.rr.com", "stoneoakpoa.org")), (True, False, False))

# ── buyer presets ──
b = W.find_buyers(Req())["buyers"]
check("HomeRepair gets buyer presets for boards, HOA managers, commercial buildings and realtors",
      [x["key"] for x in b], ["hoa_board", "hoa_manager", "property_manager", "commercial_manager", "office", "retail", "medical", "vacation", "brokerage", "agent"])
check("  each carries the category that picks the email sequence",
      {W.homerepair_segment({"category": x["category"]}) for x in b}, {"hoa", "property_manager", "commercial", "realtor"})
e = W.compose_email({"company": "Keller Williams Heritage, LLC", "category": "realtor", "city": "San Antonio"}, [])
check("a brokerage gets the realtor sequence, greeted as the office",
      (e["variant"], e["body"].splitlines()[0]), ("seq:realtor:board", "Hi Keller Williams Heritage team,"))
W.set_setting("realtor_page_url", "https://homerepair.tech/realtors")
e = W.compose_email({"company": "KW", "category": "realtor"}, [])
check("  with the partner page set in Setup, the email links to it", "https://homerepair.tech/realtors" in e["body"])
W.set_setting("realtor_page_url", "")
check("  HOA management companies get the manager email, not the board one", W.homerepair_segment({"category": "hoa_manager"}), "property_manager")
W.places_search = lambda q, key, pages=2, region="us": [{"id": "p_" + q[:6], "displayName": {"text": "Alamo Heights Medical Plaza"},
                                                          "websiteUri": "https://ahmp.example", "businessStatus": "OPERATIONAL", "formattedAddress": "TX"}]
W.google_key = lambda: "k"; W.remaining_requests = lambda: 100; W.queue_scans = lambda ids: None
r = W.find(Req(), W.FindBody(buyer="medical", city="San Antonio, TX"))
with W.closing(W.db()) as c:
    p = c.execute("SELECT company, category FROM prospects WHERE company='Alamo Heights Medical Plaza'").fetchone()
check("finding by preset uses its search words and stamps the category", (r["query"], r["added"], p["category"]), ("medical office building", 1, "commercial"))
t = ws.CURRENT.set("watson")
check("Watson Factor has no presets (its Find box stays as it was)", W.find_buyers(Req())["buyers"], [])
ws.CURRENT.reset(t)

ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
