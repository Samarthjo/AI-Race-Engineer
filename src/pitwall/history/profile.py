"""The driver profile: the history of a track, as facts the race engineer may quote.

The profile is built "as of" a session: only that session and the sessions before it on the
same track and car class count, so a report reads the same whenever it is made. Facts
(ids ``hist:*``, kind ``history``):

* ``hist:summary``: sessions, usable laps and best lap so far;
* ``hist:trend``: this session against the earlier ones (or why that cannot be told yet);
* ``hist:pattern:<corner>:<code>``: recurring findings (at most three);
* ``hist:rec:<corner>:<code>``: what was recommended after the previous session and what
  this session shows for its target metric (at most three);
* ``hist:weak:<corner>`` / ``hist:strong:<corner>``: corners with the most / least time
  left against your best there (at most three / two).

Dates and other sessions' lap numbers are left out of the sentences on purpose: the
grounding check would read them as numbers or as laps of this session.
"""

from __future__ import annotations

from typing import Any

from pitwall.engineer.facts import Fact, fmt_lap, fmt_s
from pitwall.f1udp.enums import FORMULA, name
from pitwall.history import patterns, recommendations, trends
from pitwall.history.stats import NOT_ENOUGH
from pitwall.track.model import TrackModel

MAX_PATTERNS = 3
MAX_RECS = 3
MAX_WEAK = 3
MAX_STRONG = 2

OUTCOME_WORDS = {
    "improved": "improved",
    "worse": "went the wrong way",
    "unchanged": "no clear change",
    "inconclusive": "too few laps to judge",
}


