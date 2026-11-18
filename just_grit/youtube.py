# -*- coding: utf-8 -*-
"""Publishing to YouTube (Shorts). Three things make this different from Meta.

1. IT TAKES THE FILE, NOT A URL. Instagram fetches a public URL; YouTube wants
   the bytes uploaded to it. So a Short is read from this app's own media
   store (media.py) and sent with a resumable upload: one POST that describes
   the video and returns an upload address, one PUT with the bytes.

2. THE KEY IS A REFRESH TOKEN. Google OAuth hands out an access token that
   lasts an hour and a refresh token that lasts until it is revoked. Only the
   refresh token is stored (`yt_refresh_token`); every upload trades it for a
   fresh access token first. The OAuth client id and secret come from the
   owner's own Google Cloud project - this app never ships one.

3. A SHORT IS NOT A SEPARATE API. Any vertical or square video of three
   minutes or less is shown as a Short. "#Shorts" in the title or description
   is added so it is filed that way even when the crop is borderline.

The upload is the public step and it cannot be undone from here (delete it in
YouTube Studio). Nothing in this module runs unless a person scheduled the
post or pressed Publish - same rule as everywhere else in this app.

Google's own catch, worth knowing before anything else: a video uploaded
through a Google Cloud project that has not passed YouTube's API audit is
locked to private, whatever privacyStatus says. That is a Google account
setting, not a bug here - `upload()` reports what YouTube actually set.
"""
from __future__ import annotations
import json, urllib.error, urllib.parse, urllib.request
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
# upload = post videos; readonly = read the channel's name so Setup can say
# which channel is connected. Nothing here can delete or edit a video.
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly")
TIMEOUT = 60
UPLOAD_TIMEOUT = 600
TITLE_MAX = 100
DESC_MAX = 5000
SHORTS_TAG = "#Shorts"
PRIVACY = {"public", "unlisted", "private"}


class YouTubeError(RuntimeError):
    pass


