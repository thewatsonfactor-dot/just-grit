# -*- coding: utf-8 -*-
"""A customer's pitch as data instead of Python.

The two built-in businesses keep their copy in code — it was argued over line
by line and there's no reason to move it. Everyone onboarded after them needs
a form, not a developer, or every sale still carries a dev task.

The rule that must survive: offer and proof come from the same evidence, and a
workspace with no offers refuses to draft rather than borrowing someone else's.
"""
import json, os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-cat-")
os.environ["JUST_GRIT_DATA"] = TMP
sys.path.insert(0, '.')
import webapp as W, workspaces as ws
W.DATA = pathlib.Path(TMP); W.init_db()
W.require_auth = lambda r: "t@e.com"
class Req: headers = {}; cookies = {}

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

# ── validation happens on the way IN ────────────────────────────────────
check("an offer missing 'fix' is dropped, not half-saved",
      W.parse_catalog(json.dumps({"offers": {"X": {"tail": "a", "cost": "b"}}})), {"offers": {}})
check("an offer with all three sentences is kept",
      "X" in W.parse_catalog(json.dumps({"offers": {"X": {"tail":"a","cost":"b","fix":"c"}}}))["offers"])
check("garbage JSON is {} rather than a traceback", W.parse_catalog("{not json"), {})
check("a null catalog is {}", W.parse_catalog(None), {})
bad_rx = W.parse_catalog(json.dumps({"offers": {"X": {"tail":"a","cost":"b","fix":"c",
                                                     "evidence":"([unclosed"}}}))
check("a broken regex is dropped but the offer survives",
      "evidence" not in bad_rx["offers"]["X"] and "X" in bad_rx["offers"])
bad_q = W.parse_catalog(json.dumps({"offers": {"X": {"tail":"a","cost":"b","fix":"c",
                                                    "question":"no placeholder"}}}))
check("a question without exactly one %s is dropped",
      "question" not in bad_q["offers"]["X"])

# ── the built-ins are untouched and read-only ───────────────────────────
check("watson still reads its copy from code", W.offer_copy_map() is W.OFFER_COPY)
check("  and its evidence rules from code", W.evidence_rules() is W.EVIDENCE)
# The code copy is a DEFAULT, not a lock: the owner edits his own offers in
# Setup like any customer, and reset() brings the built-in copy back.
W.save_catalog(Req(), W.CatalogBody(catalog={"offers": {"X": {"tail":"a","cost":"b","fix":"c"}}}))
check("editing a built-in's catalog is allowed and takes effect", sorted(W.offer_copy_map()), ["X"])
W.reset_catalog(Req())
check("  and reset restores the code copy", W.offer_copy_map() is W.OFFER_COPY)

# ── a customer workspace drives the whole composer from data ────────────
W.create_workspace(Req(), W.NewWorkspaceBody(slug="chirocare", name="ChiroCare",
                                             product="ChiroCare OS"))
tok = ws.CURRENT.set("chirocare")
try:
    check("a fresh customer has no catalog and refuses to draft", W.has_catalog(), False)
    empty = False
    try:
        W.save_catalog(Req(), W.CatalogBody(catalog={"offers": {}}))
    except Exception as e:
        empty = "No usable offers" in str(e)
    check("  saving an empty catalog is refused", empty, True)

    out = W.save_catalog(Req(), W.CatalogBody(catalog={"offers": {
        "After-Hours Line": {
            "tail": "the calls coming in after you close",
            "cost": "Somebody rear-ended at nine at night books with whoever picks up.",
            "fix": "I put something on your line that answers every time and texts the front desk.",
            "evidence": "voicemail|after.?hours|no one answers|never answers",
            "question": "When somebody calls %s after seven, where does that call end up?",
            "impact": ["answering at nine at night", "the call that would have gone to voicemail"]},
        "Half Written": {"tail": "only a tail"}}}))
    check("a complete offer saves", out["saved"], ["After-Hours Line"])
    check("  and the half-written one is reported as dropped", out["dropped"], ["Half Written"])
    check("the workspace can now draft", W.has_catalog(), True)

    check("the composer reads the customer's copy, not Watson's",
          W.offer_copy_map()["After-Hours Line"][0], "the calls coming in after you close")
    check("  Watson's offers are NOT visible here", "AI Vision" in W.offer_copy_map(), False)
    check("  evidence rules come from the customer's catalog",
          W.evidence_rules(), [("voicemail|after.?hours|no one answers|never answers",
                                "After-Hours Line")])
    check("  a matching complaint selects the customer's offer",
          W.offer_from("reviews say the phone just goes to voicemail"), "After-Hours Line")
    check("  a complaint matching nothing selects nothing", W.offer_from("the parking lot is full"), "")

    # end to end: the email is built entirely from the customer's own words
    d = {"company": "Stamps Chiropractic", "domain": "stampschiropractic.com",
         "vertical": "chiro", "offers": "After-Hours Line",
         "complaint": "called twice and it went straight to voicemail"}
    e = W.compose_email(d, [], variant="finding")
    check("the finished email uses the customer's tail in the subject",
          "the calls coming in after you close" in e["subject"])
    check("  and their fix in the body", "texts the front desk" in e["body"])
    check("  and never leaks Watson's product names",
          not any(p in e["body"] for p in ("AI Vision", "camera", "delivery apps")))
    eq = W.compose_email(d, [], variant="question")
    check("  the question variant uses their question too",
          "after seven" in eq["body"])
finally:
    ws.CURRENT.reset(tok)

check("back on watson, its own copy is intact", W.offer_copy_map() is W.OFFER_COPY)

print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
