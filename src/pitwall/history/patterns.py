"""Recurring patterns: findings that keep coming back.

Every lap in the history is compared with its session's best lap, so a finding there means
"on this lap you did this differently from your best lap". Over your latest ``WINDOW``
compared laps of a track (so that a habit you have fixed fades out), a pattern is recurring
when it appears on at least ``SHARE`` of them (and on at least ``MIN_COUNT`` laps), and there
are at least ``MIN_LAPS`` compared laps at all. Patterns that are the same on every lap,
including the best, are not visible this way: comparing with a faster reference (your
personal best, a real F1 lap) shows those.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

MIN_LAPS = 5
MIN_COUNT = 3
SHARE = 0.4
WINDOW = 40  # latest compared laps considered


def compared_laps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Laps that were compared with another lap (not their session's reference)."""
    return [r for r in rows if r["lap_index"] != r["reference_lap"]]


def recurring(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Recurring findings over the latest compared laps (rows oldest first)."""
    compared = compared_laps(rows)[-WINDOW:]
    n = len(compared)
    out: dict[str, Any] = {
        "compared_laps": n,
        "window": WINDOW,
        "min_laps": MIN_LAPS,
        "patterns": [],
    }
    if n < MIN_LAPS:
        out["status"] = "not enough data"
        return out
    out["status"] = "ok"
    seen: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"laps": set(), "sessions": set(), "impacts": []}
    )
    names: dict[tuple[str, str], tuple[str, str]] = {}
    for r in compared:
        for f in r["findings"]:
            key = (f["code"], f["segment_id"])
            item = seen[key]
            if (r["session_key"], r["lap_index"]) in item["laps"]:
                continue
            item["laps"].add((r["session_key"], r["lap_index"]))
            item["sessions"].add(r["session_key"])
            item["impacts"].append(f["impact_s"])
            names[key] = (f["segment_name"], f["title"])
    total_sessions = len({r["session_key"] for r in compared})
    for (code, seg), item in seen.items():
        k = len(item["laps"])
        if k < MIN_COUNT or k / n < SHARE:
            continue
        out["patterns"].append(
            {
                "code": code,
                "segment_id": seg,
                "segment_name": names[(code, seg)][0],
                "title": names[(code, seg)][1],
                "laps": k,
                "of": n,
                "share": round(k / n, 3),
                "sessions": len(item["sessions"]),
                "of_sessions": total_sessions,
                "mean_impact_s": round(sum(item["impacts"]) / k, 3),
            }
        )
    out["patterns"].sort(key=lambda p: (-p["laps"], -p["mean_impact_s"], p["segment_id"]))
    return out