def _num(v: float | None, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "km/h":
        return f"{v:.0f} km/h"
    if unit == "m":
        return f"{v:.0f} m"
    return f"{v:.2f}"


def _with_exit(corner: dict[str, Any]) -> str:
    return " (with the straight after it)" if len(corner.get("zone") or []) > 1 else ""


def _sessions(n: int) -> str:
    return "1 session" if n == 1 else f"{n} sessions"


def profile(
    rows: list[dict[str, Any]], session: dict[str, Any], model: TrackModel | None
) -> dict[str, Any]:
    """The history views as of ``session`` (rows: the track and car class, any date)."""
    mark = (session.get("first_utc") or "", session["session_key"])
    upto = [r for r in rows if (r["first_utc"] or "", r["session_key"]) <= mark]
    cur = trends.current_rows(upto, model)
    info = trends.segment_info(cur, model)
    sessions = trends.by_session(cur)
    this = [rs for rs in sessions if rs[0]["session_key"] == session["session_key"]]
    recs: list[dict[str, Any]] = []
    if this and len(sessions) >= 2:
        prev, now = sessions[-2], sessions[-1]
        for rec in recommendations.session_recommendations(prev):
            recs.append({**rec, **recommendations.evaluate(rec, prev, now)})
    return {
        "rows": upto,
        "current": cur,
        "sessions": len(sessions),
        "in_session": bool(this),
        "trend": trends.lap_trend(cur),
        "patterns": patterns.recurring(cur),
        "recommendations": recs,
        "corners": trends.corner_potential(cur, info),
    }


def profile_facts(
    rows: list[dict[str, Any]], session: dict[str, Any], model: TrackModel | None
) -> list[Fact]:
    """The ``hist:*`` facts for a report on a lap of ``session`` (unbound: the report binds
    them with the track's corner names)."""
    p = profile(rows, session, model)
    track = session.get("track_name") or "this track"
    car = name(FORMULA, session.get("formula"), "formula")
    cur = p["current"]
    facts: list[Fact] = []
    if not cur:
        facts.append(
            Fact(
                "hist:summary",
                "history",
                f"No history at {track} ({car}) yet: no laps of this or earlier sessions have "
                "been analysed for it.",
            )
        )
        return facts

    best = min(cur, key=lambda r: (r["lap_time_ms"], r["first_utc"] or ""))
    where = " (set in this session)" if best["session_key"] == session["session_key"] else ""
    facts.append(
        Fact(
            "hist:summary",
            "history",
            f"History at {track} ({car}) up to this session: {_sessions(p['sessions'])}, "
            f"{len(cur)} usable laps; best lap {fmt_lap(best['lap_time_ms'])}{where}.",
            data={"sessions": p["sessions"], "laps": len(cur), "best_ms": best["lap_time_ms"]},
        )
    )

    t = p["trend"]
    if t["status"] == NOT_ENOUGH or not p["in_session"]:
        why = t.get("reason") or "needs this session and earlier ones"
        facts.append(
            Fact(
                "hist:trend",
                "history",
                f"No trend can be told yet ({why}).",
                data={"status": t["status"]},
            )
        )
    else:
        verdict = {
            "improved": "faster than before",
            "worse": "slower than before",
            "unchanged": "no clear change",
        }[t["status"]]
        text = (
            f"This session against your {_sessions(t['sessions_before'])} before it here: "
            f"median lap {fmt_lap(round(t['after'] * 1000))} vs "
            f"{fmt_lap(round(t['before'] * 1000))}, {fmt_s(t['delta'])} ({verdict}; changes "
            f"smaller than {t['threshold']:.3f} s are not counted)."
        )
        if t["caveats"]:
            text += " " + " ".join(t["caveats"])
        facts.append(
            Fact(
                "hist:trend",
                "history",
                text,
                data={k: t.get(k) for k in ("status", "delta", "threshold", "n_before", "n_after")},
            )
        )

    pat = p["patterns"]
    for item in pat["patterns"][:MAX_PATTERNS]:
        facts.append(
            Fact(
                f"hist:pattern:{item['segment_id']}:{item['code']}",
                "history",
                f"Recurring at {item['segment_name']}: {item['title'][0].lower()}"
                f"{item['title'][1:]} on {item['laps']} of {item['of']} laps compared with "
                f"their session's best lap, in {_sessions(item['sessions'])} of "
                f"{item['of_sessions']}; on average {fmt_s(item['mean_impact_s'])} each time.",
                segment_id=item["segment_id"],
                segment_name=item["segment_name"],
                code=item["code"],
                data=item,
            )
        )

    for rec in p["recommendations"][:MAX_RECS]:
        m = rec["metric_change"]
        outcome = OUTCOME_WORDS[rec["outcome"]]
        if rec["outcome"] == "inconclusive":
            measured = (
                f"too few laps with it to judge ({m['n_before']} before, {m['n_after']} now; "
                f"needs {recommendations.EVAL_MIN_LAPS} each)"
            )
        else:
            measured = (
                f"median {_num(m['before'], rec['unit'])} before, {_num(m['after'], rec['unit'])} "
                f"in this session: {outcome}"
            )
            st = rec["segment_time"]
            if st["status"] != NOT_ENOUGH and st["delta"] is not None:
                measured += f"; time there {fmt_s(st['delta'])}"
        facts.append(
            Fact(
                f"hist:rec:{rec['segment_id']}:{rec['code']}",
                "history",
                f"After your previous session the recommendation at {rec['segment_name']} was: "
                f"{rec['suggestion'] or rec['title']} Its target, the {rec['metric_words']}: "
                f"{measured}.",
                segment_id=rec["segment_id"],
                segment_name=rec["segment_name"],
                code=rec["code"],
                data={k: v for k, v in rec.items() if k != "score"},
            )
        )

    corners = p["corners"]
    if corners["status"] == "ok":
        for c in corners["weakest"][:MAX_WEAK]:
            facts.append(
                Fact(
                    f"hist:weak:{c['segment_id']}",
                    "history",
                    f"Most time left at {c['segment_name']}{_with_exit(c)}: over your last "
                    f"{corners['recent_laps']} laps you are on average {fmt_s(c['mean_gap_s'])} "
                    f"slower there than your best (median {fmt_s(c['median_gap_s'])}).",
                    segment_id=c["segment_id"],
                    segment_name=c["segment_name"],
                    data=c,
                )
            )
        for c in corners["strongest"][:MAX_STRONG]:
            facts.append(
                Fact(
                    f"hist:strong:{c['segment_id']}",
                    "history",
                    f"Closest to your best at {c['segment_name']}{_with_exit(c)}: on average "
                    f"{fmt_s(c['mean_gap_s'])} from your best there over your last "
                    f"{corners['recent_laps']} laps.",
                    segment_id=c["segment_id"],
                    segment_name=c["segment_name"],
                    data=c,
                )
            )
    return facts
