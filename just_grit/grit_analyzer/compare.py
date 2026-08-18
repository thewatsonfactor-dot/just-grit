"""Competitor mode — N sites in one market, ranked against each other.

The output answers the seller's question, not the analyst's: who's winning,
on what, and what's the one sentence that sells against each competitor.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .analyzer import analyze


def compare(urls: list[str], deep: bool = False) -> dict:
    def one(u):
        try:
            return analyze(u, deep=deep)
        except Exception as e:
            return {"ok": False, "url": u, "host": u, "error": str(e)[:120]}

    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(one, urls))

    live = [r for r in results if r.get("ok")]
    live.sort(key=lambda r: -r["overall"])
    dead = [r for r in results if not r.get("ok")]

    ranking = []
    leader = live[0] if live else None
    for i, r in enumerate(live):
        gap_vs_leader = (leader["overall"] - r["overall"]) if leader else 0
        weakest_cat = min(r["scores"].items(), key=lambda kv: kv[1]["score"])
        strongest_cat = max(r["scores"].items(), key=lambda kv: kv[1]["score"])
        top = r["priorities"][0] if r["priorities"] else None
        ranking.append({
            "rank": i + 1,
            "host": r["host"],
            "score": r["overall"],
            "grade": r["grade"],
            "cms": r["stack"]["cms"],
            "has_retargeting": r["stack"]["has_retargeting"],
            "has_analytics": r["stack"]["has_analytics"],
            "strongest": {"category": strongest_cat[1]["label"],
                          "score": strongest_cat[1]["score"]},
            "weakest": {"category": weakest_cat[1]["label"],
                        "score": weakest_cat[1]["score"]},
            "top_issue": top["title"] if top else None,
            "gap_vs_leader": gap_vs_leader,
            "sell_against": _sell_against(r, leader),
        })

    return {
        "ok": True,
        "compared": len(urls),
        "loaded": len(live),
        "failed": [{"host": r.get("host"), "error": r.get("error")} for r in dead],
        "leader": leader["host"] if leader else None,
        "ranking": ranking,
        "market_read": _market_read(live, dead),
    }


def _sell_against(r: dict, leader: dict | None) -> str:
    """One sentence to use when selling against this specific competitor —
    or, if the prospect IS this company, the one-liner for the pitch."""
    if leader and r["host"] == leader["host"]:
        weakest = min(r["scores"].items(), key=lambda kv: kv[1]["score"])[1]
        return (f"Market leader at {r['overall']}, but weakest on "
                f"{weakest['label'].lower()} ({weakest['score']}) — that's the "
                "crack to widen.")
    top = r["priorities"][0] if r["priorities"] else None
    bits = []
    if top:
        bits.append(top["title"].lower())
    if not r["stack"]["has_retargeting"]:
        bits.append("no retargeting — their visitors are recapturable by you")
    if leader:
        bits.append(f"{leader['overall'] - r['overall']} points behind "
                    f"{leader['host']}")
    return "; ".join(bits[:3]).capitalize() + "."


def _market_read(live: list[dict], dead: list[dict]) -> str:
    if not live:
        return "Nothing loaded — check the domains."
    avg = round(sum(r["overall"] for r in live) / len(live))
    no_pixel = sum(1 for r in live if not r["stack"]["has_retargeting"])
    no_ga = sum(1 for r in live if not r["stack"]["has_analytics"])
    spread = live[0]["overall"] - live[-1]["overall"]
    s = (f"{len(live)} sites scored, market average {avg}/100, "
         f"{spread}-point spread between best and worst. ")
    if no_pixel >= len(live) * 0.6:
        s += (f"{no_pixel} of {len(live)} run no retargeting — in this market, "
              "whoever installs a pixel first gets everyone else's abandoned "
              "visitors. ")
    if no_ga:
        s += f"{no_ga} can't measure their own marketing at all. "
    if dead:
        s += f"{len(dead)} site(s) didn't load — easiest calls on the list. "
    s += (f"The leader is {live[0]['host']} at {live[0]['overall']}; the gap "
          "everyone else needs to close is mostly "
          f"{min(live[-1]['scores'].items(), key=lambda kv: kv[1]['score'])[1]['label'].lower()}.")
    return s
