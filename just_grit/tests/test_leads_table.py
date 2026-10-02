# -*- coding: utf-8 -*-
"""The Leads table - one grid over prospects + pipeline + outreach, with
search, sort, tags, bulk actions and a CSV that round-trips."""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-leads-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import webapp as W, workspaces as ws

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
class Req:
    headers = {}; cookies = {}; query_params = {}
    class client: host = "127.0.0.1"
with W.closing(W.db()) as c:
    c.execute("INSERT INTO prospects (company, domain, phone, city, category, email, status, stage, added_at, score) VALUES "
              "('Edwards PM','edwardspm.com','(210) 555-0101','San Antonio, TX','Property management','leads@edwardspm.com','working','working',?,41)", (W.now(),))
    c.execute("INSERT INTO prospects (company, domain, city, category, status, stage, added_at) VALUES "
              "('Misty Oaks HOA','mistyoaks.org','New Braunfels, TX','HOA','new','lead',?)", (W.now(),))
    c.execute("INSERT INTO outreach (prospect_id, offer, subject, body, state, step, created_at, sent_at, to_addr) VALUES (1,'x','s','b','sent',1,?,?, 'leads@edwardspm.com')", (W.now(), W.now()))
    c.commit()

d = W.leads_table(Req())
check("two leads, newest activity first", (d["total"], d["rows"][0]["company"]), (2, "Edwards PM"))
check("  the email count and stage ride along", (d["rows"][0]["emails_sent"], d["rows"][0]["stage"]), (1, "working"))
check("search finds by city", W.leads_table(Req(), q="braunfels")["rows"][0]["company"], "Misty Oaks HOA")
check("stage filter", W.leads_table(Req(), stage="lead")["total"], 1)
check("sort by company ascending", [r["company"] for r in W.leads_table(Req(), sort="company", dir="asc")["rows"]], ["Edwards PM", "Misty Oaks HOA"])
check("stages come with counts", next(s["n"] for s in d["stages"] if s["key"] == "working"), 1)

r = W.leads_bulk(Req(), W.LeadsBulkBody(ids=[1, 2], action="tag", value="Nov callback"))
check("bulk tag", r["n"], 2)
d = W.leads_table(Req(), tag="nov callback")
check("  tag filter is case-insensitive", d["total"], 2)
check("  tags listed with counts", d["tags"], [{"tag": "Nov callback", "n": 2}])
W.leads_bulk(Req(), W.LeadsBulkBody(ids=[2], action="untag", value="nov callback"))
check("  untag", W.leads_table(Req(), tag="Nov callback")["total"], 1)
W.leads_bulk(Req(), W.LeadsBulkBody(ids=[2], action="stage", value="review"))
check("bulk stage move", W.leads_table(Req(), stage="review")["rows"][0]["company"], "Misty Oaks HOA")
try:
    W.leads_bulk(Req(), W.LeadsBulkBody(ids=[], action="tag", value="x")); check("empty pick is refused", False)
except W.HTTPException as e:
    check("empty pick is refused with a sentence", e.detail, "Pick at least one lead first.")

csv_text = W.leads_csv(Req()).body.decode()
check("CSV out has the import's column names", csv_text.splitlines()[0], "company,website,phone,email,city,trade,contact,stage,tags,score,emails_sent,last_activity")
r = W.leads_import(Req(), W.LeadsImportBody(csv_text=csv_text, tag="reimport"))
check("CSV back in matches existing rows, adds none", (r["added"], r["updated"]), (0, 2))
new = "Business,Website,Phone,Email,City,Trade,Contact,Tags\nBlanco Cafe,https://www.blancocafe.com/menu,(830) 555-0100,hi@blancocafe.com,New Braunfels TX,Restaurant,Ann Carter,\"walk-in, hot\"\nEdwards PM,edwardspm.com,,,,,,vip\n,,,,,,,\n"
r = W.leads_import(Req(), W.LeadsImportBody(csv_text=new))
check("a spreadsheet with friendly headers imports", (r["added"], r["updated"], r["skipped"]), (1, 1, 1))
d = W.leads_table(Req(), q="blanco")
row = d["rows"][0]
check("  website cleaned to a domain, tags kept, contact created", (row["domain"], row["tags"], row["contact"]), ("blancocafe.com", ["walk-in", "hot"], "Ann Carter"))
row = W.leads_table(Req(), q="edwards")["rows"][0]
check("  existing phone not overwritten, new tag merged", (row["phone"], "vip" in row["tags"]), ("(210) 555-0101", True))
try:
    W.leads_import(Req(), W.LeadsImportBody(csv_text="phone,email\n1,2\n")); check("no company column refused", False)
except W.HTTPException as e:
    check("no company column is explained", "company" in e.detail)
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
