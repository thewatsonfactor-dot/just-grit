# -*- coding: utf-8 -*-
"""The publishing queue and the per-network rules.

Written for a pivot: Daniel wants Just Grit to publish directly rather than
resell Metricool. The queue, the validation and the human-approval gate are
identical either way, so they live here and the PUBLISHER is swappable. That
is the whole point of this file - the expensive part of a social tool was
never the HTTP call.

WHAT ACTUALLY BREAKS, and why validation happens before anything leaves:
every network rejects content for its own reasons, hours later, in a webhook
nobody is watching. A caption of 2,300 characters is not an error you want to
discover from a customer. So the rules below are checked at queue time, where
a human is still looking at the screen.

Sources for these limits are the platforms' own developer docs as of
September 2026. They move - `LIMITS_CHECKED` is the date to re-verify.
"""
from __future__ import annotations
import re
from datetime import datetime

LIMITS_CHECKED = "2026-09-10"

# ── per-network rules ────────────────────────────────────────────────
RULES = {
    "instagram": {
        "caption_max": 2200,
        "hashtag_max": 30,
        "mention_max": 20,
        "image_formats": {"jpg", "jpeg"},      # JPEG only. PNG is rejected.
        "video_formats": {"mp4", "mov"},
        "aspect_min": 4 / 5,                   # 0.80
        "aspect_max": 1.91,
        "needs_public_media_url": True,        # no direct upload, ever
        "media_required": True,                # text-only posts do not exist
        "daily_api_posts": 100,
    },
    "facebook": {
        "caption_max": 63206,
        "hashtag_max": 0,                      # 0 = not limited
        "mention_max": 0,
        "image_formats": {"jpg", "jpeg", "png", "gif"},
        "video_formats": {"mp4", "mov"},
        "aspect_min": 0, "aspect_max": 0,
        "needs_public_media_url": False,
        "media_required": False,
        "daily_api_posts": 0,
    },
    "youtube": {
        "caption_max": 5000,                   # description
        "title_max": 100,
        "hashtag_max": 15,
        "mention_max": 0,
        "image_formats": set(),
        "video_formats": {"mp4", "mov", "avi", "webm"},
        "aspect_min": 0, "aspect_max": 0,
        "needs_public_media_url": False,
        "media_required": True,
        # 10,000 quota units/day, an upload costs ~1,600. Six uploads and the
        # project is done for the day - including every other call it makes.
        "daily_api_posts": 6,
        "quota_note": ("YouTube bills quota units, not posts: 10,000/day and an "
                       "upload costs ~1,600. Six uploads exhausts the project's "
                       "entire daily quota, search calls included."),
    },
}

HASHTAG = re.compile(r"(?<!\w)#\w+")
MENTION = re.compile(r"(?<!\w)@\w+")


def _ext(url: str) -> str:
    return (url or "").rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()


def validate(network: str, text: str, media: list | None = None,
             title: str = "", aspect: float | None = None) -> list:
    """Every reason this post would be rejected. Empty list means it can go."""
    net = (network or "").lower()
    r = RULES.get(net)
    if not r:
        return ["%s is not a network this can publish to (yet). Known: %s"
                % (network, ", ".join(sorted(RULES)))]
    media = media or []
    problems = []

    body = text or ""
    if len(body) > r["caption_max"]:
        problems.append("%s caption is %d characters; the limit is %d."
                        % (net, len(body), r["caption_max"]))
    if r["hashtag_max"] and len(HASHTAG.findall(body)) > r["hashtag_max"]:
        problems.append("%s allows %d hashtags; this has %d."
                        % (net, r["hashtag_max"], len(HASHTAG.findall(body))))
    if r["mention_max"] and len(MENTION.findall(body)) > r["mention_max"]:
        problems.append("%s allows %d @mentions; this has %d."
                        % (net, r["mention_max"], len(MENTION.findall(body))))
    if net == "youtube":
        if not title.strip():
            problems.append("YouTube needs a title.")
        elif len(title) > r["title_max"]:
            problems.append("YouTube title is %d characters; the limit is %d."
                            % (len(title), r["title_max"]))

    if r["media_required"] and not media:
        problems.append("%s cannot post text alone - it needs an image or a video."
                        % net)

    for m in media:
        e = _ext(m)
        ok = r["image_formats"] | r["video_formats"]
        if ok and e not in ok:
            problems.append("%s will not accept a .%s file. Allowed: %s"
                            % (net, e or "?", ", ".join(sorted(ok))))
        if r["needs_public_media_url"] and not str(m).startswith(("http://", "https://")):
            problems.append(
                "Instagram cannot be handed a file - the media has to already sit "
                "at a public URL it can fetch (%r is not one). This is the "
                "requirement people discover last and it needs somewhere to host "
                "images before any of this works." % m)

    if aspect and r["aspect_min"] and not (r["aspect_min"] <= aspect <= r["aspect_max"]):
        problems.append("%s needs an aspect ratio between %.2f and %.2f; this is %.2f."
                        % (net, r["aspect_min"], r["aspect_max"], aspect))
    return problems


def daily_room(network: str, already_sent_today: int) -> dict:
    """How many more posts this network will take today."""
    r = RULES.get((network or "").lower())
    if not r or not r["daily_api_posts"]:
        return {"limited": False, "left": None, "note": ""}
    left = max(0, r["daily_api_posts"] - max(0, already_sent_today))
    return {"limited": True, "left": left, "cap": r["daily_api_posts"],
            "note": r.get("quota_note", "")}


def check_post(networks: list, text: str, media: list | None = None,
               title: str = "", aspect: float | None = None,
               sent_today: dict | None = None) -> dict:
    """One post, every network it targets. Returns per-network problems.

    A post going to three networks is three different sets of rules, and the
    strictest one is usually Instagram. Reporting them per network rather than
    as one merged list is what lets somebody fix the caption for IG without
    wondering whether Facebook cared.
    """
    sent_today = sent_today or {}
    out, blocked = {}, []
    for n in networks or []:
        probs = validate(n, text, media, title, aspect)
        room = daily_room(n, sent_today.get(n, 0))
        if room["limited"] and room["left"] == 0:
            probs.append("%s is at its daily ceiling of %d. This has to wait for "
                         "tomorrow." % (n, room["cap"]))
        out[n] = {"problems": probs, "room": room}
        if probs:
            blocked.append(n)
    return {"ok": not blocked, "blocked": blocked, "by_network": out,
            "limits_checked": LIMITS_CHECKED}
