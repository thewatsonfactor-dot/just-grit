# -*- coding: utf-8 -*-
"""Metricool: scheduling, best-time-to-post, and competitor tracking.

Endpoints and parameter names below were taken from Metricool's own OpenAPI
spec (https://app.metricool.com/api/swagger.json), not from a blog post or a
guess. Two things about this API are easy to get wrong and are the reason a
first attempt fails:

  1. Auth is a custom header, `X-Mc-Auth`, not `Authorization: Bearer`.
  2. EVERY call needs `userId` and `blogId` as query parameters, including
     ones that already name a resource in the path.

The token is an Advanced/Custom plan feature. If the account is on a lower
plan there is no token to get, and this module says so rather than failing
with an opaque 401.

NOTHING HERE PUBLISHES ON ITS OWN. `schedule_post` writes a DRAFT by default;
`autoPublish` has to be passed explicitly. Same rule as the Outbox: a machine
may prepare, a person decides.
"""
from __future__ import annotations
import json, urllib.parse, urllib.request, urllib.error

BASE = "https://app.metricool.com/api"
TIMEOUT = 25

# Networks Metricool will track competitors on. Anything else is rejected up
# front - the API returns a generic error for an unsupported network and it
# reads like an auth problem.
COMPETITOR_NETWORKS = {"instagram", "facebook", "youtube", "twitter",
                       "twitch", "bluesky"}
BESTTIME_PROVIDERS = {"instagram", "facebook", "twitter", "linkedin",
                      "tiktok", "youtube"}


class MetricoolError(RuntimeError):
    pass


class Metricool:
    def __init__(self, token: str, user_id: str, blog_id: str,
                 timezone: str = "America/Chicago"):
        if not token or not user_id or not blog_id:
            raise MetricoolError(
                "Metricool needs a token, a userId and a blogId. Settings > "
                "API in Metricool has all three; the token is only issued on "
                "Advanced and Custom plans.")
        self.token, self.user_id, self.blog_id = token, str(user_id), str(blog_id)
        self.tz = timezone

    # ── plumbing ─────────────────────────────────────────────────────
    def _call(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None):
        q = {"userId": self.user_id, "blogId": self.blog_id}
        for k, v in (params or {}).items():
            if v is None:
                continue
            q[k] = v
        url = "%s%s?%s" % (BASE, path, urllib.parse.urlencode(q, doseq=True))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Mc-Auth": self.token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise MetricoolError(
                    "Metricool rejected the credentials (HTTP %d). The token "
                    "is per-account and only exists on Advanced/Custom plans, "
                    "and userId/blogId must match the brand you're addressing."
                    % e.code)
            raise MetricoolError("Metricool returned %d: %s" % (e.code, detail))
        except Exception as e:
            raise MetricoolError("Could not reach Metricool (%s)" % type(e).__name__)

    # ── scheduling ───────────────────────────────────────────────────
    def scheduled_posts(self, start: str, end: str):
        """Posts scheduled but not yet published, between two ISO datetimes."""
        return self._call("GET", "/v2/scheduler/posts",
                          {"start": start, "end": end, "timezone": self.tz})

    def schedule_post(self, text: str, when: str, networks: list,
                      media: list | None = None, auto_publish: bool = False,
                      draft: bool | None = None, first_comment: str = ""):
        """Queue one post.

        `draft=True` and `auto_publish=False` are the defaults on purpose. A
        draft sits in the Metricool calendar for a human to look at; nothing
        reaches an audience until somebody says so. Pass auto_publish=True
        only for a post a person has already approved.
        """
        bad = [n for n in networks if not n]
        if not networks or bad:
            raise MetricoolError("At least one network is required.")
        # draft=None means "follow auto_publish". A hard default of True made
        # schedule_post(auto_publish=True) produce a draft that never went out -
        # the two flags contradicting each other, silently.
        if draft is None:
            draft = not auto_publish
        payload = {
            "text": text,
            "publicationDate": {"dateTime": when, "timezone": self.tz},
            "providers": [{"network": n} for n in networks],
            "autoPublish": bool(auto_publish),
            "draft": bool(draft),
        }
        if media:
            payload["media"] = media
        if first_comment:
            payload["firstCommentText"] = first_comment
        return self._call("POST", "/v2/scheduler/posts", body=payload)

    def update_post(self, post_id, payload: dict):
        return self._call("PUT", "/v2/scheduler/posts/%s" % post_id, body=payload)

    def delete_post(self, post_id):
        return self._call("DELETE", "/v2/scheduler/posts/%s" % post_id)

    # ── best time to post ────────────────────────────────────────────
    def best_times(self, provider: str, start: str, end: str):
        p = (provider or "").lower()
        if p not in BESTTIME_PROVIDERS:
            raise MetricoolError("Best-time isn't available for %r. Try: %s"
                                 % (provider, ", ".join(sorted(BESTTIME_PROVIDERS))))
        return self._call("GET", "/v2/scheduler/besttimes/%s" % p,
                          {"start": start, "end": end, "timezone": self.tz})

    @staticmethod
    def top_slots(payload, n: int = 5):
        """Flatten the day/hour grid into the n best slots.

        Metricool returns a 7x24 grid of scores. An all-zero grid is not "post
        at midnight Sunday" - it means there is no posting history to learn
        from yet, and saying so is more useful than ranking noise.
        """
        rows = (payload or {}).get("data") or []
        slots = []
        for d in rows:
            for h in d.get("bestTimesByHour") or []:
                slots.append((d.get("dayOfWeek"), h.get("hourOfDay"),
                              h.get("value") or 0))
        if not slots:
            return {"ready": False, "why": "Metricool returned no slots.", "slots": []}
        if max(s[2] for s in slots) == 0:
            return {"ready": False, "slots": [],
                    "why": "Every slot scores zero, which means there is not enough "
                           "posting history on this network yet. Post for a few "
                           "weeks and this fills in."}
        slots.sort(key=lambda s: -s[2])
        return {"ready": True, "why": "", "slots": [
            {"day": s[0], "hour": s[1], "score": s[2]} for s in slots[:n]]}

    # ── competitors ──────────────────────────────────────────────────
    def competitors(self, network: str, frm: str, to: str, limit: int = 50):
        net = (network or "").lower()
        if net not in COMPETITOR_NETWORKS:
            raise MetricoolError(
                "Metricool tracks competitors on %s only - not %r."
                % (", ".join(sorted(COMPETITOR_NETWORKS)), network))
        return self._call("GET", "/v2/analytics/competitors/%s" % net,
                          {"from": frm, "to": to, "timezone": self.tz,
                           "limit": str(limit)})

    def add_competitor(self, network: str, handle: str):
        net = (network or "").lower()
        if net not in COMPETITOR_NETWORKS:
            raise MetricoolError("Cannot track competitors on %r." % network)
        return self._call("POST", "/v2/analytics/competitors/%s" % net,
                          {"id": handle})

    def remove_competitor(self, network: str, handle: str):
        return self._call("DELETE", "/v2/analytics/competitors/%s"
                          % (network or "").lower(), {"id": handle})

    def competitor_posts(self, network: str, frm: str, to: str):
        """What they actually published. IG uses a different path to include reels."""
        net = (network or "").lower()
        path = ("/v2/analytics/competitors/instagram/publications" if net == "instagram"
                else "/v2/analytics/competitors/%s/posts" % net)
        return self._call("GET", path, {"from": frm, "to": to, "timezone": self.tz})
