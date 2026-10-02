# -*- coding: utf-8 -*-
"""Publishing to Instagram. Two steps, not one, and that is the whole story.

Graph API v25.0. Unlike Facebook you cannot post in a single call:

    1. POST /<IG_ID>/media          -> creates a CONTAINER, returns its id
    2. (video only) poll the container until status_code is FINISHED
    3. POST /<IG_ID>/media_publish  -> publishes it, with creation_id

Skipping step 2 is the classic failure: an image container is ready almost
immediately so it works in testing, then the first video publishes a container
that is still IN_PROGRESS and errors in a way that looks random.

The other trap is that there is NO unpublished state. Facebook lets you create
a post and release it later; Instagram's media_publish IS the publish. So this
module splits the two calls deliberately - `stage()` prepares and stops,
`release()` is the irreversible one. A human decides between them, same rule
as everywhere else in this app.

Media must sit at a public URL. Meta fetches it; there is no upload. See
media.py, and check Cloudflare Access before blaming Instagram.
"""
from __future__ import annotations
import json, time, urllib.parse, urllib.request, urllib.error

GRAPH = "https://graph.facebook.com/v25.0"
TIMEOUT = 30
TERMINAL_OK = "FINISHED"
TERMINAL_BAD = {"ERROR", "EXPIRED"}


class InstagramError(RuntimeError):
    pass


def _req(method: str, path: str, params: dict):
    url = "%s/%s" % (GRAPH, path.lstrip("/"))
    body = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, "")}).encode()
    if method == "GET":
        url, body = "%s?%s" % (url, body.decode()), None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
        except ValueError:
            err = {}
        raise InstagramError(_explain(e.code, err, raw))
    except Exception as e:
        raise InstagramError("Could not reach Instagram (%s)." % type(e).__name__)


def _explain(code, err, raw) -> str:
    msg = err.get("message") or raw[:200]
    c, sub = err.get("code"), err.get("error_subcode")
    if c == 190:
        return ("The access token is invalid or expired. Instagram publishing "
                "uses the token of the linked Facebook Page, not a personal "
                "one. (Meta: %s)" % msg)
    if c == 200 or sub == 1349004:
        return ("Missing permission. Publishing needs instagram_business_content_"
                "publish (or instagram_content_publish) and the account must be a "
                "Business or Creator account - a personal account cannot use the "
                "API at all. (Meta: %s)" % msg)
    if c == 9007 or "media" in msg.lower() and "fetch" in msg.lower():
        return ("Instagram could not fetch the media. Their servers do the "
                "fetching, so the URL has to answer an anonymous request - not "
                "behind Cloudflare Access, not localhost. (Meta: %s)" % msg)
    if c == 36003 or "aspect" in msg.lower():
        return ("The image aspect ratio is outside Instagram's 4:5 to 1.91:1 "
                "window. (Meta: %s)" % msg)
    if c == 36001:
        return "Instagram rejected the image format. It takes JPEG. (Meta: %s)" % msg
    return "Instagram returned %s: %s" % (code, msg)


# ── step 1: stage ────────────────────────────────────────────────────
def stage(ig_id: str, token: str, image_url: str = "", video_url: str = "",
          caption: str = "", media_type: str = "") -> dict:
    """Create the container. Nothing is public after this call."""
    if not ig_id or not token:
        raise InstagramError("An Instagram user id and an access token are required.")
    if not image_url and not video_url:
        raise InstagramError(
            "Instagram cannot post text alone - it needs an image or a video.")
    for u in (image_url, video_url):
        if u and not str(u).startswith(("http://", "https://")):
            raise InstagramError(
                "Instagram fetches the media itself, so it has to be a public "
                "http(s) URL. A local file path will never work.")
    params = {"caption": caption, "access_token": token}
    if video_url:
        params["video_url"] = video_url
        params["media_type"] = (media_type or "REELS").upper()
    else:
        params["image_url"] = image_url
        if media_type:
            params["media_type"] = media_type.upper()
    out = _req("POST", "%s/media" % ig_id, params)
    if not out.get("id"):
        raise InstagramError("Instagram did not return a container id: %r" % out)
    return {"container_id": out["id"], "is_video": bool(video_url)}


def container_status(container_id: str, token: str) -> str:
    out = _req("GET", str(container_id), {"fields": "status_code",
                                          "access_token": token})
    return out.get("status_code") or "UNKNOWN"


def wait_ready(container_id: str, token: str, tries: int = 5,
               delay: float = 60.0, sleep=time.sleep) -> str:
    """Poll until the container is publishable.

    Meta's own guidance is once a minute for no more than five minutes. An
    image is usually ready at once; a video is not, and publishing an
    IN_PROGRESS container is the mistake that only shows up once real video
    goes through.
    """
    last = ""
    for i in range(max(1, tries)):
        last = container_status(container_id, token)
        if last == TERMINAL_OK:
            return last
        if last in TERMINAL_BAD:
            raise InstagramError(
                "The container came back %s before it could be published. "
                "EXPIRED means it sat unpublished too long; ERROR usually means "
                "Instagram could not fetch or transcode the media." % last)
        if i < tries - 1:
            sleep(delay)
    raise InstagramError(
        "The container was still %s after %d checks. Video processing can take "
        "longer than this - the container stays valid, so try releasing it "
        "again shortly rather than staging a new one." % (last, tries))


# ── step 2: release. This one is public and cannot be undone ─────────
def release(ig_id: str, token: str, container_id: str) -> dict:
    """Publish a staged container. There is no unpublished state on Instagram:
    once this returns, it is live and visible."""
    if not container_id:
        raise InstagramError("Nothing staged to publish.")
    return _req("POST", "%s/media_publish" % ig_id,
                {"creation_id": container_id, "access_token": token})


def quota(ig_id: str, token: str) -> dict:
    """How many of the 100 posts per rolling 24 hours are left."""
    out = _req("GET", "%s/content_publishing_limit" % ig_id,
               {"access_token": token, "fields": "config,quota_usage"})
    row = (out.get("data") or [{}])[0]
    used = row.get("quota_usage") or 0
    cap = (row.get("config") or {}).get("quota_total") or 100
    return {"used": used, "cap": cap, "left": max(0, cap - used)}


def media_stats(media_id: str, token: str) -> dict:
    """Reach, likes, comments for one published media."""
    d = _req("GET", media_id, {"fields": "like_count,comments_count", "access_token": token})
    out = {"likes": d.get("like_count", 0), "comments": d.get("comments_count", 0), "shares": None, "reach": None}
    try:
        ins = _req("GET", "%s/insights" % media_id, {"metric": "reach", "access_token": token})
        out["reach"] = ((ins.get("data") or [{}])[0].get("values") or [{}])[0].get("value")
    except InstagramError:
        pass
    return out
