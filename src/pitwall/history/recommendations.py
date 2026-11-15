"""Recommendation tracking: what to work on after each session, and whether it worked.

After a session, the findings that came back most often on its laps (at least
``MIN_COUNT`` laps and ``SHARE`` of the laps compared with the session's best lap) become at
most three recommendations. Each has a **target metric** for its corner (for "brakes
earlier": the brake point, later is better; for "lower minimum speed": the minimum speed,
higher is better...). In the next session on the same track and car, the target metric of all
usable laps is compared with the session the recommendation came from:

* **improved** / **worse**: the median moved the right / wrong way by more than the
  metric's minimum meaningful change and than two standard errors;
* **unchanged**: within that;
* **inconclusive**: fewer than ``EVAL_MIN_LAPS`` laps with the metric on either side;
* **open**: there is no later session yet.

The corner's time is compared the same way and shown next to it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pitwall.history.patterns import compared_laps
from pitwall.history.stats import NOT_ENOUGH, compare_groups

MIN_LAPS = 3  # compared laps in a session to make recommendations at all
MIN_COUNT = 2
SHARE = 0.4
MAX_RECS = 3
EVAL_MIN_LAPS = 3
SEGMENT_MIN_CHANGE_S = 0.02

# finding code -> (target metric, better, minimum meaningful change, unit, words)
TARGETS: dict[str, tuple[str, str, float, str, str]] = {
    "EARLY_BRAKE": ("brake_onset_m", "higher", 3.0, "m", "brake point (m into the lap)"),
    "LATE_BRAKE": ("min_speed_kmh", "higher", 2.0, "km/h", "minimum speed"),
    "SOFT_BRAKING": ("peak_brake", "higher", 0.05, "", "peak brake input"),
    "LOW_MIN_SPEED": ("min_speed_kmh", "higher", 2.0, "km/h", "minimum speed"),
    "LATE_THROTTLE": ("full_throttle_m", "lower", 5.0, "m", "full-throttle point (m into the lap)"),
    "THROTTLE_HESITATION": ("hesitation", "lower", 0.1, "", "throttle hesitation"),
    "COASTING": ("coasting_m", "lower", 5.0, "m", "coasting distance"),
    "EXIT_DEFICIT": ("exit_speed_kmh", "higher", 2.0, "km/h", "exit speed"),
    "LOCKUP": ("lockup_m", "lower", 2.0, "m", "lock-up distance"),
    "WHEELSPIN": ("wheelspin_m", "lower", 2.0, "m", "wheelspin distance"),
}


def _target(code: str, finding: dict[str, Any]) -> tuple[str, str, float, str, str] | None:
    t = TARGETS.get(code)
    if t is None:
        return None
    if code == "LATE_THROTTLE" and (finding.get("measured") or {}).get("point") != "full throttle":
        return ("throttle_pickup_m", "lower", 5.0, "m", "throttle pickup point (m into the lap)")
    return t


def session_recommendations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most three recommendations from one session's laps."""
    compared = compared_laps(rows)
    n = len(compared)
    if n < MIN_LAPS:
        return []
    found: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in compared:
        seen = set()
        for f in r["findings"]:
            key = (f["code"], f["segment_id"])
            if key not in seen:
                seen.add(key)
                found[key].append(f)
    recs = []
    for (code, seg), items in found.items():
        k = len(items)
        target = _target(code, items[-1])
        if target is None or k < MIN_COUNT or k / n < SHARE:
            continue
        metric, better, min_change, unit, words = target
        mean_impact = sum(f["impact_s"] for f in items) / k
        recs.append(
            {
                "code": code,
                "segment_id": seg,
                "segment_name": items[-1]["segment_name"],
                "title": items[-1]["title"],
                "suggestion": items[-1]["suggestion"],
                "laps": k,
                "of": n,
                "mean_impact_s": round(mean_impact, 3),
                "metric": metric,
                "metric_words": words,
                "unit": unit,
                "better": better,
                "min_change": min_change,
                "score": k * max(mean_impact, 0.001),
            }
        )
    recs.sort(key=lambda r: (-r["score"], r["segment_id"], r["code"]))
    return recs[:MAX_RECS]


def _values(rows: list[dict[str, Any]], seg: str, metric: str | None) -> list[float]:
    out = []
    for r in rows:
        for s in r["segments"]:
            if s["id"] != seg:
                continue
            v = s["time_s"] if metric is None else (s.get("metrics") or {}).get(metric)
            if v is not None:
                out.append(float(v))
    return out


def evaluate(
    rec: dict[str, Any], rows: list[dict[str, Any]], next_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    seg = rec["segment_id"]
    change = compare_groups(
        _values(rows, seg, rec["metric"]),
        _values(next_rows, seg, rec["metric"]),
        better=rec["better"],
        min_change=rec["min_change"],
        min_laps=EVAL_MIN_LAPS,
    )
    seg_time = compare_groups(
        _values(rows, seg, None),
        _values(next_rows, seg, None),
        better="lower",
        min_change=SEGMENT_MIN_CHANGE_S,
        min_laps=EVAL_MIN_LAPS,
    )
    outcome = "inconclusive" if change.status == NOT_ENOUGH else change.status
    return {
        "outcome": outcome,
        "metric_change": change.to_dict(2),
        "segment_time": seg_time.to_dict(4),
    }


def track_recommendations(
    sessions: list[dict[str, Any]], rows_by_session: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Recommendations of every session (oldest first), each judged in the next session."""
    out = []
    for i, s in enumerate(sessions):
        recs = session_recommendations(rows_by_session.get(s["session_key"], []))
        nxt = sessions[i + 1] if i + 1 < len(sessions) else None
        for rec in recs:
            item = {k: v for k, v in rec.items() if k != "score"}
            item["session_key"] = s["session_key"]
            item["session_date"] = s.get("date")
            if nxt is None:
                item["outcome"] = "open"
                item["next_session_key"] = None
            else:
                item.update(
                    evaluate(
                        rec,
                        rows_by_session.get(s["session_key"], []),
                        rows_by_session.get(nxt["session_key"], []),
                    )
                )
                item["next_session_key"] = nxt["session_key"]
                item["next_session_date"] = nxt.get("date")
            out.append(item)
    return out
