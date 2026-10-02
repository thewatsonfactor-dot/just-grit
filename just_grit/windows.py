# -*- coding: utf-8 -*-
"""Sending windows by audience - send when they read, not when it's
convenient to queue.

From the Small Business plugin's outreach playbook (sequence_patterns.md,
2026-09-15), which Daniel handed over on 2026-09-25: office and
professional people read 8-10am Tue-Thu; trades 6-8am or after 5pm;
retail mid-morning, never weekends; and nothing to anyone on Monday
morning or Friday afternoon, because neither gets read.

Property managers, HOA management companies, commercial buildings and
brokerages are offices. HOA boards are volunteers with day jobs - they
read the board inbox in the evening. Watson Factor's local business
owners are the retail/hospitality row.

A window is a list of (weekday, start_hour, end_hour) in the workspace's
local time. The autopilot sends a draft only while its audience's window
is open; the 48-hour rule and the hourly pace still apply on top.
"""
from datetime import datetime, timedelta

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# Best hours first, so the ordering is "who reads now".
WINDOWS = {
    # offices: never Monday morning, never Friday afternoon
    "office": [(TUE, 8, 17), (WED, 8, 17), (THU, 8, 17), (MON, 13, 17), (FRI, 8, 12)],
    # volunteer boards: evenings, plus mid-week mornings for the ones who
    # forward the board inbox to work
    "hoa": [(MON, 17, 20), (TUE, 17, 20), (WED, 17, 20), (THU, 17, 20),
            (TUE, 8, 12), (WED, 8, 12), (THU, 8, 12)],
    # trades and field services
    "trades": [(TUE, 6, 8), (WED, 6, 8), (THU, 6, 8), (MON, 17, 19), (TUE, 17, 19),
               (WED, 17, 19), (THU, 17, 19), (FRI, 6, 8)],
    # retail and hospitality: mid-morning, no weekends
    "retail": [(TUE, 9, 12), (WED, 9, 12), (THU, 9, 12), (MON, 13, 16), (FRI, 9, 12)],
}
DEFAULT_AUDIENCE = "office"

LABEL = {
    "office": "offices: Tue-Thu 8-5, Mon after 1, Fri before noon",
    "hoa": "HOA boards: weekday evenings 5-8, Tue-Thu mornings",
    "trades": "trades: 6-8am or 5-7pm, Tue-Fri",
    "retail": "shops and restaurants: mid-morning Tue-Fri, Mon after 1",
}

# prospect category -> audience
CATEGORY_AUDIENCE = {
    "hoa_board": "hoa", "hoa": "hoa", "condo": "hoa", "board": "hoa",
    "property_manager": "office", "hoa_manager": "office", "realtor": "office",
    "real_estate": "office", "brokerage": "office", "real_estate_agent": "office",
    "commercial": "office", "commercial_manager": "office", "office": "office",
    "medical": "office", "retail": "retail", "vacation": "office",
    "restaurant": "retail", "cafe": "retail", "salon": "retail", "bar": "retail",
    "shop": "retail", "store": "retail", "boutique": "retail",
    "plumber": "trades", "hvac": "trades", "electrician": "trades", "roofer": "trades",
    "contractor": "trades", "landscaper": "trades", "handyman": "trades", "painter": "trades",
}


def audience_for(category: str, workspace: str = "") -> str:
    cat = (category or "").lower().strip()
    if cat in CATEGORY_AUDIENCE:
        return CATEGORY_AUDIENCE[cat]
    for k, v in CATEGORY_AUDIENCE.items():
        if k in cat:
            return v
    if workspace == "homerepair":
        return "office"                      # every HomeRepair buyer is an office or a board
    return "retail" if workspace == "watson" else DEFAULT_AUDIENCE


def is_open(local_dt: datetime, audience: str) -> bool:
    for day, start, end in WINDOWS.get(audience, WINDOWS[DEFAULT_AUDIENCE]):
        if local_dt.weekday() == day and start <= local_dt.hour < end:
            return True
    return False


def next_open(local_dt: datetime, audience: str) -> datetime:
    """The next moment this audience's window is open (now, if it is)."""
    if is_open(local_dt, audience):
        return local_dt
    wins = WINDOWS.get(audience, WINDOWS[DEFAULT_AUDIENCE])
    best = None
    for delta in range(0, 8):
        d = (local_dt + timedelta(days=delta)).replace(minute=0, second=0, microsecond=0)
        for day, start, end in wins:
            if d.weekday() != day:
                continue
            cand = d.replace(hour=start)
            if cand <= local_dt:
                continue
            if best is None or cand < best:
                best = cand
    return best or local_dt + timedelta(days=1)


def describe(audience: str) -> str:
    return LABEL.get(audience, LABEL[DEFAULT_AUDIENCE])
