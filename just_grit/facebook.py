# -*- coding: utf-8 -*-
"""Publishing to a Facebook Page.

Graph API v25.0. Facebook is deliberately the first network: its rules are the
most forgiving (text alone is a valid post, PNG is fine, no aspect ratio), so
when a publish fails you know it is the auth path and not the content. Every
other network adds its own reasons to fail on top of these.

TOKENS. A Page post needs a PAGE access token, not the user token you get from
login. The usual failure is posting with a user token and getting a permissions
error that names neither problem. `page_token()` does the exchange, and the
result is long-lived (Meta documents page tokens derived from a long-lived user
token as non-expiring).

Permissions: pages_manage_posts, plus pages_read_engagement and pages_show_list.

NOTHING HERE PUBLISHES WITHOUT BEING TOLD. `published=False` creates an
unpublished post the Page admin releases by hand; that is the default, same
rule as the Outbox and the ad campaigns.
"""
from __future__ import annotations
import json, urllib.parse, urllib.request, urllib.error

GRAPH = "https://graph.facebook.com/v25.0"
TIMEOUT = 30


class FacebookError(RuntimeError):
    pass


def _post(path: str, params: dict):
    url = "%s/%s" % (GRAPH, path.lstrip("/"))
    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, "")}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
        except ValueError:
            err = {}
        raise FacebookError(_explain(e.code, err, raw))
    except Exception as e:
        raise FacebookError("Could not reach Facebook (%s)." % type(e).__name__)


