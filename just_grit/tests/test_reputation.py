# -*- coding: utf-8 -*-
"""Reputation: the business's own Google listing over time, reviews with
a reply drafted, and review requests by text or email. No network."""
import os, pathlib, sys, tempfile
TMP = tempfile.mkdtemp(prefix="jg-rep-")
os.environ["JUST_GRIT_DATA"] = TMP
os.environ["JUST_GRIT_NO_LOOP"] = "1"
sys.path.insert(0, '.')
import reputation as rep
import webapp as W, workspaces as ws, textback as tb

fails = 0
def check(name, got, want=True):
    global fails
    ok = (got == want); fails += (not ok)
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   got=%r want=%r" % (got, want)))

GOOGLE = {"id": "ChIJabc", "displayName": {"text": "HomeRepair Tech"}, "rating": 4.6, "userRatingCount": 34, "googleMapsUri": "https://maps.google.com/?cid=1",
          "reviews": [
              {"name": "places/ChIJabc/reviews/r1", "rating": 5, "text": {"text": "Showed up on time and fixed the water heater the same day. Highly recommend."}, "authorAttribution": {"displayName": "Ann Carter"}, "publishTime": "2026-09-20T10:00:00Z", "relativePublishTimeDescription": "a week ago"},
              {"name": "places/ChIJabc/reviews/r2", "rating": 2, "text": {"text": "Tech was late and left a mess in the garage. Still waiting on a call back."}, "authorAttribution": {"displayName": "Bob"}, "publishTime": "2026-09-18T10:00:00Z", "relativePublishTimeDescription": "a week ago"},
              {"name": "places/ChIJabc/reviews/r3", "rating": 3, "text": {"text": "Fine."}, "authorAttribution": {"displayName": "C"}, "publishTime": "2026-09-01T10:00:00Z", "relativePublishTimeDescription": "3 weeks ago"},
          ]}
L = rep.parse_listing(GOOGLE)
check("listing parsed", (L["name"], L["rating"], L["count"], len(L["reviews"])), ("HomeRepair Tech", 4.6, 34, 3))
check("the review link is built from the place id", L["review_url"], "https://search.google.com/local/writereview?placeid=ChIJabc")
check("sentiment: stars first", [rep.sentiment(r["rating"], r["text"]) for r in L["reviews"]], ["positive", "negative", "neutral"])
check("  four stars with two complaints reads negative", rep.sentiment(4, "rude and late, never again"), "negative")
r1 = dict(L["reviews"][0], sentiment="positive"); r2 = dict(L["reviews"][1], sentiment="negative")
d1 = rep.reply_draft(r1, "HomeRepair Tech", "Daniel", "(830) 205-0202")
d2 = rep.reply_draft(r2, "HomeRepair Tech", "Daniel", "(830) 205-0202")
check("positive reply thanks by first name and quotes them", ("Thanks, Ann." in d1, "water heater" in d1, d1.endswith("- Daniel, HomeRepair Tech")), (True, True, True))
check("negative reply apologises, names what they said, gives the number", ("sorry" in d2, "left a mess" in d2, "(830) 205-0202" in d2), (True, True, True))
check("  no bot words in either", all(w not in (d1 + d2).lower() for w in ["reach out", "we apologize for any inconvenience", "valued customer"]))
t = rep.request_text("Ann Carter", "HomeRepair Tech", "Daniel", L["review_url"])
check("request text: name, sender, link, under 200 chars", ("Hi Ann," in t, "Daniel from" in t, L["review_url"] in t, len(t) < 200), (True, True, True, True))
subj, body = rep.request_email("", "HomeRepair Tech", "Daniel", L["review_url"])
check("request email works with no name", (body.startswith("Hi,"), L["review_url"] in body, "reply to this email" in body), (True, True, True))
check("trend deltas", rep.trend([{"at": "2026-08-01T00:00:00", "rating": 4.4, "count": 30}, {"at": "2026-09-26T00:00:00", "rating": 4.6, "count": 34}]), {"rating_delta": 0.2, "count_delta": 4, "since": "2026-08-01"})
check("split percentages", rep.split([r1, r2, dict(L["reviews"][2], sentiment="neutral")])["positive_pct"], 33)

