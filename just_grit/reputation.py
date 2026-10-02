# -*- coding: utf-8 -*-
"""Reputation: the business's own Google listing, watched over time.

Copied from the one GoHighLevel page a local owner opens every week
(26 Sep 2026): rating and review-count trend, the latest reviews sorted
positive / negative, a reply drafted for each, and a "send a review
request" button. Sources: Google Places (New) place details for the
listing and its five most relevant reviews; a weekly snapshot row for the
trend. No AI: sentiment is the star rating with a few keyword nudges,
replies are templates in the owner's voice. Nothing here posts anything;
replies are copied by hand into Google, requests go out only when the
owner presses Send.
"""
import json, re, urllib.request, urllib.error, urllib.parse

DETAILS_URL = "https://places.googleapis.com/v1/places/%s"
DETAILS_MASK = "id,displayName,rating,userRatingCount,googleMapsUri,reviews"


class ListingError(Exception):
    pass


def fetch_listing(place_id: str, key: str, opener=None) -> dict:
    """{'place_id','name','rating','count','maps_url','review_url','reviews':[...]}"""
    if not (place_id and key):
        raise ListingError("Need the listing's Google place id and a Google key.")
    req = urllib.request.Request(DETAILS_URL % urllib.parse.quote(place_id),
                                 headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": DETAILS_MASK})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise ListingError("Google rejected the key. 'Places API (New)' has to be enabled for it.")
        if e.code == 404:
            raise ListingError("Google doesn't know that place id any more. Pick the listing again.")
        raise ListingError("Google returned %d." % e.code)
    except Exception as e:
        raise ListingError("Could not reach Google (%s)." % type(e).__name__)
    return parse_listing(data)


def parse_listing(data: dict) -> dict:
    pid = data.get("id") or ""
    out = {"place_id": pid, "name": (data.get("displayName") or {}).get("text", ""),
           "rating": float(data.get("rating") or 0), "count": int(data.get("userRatingCount") or 0),
           "maps_url": data.get("googleMapsUri") or "", "review_url": review_link(pid), "reviews": []}
    for r in data.get("reviews") or []:
        txt = ((r.get("text") or {}).get("text") or (r.get("originalText") or {}).get("text") or "").strip()
        au = (r.get("authorAttribution") or {}).get("displayName") or "A customer"
        out["reviews"].append({
            "review_id": r.get("name") or ("%s|%s|%s" % (au, r.get("publishTime", ""), r.get("rating", ""))),
            "author": au, "rating": int(r.get("rating") or 0), "text": txt,
            "at": r.get("publishTime") or "", "when": r.get("relativePublishTimeDescription") or ""})
    return out


def review_link(place_id: str) -> str:
    return "https://search.google.com/local/writereview?placeid=%s" % urllib.parse.quote(place_id) if place_id else ""


# ── sentiment: stars first, words second ────────────────────────────────
NEG_WORDS = ["never again", "rude", "no show", "no-show", "late", "overcharged", "scam", "worst", "terrible",
             "unprofessional", "didn't show", "did not show", "ignored", "unanswered", "still waiting", "refund",
             "damaged", "broke", "mess", "disappointed", "waste"]
POS_WORDS = ["on time", "highly recommend", "recommend", "professional", "great", "excellent", "fast", "friendly",
             "fair price", "honest", "thank", "went above", "quick", "clean", "easy"]


def sentiment(rating: int, text: str) -> str:
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    neg = sum(1 for w in NEG_WORDS if w in low)
    pos = sum(1 for w in POS_WORDS if w in low)
    if rating >= 4:
        return "negative" if neg >= 2 and neg > pos else "positive"
    if rating <= 2:
        return "negative"
    return "positive" if pos > neg else "negative" if neg > pos else "neutral"


def split(reviews: list) -> dict:
    n = len(reviews) or 1
    c = {"positive": 0, "negative": 0, "neutral": 0}
    for r in reviews:
        c[r.get("sentiment") or sentiment(r.get("rating", 0), r.get("text", ""))] += 1
    return {k: v for k, v in c.items()} | {k + "_pct": round(100 * v / n) for k, v in c.items()}


# ── reply drafts, in the owner's voice ──────────────────────────────────
def _first(name: str) -> str:
    n = (name or "").strip().split(" ")[0]
    return n if n and n.lower() not in ("a", "google", "user", "customer") else ""


def _mention(text: str) -> str:
    """One thing they said, to prove the reply was written for them."""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    m = re.search(r"([^.!?]{12,90}[.!?])", t)
    s = (m.group(1) if m else t[:80]).strip().rstrip(".!?")
    return s[0].lower() + s[1:] if s else ""


def reply_draft(review: dict, business: str, sender: str, phone: str = "") -> str:
    who = _first(review.get("author", ""))
    hi = "Thanks, %s." % who if who else "Thank you."
    sent = review.get("sentiment") or sentiment(review.get("rating", 0), review.get("text", ""))
    said = _mention(review.get("text", ""))
    sign = " - %s, %s" % (sender, business) if sender else " - %s" % business
    if sent == "positive":
        body = ("%s Glad it went the way it should%s. That's the job, and it helps more than you'd think when you say so here."
                % (hi, (" - " + said) if said else ""))
        return body + sign
    if sent == "negative":
        body = ("%s I'm sorry - that's not how we want a visit to go%s. I'd like to make it right. "
                "Call me%s and I'll handle it myself." % (hi.replace("Thanks", "Thank you for telling us") if who else "Thank you for telling us.",
                                                          (", and I read what you wrote about " + said) if said else "",
                                                          (" at " + phone) if phone else " at the number on our page"))
        return body + sign
    body = ("%s We take every review seriously%s. If there's something we could have done better, call me%s - I'd rather hear it than guess."
            % (hi, (", including " + said) if said else "", (" at " + phone) if phone else ""))
    return body + sign


# ── the review request ──────────────────────────────────────────────────
def request_text(customer: str, business: str, sender: str, link: str) -> str:
    """Under 160 characters where it can be; the link is the point."""
    who = _first(customer)
    return ("%s%s from %s. Thanks for having us out. A Google review helps a small shop more than anything: %s"
            % (("Hi %s, " % who) if who else "Hi, ", sender or business, business, link))


def request_email(customer: str, business: str, sender: str, link: str) -> tuple:
    who = _first(customer)
    subject = "Quick favor from %s" % business
    body = ("%s\n\nThanks for having us out. If the work was good, would you say so on Google? It takes a minute and it's how the next "
            "customer finds us:\n\n%s\n\nIf anything wasn't right, reply to this email instead and I'll fix it myself.\n\nThanks,\n%s\n%s"
            % (("Hi %s," % who) if who else "Hi,", link, sender or business, business))
    return subject, body


def trend(snaps: list) -> dict:
    """snaps: [{'at','rating','count'}] oldest first → deltas for the header."""
    if not snaps:
        return {"rating_delta": None, "count_delta": None, "since": ""}
    first, last = snaps[0], snaps[-1]
    return {"rating_delta": round(last["rating"] - first["rating"], 2), "count_delta": last["count"] - first["count"],
            "since": first["at"][:10]}