def _get(path: str, params: dict):
    url = "%s/%s?%s" % (GRAPH, path.lstrip("/"), urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
        except ValueError:
            err = {}
        raise FacebookError(_explain(e.code, err, raw))
    except Exception as e:
        raise FacebookError("Could not reach Facebook (%s)." % type(e).__name__)


def _explain(code, err, raw) -> str:
    """Meta's errors are famously unhelpful. Translate the ones that will happen.

    Each of these cost somebody an afternoon at least once, and the API's own
    message names none of them.
    """
    sub = err.get("error_subcode")
    msg = err.get("message") or raw[:200]
    if err.get("code") == 190:
        return ("The access token is invalid or expired. If you just made it in "
                "Graph API Explorer it was probably a short-lived user token - "
                "exchange it for a long-lived one, then derive the PAGE token. "
                "(Meta: %s)" % msg)
    if err.get("code") == 200 or sub == 1349004:
        return ("Missing permission. Posting to a Page needs pages_manage_posts, "
                "and the token has to be the PAGE token, not the user token that "
                "created it. (Meta: %s)" % msg)
    if err.get("code") == 100 and "url" in msg.lower():
        return ("Facebook could not fetch the image URL. Their servers do the "
                "fetching, so the URL has to be reachable by an anonymous "
                "stranger - not behind Cloudflare Access, not localhost. "
                "(Meta: %s)" % msg)
    if err.get("code") == 803:
        return "That Page id does not exist or the token cannot see it. (Meta: %s)" % msg
    return "Facebook returned %s: %s" % (code, msg)


# ── tokens ───────────────────────────────────────────────────────────
def long_lived_token(app_id: str, app_secret: str, short_token: str) -> dict:
    """Turn the 1-2 hour token from Graph Explorer into a ~60 day one."""
    return _get("oauth/access_token", {
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_token})


def pages(user_token: str) -> list:
    """Pages this user administers, each with its own page token."""
    out = _get("me/accounts", {"access_token": user_token,
                               "fields": "id,name,access_token"})
    return out.get("data", [])


def page_direct(user_token: str, page_id: str) -> dict:
    """Ask for one Page by id. me/accounts leaves out Pages a person reaches
    through a business portfolio (Homerepair.tech on 2026-09-24: me/accounts
    came back empty while GET /<page id> returned the Page, its Instagram and
    - with the field asked for - its token). {} if the token can't see it."""
    try:
        out = _get(str(page_id), {"fields": "id,name,access_token", "access_token": user_token})
    except FacebookError:
        return {}
    return out if out.get("id") else {}


def business_pages(user_token: str) -> list:
    """Pages owned by the person's business portfolios. Needs
    business_management; quietly empty without it."""
    try:
        out = _get("me/businesses", {"fields": "owned_pages{id,name}", "access_token": user_token})
    except FacebookError:
        return []
    rows = []
    for b in out.get("data", []):
        rows += (b.get("owned_pages") or {}).get("data", [])
    return rows


def token_expiry(token: str) -> dict:
    """{"expires_at": unix or 0 for never, "valid": bool} - a token can
    inspect itself through debug_token."""
    try:
        d = _get("debug_token", {"input_token": token, "access_token": token}).get("data", {})
    except FacebookError:
        return {}
    return {"expires_at": int(d.get("expires_at") or 0), "valid": bool(d.get("is_valid"))}


def page_token(user_token: str, page_id: str) -> str:
    for p in pages(user_token):
        if str(p.get("id")) == str(page_id):
            tok = p.get("access_token")
            if tok:
                return tok
    direct = page_direct(user_token, page_id)
    if direct.get("access_token"):
        return direct["access_token"]
    raise FacebookError(
        "No page token came back for page %s. Either the user is not an admin "
        "of it, or the token is missing pages_show_list." % page_id)


def instagram_account(page_id: str, page_token: str) -> dict:
    """The Instagram Business account linked to this Page.

    Instagram's API addresses accounts by a numeric id, not by @handle, and the
    only place that id lives is on the Page it is linked to. Looking it up by
    hand in Graph API Explorer is the step people get stuck on, so the token
    exchange does it here instead.

    Returns {} when nothing is linked - which is a real answer, not an error:
    a personal Instagram account cannot be linked and cannot use the API.
    """
    out = _get(str(page_id), {"fields": "instagram_business_account{id,username}",
                              "access_token": page_token})
    ig = out.get("instagram_business_account") or {}
    return {"id": ig.get("id", ""), "username": ig.get("username", "")}


# ── publishing ───────────────────────────────────────────────────────
def publish(page_id: str, token: str, message: str = "",
            image_url: str = "", link: str = "", published: bool = False) -> dict:
    """One post. `published=False` leaves it unpublished for a human to release.

    Routing matters: a photo goes to /photos with `url`, everything else goes
    to /feed. Posting an image_url to /feed silently drops the image and
    publishes the text alone, which looks like it worked.
    """
    if not page_id or not token:
        raise FacebookError("A page id and a page access token are both required.")
    if not (message or "").strip() and not image_url:
        raise FacebookError("A post needs text, an image, or both.")
    if image_url and not str(image_url).startswith(("http://", "https://")):
        raise FacebookError(
            "Facebook fetches the image itself, so image_url has to be a public "
            "http(s) URL - a local path will never work.")

    if image_url:
        return _post("%s/photos" % page_id, {
            "url": image_url, "caption": message,
            "published": "true" if published else "false",
            "access_token": token})
    return _post("%s/feed" % page_id, {
        "message": message, "link": link or None,
        "published": "true" if published else "false",
        "access_token": token})


def post_stats(post_id: str, token: str) -> dict:
    """Reach, likes, comments, shares for one published post. A photo post
    returns a photo id; its feed post is `post_id` on the photo object, so
    try the object as-is and fall back through that field."""
    fields = "likes.summary(true).limit(0),comments.summary(true).limit(0),shares,post_id"
    d = _get(post_id, {"fields": fields, "access_token": token})
    if "likes" not in d and d.get("post_id"):
        d = _get(d["post_id"], {"fields": fields, "access_token": token})
    out = {"likes": (d.get("likes") or {}).get("summary", {}).get("total_count", 0),
           "comments": (d.get("comments") or {}).get("summary", {}).get("total_count", 0),
           "shares": (d.get("shares") or {}).get("count", 0), "reach": None}
    try:
        ins = _get("%s/insights" % (d.get("id") or post_id), {"metric": "post_impressions_unique", "access_token": token})
        vals = ((ins.get("data") or [{}])[0].get("values") or [{}])
        out["reach"] = vals[0].get("value")
    except FacebookError:
        pass
    return out