# ── the one HTTP door. Tests replace it; nothing else touches the network ──
def _http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
          timeout: int = TIMEOUT):
    """Returns (status, headers, body_bytes). Raises YouTubeError on failure."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise YouTubeError(_explain(e.code, raw))
    except Exception as e:
        raise YouTubeError("Could not reach YouTube (%s)." % type(e).__name__)


http = _http


def _explain(code: int, raw: str) -> str:
    try:
        j = json.loads(raw)
    except ValueError:
        j = {}
    err = j.get("error")
    if isinstance(err, str):                       # the OAuth endpoints
        desc = j.get("error_description") or err
        if err == "invalid_grant":
            return ("Google refused the saved YouTube key (it was revoked, or the "
                    "Google Cloud app is still in Testing and the key expired after "
                    "7 days). Connect YouTube again in Setup. (Google: %s)" % desc)
        if err in ("invalid_client", "unauthorized_client"):
            return ("Google doesn't recognise the YouTube client ID or secret in "
                    "Setup. Check both against your Google Cloud project. (Google: %s)" % desc)
        return "Google returned %s: %s" % (code, desc)
    err = err or {}
    reasons = {x.get("reason") for x in (err.get("errors") or []) if isinstance(x, dict)}
    msg = err.get("message") or raw[:200]
    if "quotaExceeded" in reasons or "uploadLimitExceeded" in reasons:
        return ("YouTube's daily limit for this Google Cloud project is used up. "
                "It resets at midnight Pacific time. (YouTube: %s)" % msg)
    if code == 401:
        return ("YouTube rejected the key. Connect YouTube again in Setup. "
                "(YouTube: %s)" % msg)
    if code == 403:
        return ("YouTube said no (%s). Check that the YouTube Data API v3 is "
                "enabled in your Google Cloud project and that you connected the "
                "right channel. (YouTube: %s)" % (", ".join(sorted(reasons)) or "forbidden", msg))
    return "YouTube returned %s: %s" % (code, msg)


# ── connecting: the owner's browser goes to Google and comes back ──────
def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    if not client_id:
        raise YouTubeError("Add the YouTube client ID in Setup first.")
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPES,
        # offline + consent = Google returns a refresh token every time,
        # not only the first time this account ever said yes.
        "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state})


def _token(params: dict) -> dict:
    _, _, raw = http("POST", TOKEN_URL,
                     {"Content-Type": "application/x-www-form-urlencoded"},
                     urllib.parse.urlencode(params).encode())
    try:
        return json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise YouTubeError("Google sent back something that isn't JSON.")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """The one-time code from Google's redirect -> {refresh_token, access_token}."""
    out = _token({"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    if not out.get("refresh_token"):
        raise YouTubeError("Google didn't send a lasting key. Press Connect YouTube "
                           "again and approve every box on Google's screen.")
    return out


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    if not (client_id and client_secret and refresh_token):
        raise YouTubeError("YouTube is not connected. See Setup → Social.")
    out = _token({"client_id": client_id, "client_secret": client_secret,
                  "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if not out.get("access_token"):
        raise YouTubeError("Google did not return an access token: %r" % out)
    return out["access_token"]


def channel(token: str) -> dict:
    """{id, title} of the channel this key posts to."""
    _, _, raw = http("GET", API + "/channels?" + urllib.parse.urlencode(
        {"part": "snippet", "mine": "true"}), {"Authorization": "Bearer " + token})
    items = (json.loads(raw.decode("utf-8", "replace") or "{}").get("items") or [])
    if not items:
        raise YouTubeError("That Google account has no YouTube channel. Sign in with "
                           "the account (or brand account) that owns the channel.")
    return {"id": items[0].get("id", ""),
            "title": (items[0].get("snippet") or {}).get("title", "")}


# ── posting ──────────────────────────────────────────────────────────
def shorts_text(title: str, description: str) -> tuple[str, str]:
    """Title and description as they will be sent: trimmed to YouTube's limits,
    with #Shorts present once."""
    t = " ".join((title or "").split())
    d = (description or "").strip()
    if not t:
        raise YouTubeError("A YouTube Short needs a title.")
    if SHORTS_TAG.lower() not in (t + " " + d).lower():
        if len(t) + 1 + len(SHORTS_TAG) <= TITLE_MAX:
            t = "%s %s" % (t, SHORTS_TAG)
        else:
            d = (d + "\n\n" + SHORTS_TAG).strip()
    if len(t) > TITLE_MAX:
        raise YouTubeError("The YouTube title is %d characters; the limit is %d."
                           % (len(t), TITLE_MAX))
    return t, d[:DESC_MAX]


def upload(token: str, path, title: str, description: str = "",
           privacy: str = "public", tags: list | None = None) -> dict:
    """Upload one video file. Public as soon as YouTube finishes processing
    (unless Google locks it private - see the module note).
    Returns {id, url, privacy}."""
    p = Path(path)
    if not p.is_file():
        raise YouTubeError("The video file is missing from this app's media folder.")
    if privacy not in PRIVACY:
        raise YouTubeError("privacy must be public, unlisted or private.")
    title, description = shorts_text(title, description)
    data = p.read_bytes()
    meta = {"snippet": {"title": title, "description": description,
                        "tags": [t for t in (tags or []) if t][:15], "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    _, hdrs, _ = http("POST", UPLOAD + "?" + urllib.parse.urlencode(
        {"uploadType": "resumable", "part": "snippet,status"}),
        {"Authorization": "Bearer " + token,
         "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Type": "video/mp4",
         "X-Upload-Content-Length": str(len(data))},
        json.dumps(meta).encode())
    where = {k.lower(): v for k, v in (hdrs or {}).items()}.get("location")
    if not where or not where.startswith("https://"):
        raise YouTubeError("YouTube did not give an upload address.")
    _, _, raw = http("PUT", where, {"Authorization": "Bearer " + token,
                                    "Content-Type": "video/mp4"}, data,
                     timeout=UPLOAD_TIMEOUT)
    out = json.loads(raw.decode("utf-8", "replace") or "{}")
    vid = out.get("id")
    if not vid:
        raise YouTubeError("YouTube took the upload but returned no video id: %r"
                           % str(out)[:200])
    got = (out.get("status") or {}).get("privacyStatus", privacy)
    res = {"id": vid, "url": "https://youtube.com/shorts/%s" % vid, "privacy": got}
    if got != privacy:
        res["warning"] = ("YouTube set this video to %s, not %s. That usually means "
                          "the Google Cloud project hasn't passed YouTube's API audit "
                          "yet." % (got, privacy))
    return res


def video_stats(token: str, video_id: str) -> dict:
    _, _, raw = http("GET", API + "/videos?" + urllib.parse.urlencode(
        {"part": "statistics", "id": video_id}), {"Authorization": "Bearer " + token})
    items = json.loads(raw.decode("utf-8", "replace") or "{}").get("items") or []
    s = (items[0].get("statistics") if items else {}) or {}
    num = lambda k: int(s[k]) if str(s.get(k, "")).isdigit() else None
    return {"likes": num("likeCount"), "comments": num("commentCount"),
            "views": num("viewCount"), "shares": None, "reach": num("viewCount")}
