# -*- coding: utf-8 -*-
"""Who the email actually goes to, and whether we can name them.

Half the addresses on this list are shared inboxes, thirteen prospects carry a
scraped page title where the company name should be (five are literally called
"Home"), and thirteen more are franchise locations of one corporate chain
sharing a single domain. None of that is a copy problem - it is who the copy
was pointed at.
"""
import pathlib, sqlite3, sys, tempfile
sys.path.insert(0, '.')
import webapp as W
W.DATA = pathlib.Path(tempfile.mkdtemp(prefix="jg-tgt-")); W.init_db()

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want)
    fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── a person beats a shared inbox ───────────────────────────────────────
contacts = [{"name": "", "email": "info@cafe.com"},
            {"name": "Bobby Ruiz", "email": "bobby@cafe.com"}]
addr, who = W.best_recipient(contacts, {"email": "hello@cafe.com"})
check("a named owner at position 2 beats the generic inbox at position 1",
      addr, "bobby@cafe.com")
check("  and we know whose name it is", who, "Bobby Ruiz")
check("with no named contact, a contact address still beats the business one",
      W.best_recipient([{"name": "", "email": "info@cafe.com"}],
                       {"email": "x@cafe.com"})[0], "info@cafe.com")
check("with no contacts at all, the business address is used",
      W.best_recipient([], {"email": "x@cafe.com"})[0], "x@cafe.com")
check("nothing anywhere is an empty string, not a crash",
      W.best_recipient([], {})[0], "")

# ── shared inboxes get told they're shared inboxes ──────────────────────
d = {"company": "Blanco Cafe", "domain": "blanco.com", "vertical": "restaurant",
     "offers": "AI Vision", "complaint": "inattentive servers, checks dropped without a check-back"}
to_role = W.compose_email(d, [], variant="finding", to_addr="info@blanco.com")
check("writing to info@ with no name asks to be passed along",
      "Point me to whoever" in to_role["body"])
to_person = W.compose_email(d, [{"name": "Bobby Ruiz", "email": "bobby@blanco.com"}],
                            variant="finding", to_addr="bobby@blanco.com")
check("writing to a named person does not",
      "point me to whoever" not in to_person["body"])
check("  and greets them by name", "Hi Bobby" in to_person["body"])
# a known name + a shared inbox is fine - the front desk forwards it
to_both = W.compose_email(d, [{"name": "Bobby Ruiz", "email": ""}],
                          variant="finding", to_addr="info@blanco.com")
check("a known name sent to the front desk still greets the owner",
      "Hi Bobby" in to_both["body"])
check("  and skips the forward ask, because we named someone",
      "point me to whoever" not in to_both["body"])

# ── the company name in the subject is a real name ──────────────────────
check("a scraped page title is cleaned for the subject",
      W.compose_email({"company": "Buttermilk Cafe &#8211; Home", "domain": "b.com"},
                      [], variant="finding")["subject"].startswith("Buttermilk Cafe"))
check("'Home' is not a business name", W.usable_company("Home"), False)
check("'Book Online' is not a business name", W.usable_company("Book Online"), False)
check("a real name passes", W.usable_company("Blanco Cafe"), True)

# ── the draft loop refuses to spend a send on junk ──────────────────────
def draft_once(company, domain, dupes=1):
    c = W.db(); c.row_factory = sqlite3.Row
    c.execute("DELETE FROM prospects"); c.execute("DELETE FROM outreach")
    for i in range(dupes):
        c.execute("INSERT INTO prospects (company,email,domain,status,vertical,opener)"
                  " VALUES (?,?,?,'new','restaurant','your phone number isn''t tap-to-call')",
                  (company, "info@%d.com" % i, domain))
    c.commit()
    rows = c.execute("SELECT * FROM prospects").fetchall()
    out = []
    for r in rows:
        d = W.row_to_dict(r)
        if not W.usable_company(d.get("company") or ""):
            continue
        same = c.execute("SELECT COUNT(*) FROM prospects WHERE domain=?", (d["domain"],)).fetchone()[0]
        if same >= W.CHAIN_DOMAIN_MIN:
            continue
        out.append(d)
    c.close()
    return out

check("a prospect named 'Home' is never drafted", len(draft_once("Home", "h.com")), 0)
check("a normal business is drafted", len(draft_once("Blanco Cafe", "b.com")), 1)
check("13 franchise locations on one corporate domain are all skipped",
      len(draft_once("The Joint Chiropractic", "thejoint.com", dupes=13)), 0)
check("two locations of a local business are NOT treated as a chain",
      len(draft_once("The Wash Tub", "washtub.com", dupes=2)), 2)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