# through the app
W.DATA = pathlib.Path(TMP)
tok = ws.CURRENT.set("homerepair"); W.init_db()
W.require_auth = lambda r: "local"
W.google_key = lambda: "k"
W.reserve_request = lambda: True
rep.fetch_listing = lambda pid, key, opener=None: rep.parse_listing(GOOGLE)
W.set_setting("sender_company", "HomeRepair Tech"); W.set_setting("sender_name", "Daniel"); W.set_setting("sender_phone", "(830) 205-0202")
W.set_setting("telnyx_api_key", "t"); W.set_setting("telnyx_from_number", "+18305550100")
class Req:
    headers = {}; cookies = {}; query_params = {}
    class client: host = "127.0.0.1"
d = W.reputation_get(Req())
check("no listing yet: the page says so", d["place_id"], "")
W.places_search = lambda q, key, pages=1: [{"id": "ChIJabc", "displayName": {"text": "HomeRepair Tech"}, "formattedAddress": "New Braunfels, TX", "rating": 4.6, "userRatingCount": 34}]
r = W.reputation_listing(Req(), W.ListingBody(query="HomeRepair Tech New Braunfels"))
check("search returns candidates", r["candidates"][0]["place_id"], "ChIJabc")
r = W.reputation_listing(Req(), W.ListingBody(place_id="ChIJabc"))
check("picking one saves it and pulls the listing", r["listing"]["count"], 34)
d = W.reputation_get(Req())
check("the page now has rating, count, one snapshot, three reviews with drafts",
      (d["rating"], d["count"], len(d["snapshots"]), len(d["reviews"]), all(x["reply_draft"] for x in d["reviews"])), (4.6, 34, 1, 3, True))
check("  negative review is flagged", next(x["sentiment"] for x in d["reviews"] if x["author"] == "Bob"), "negative")
W.reputation_due()
check("the weekly job doesn't add a second snapshot inside a week", len(W.reputation_get(Req())["snapshots"]), 1)
W.reputation_refresh_route(Req())
check("  a manual check does (and reviews are not duplicated)", (len(W.reputation_get(Req())["snapshots"]), len(W.reputation_get(Req())["reviews"])), (2, 3))
SENT = []
tb._send = lambda **k: SENT.append(k) or "m1"
r = W.reputation_request(Req(), W.ReviewRequestBody(customer="Ann Carter", phone="(830) 555-0199", channel="text"))
check("a review request texts the customer with the link", (SENT[0]["to_number"], L["review_url"] in SENT[0]["text"]), ("+18305550199", True))
with W.closing(W.db()) as c:
    c.execute("INSERT INTO sms_optouts (number, at, source) VALUES ('+18305550198', ?, 'sms:stop')", (W.now(),)); c.commit()
try:
    W.reputation_request(Req(), W.ReviewRequestBody(customer="X", phone="(830) 555-0198", channel="text")); check("STOP numbers refused", False)
except W.HTTPException as e:
    check("a number that said STOP is refused", "asked us to stop" in e.detail)
try:
    W.reputation_request(Req(), W.ReviewRequestBody(customer="X", email="nope", channel="email")); check("bad email refused", False)
except W.HTTPException as e:
    check("a bad email address is refused plainly", "doesn't look right" in e.detail)
d = W.reputation_get(Req())
check("requests are logged and counted", (d["requests_sent"], d["requests"][0]["channel"]), (1, "text"))
W.reputation_review_replied(Req(), d["reviews"][0]["id"], W.ReviewMarkBody(replied=True))
check("mark replied sticks", bool(W.reputation_get(Req())["reviews"][0]["replied_at"]))
ws.CURRENT.reset(tok)
print()
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
