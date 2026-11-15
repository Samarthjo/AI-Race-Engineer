"""Per-track history: sessions over time, trends, strongest and weakest corners.

Laps are grouped by track (the track key, which also keeps synthetic tracks apart), car class
(formula) and real/synthetic; groups are never mixed. Corner-level results only use rows
made with the current version of the track's corners.

* **Trend:** the latest session's laps against the laps of all earlier sessions, with
  :func:`pitwall.history.stats.compare_groups` (medians; the noise comes from the laps' own
  spread; at least four laps on each side). Lap time: a change under 0.05 s is "unchanged";
  a corner: under 0.02 s.
* **Strongest / weakest corners (against your best):** for each corner together with the
  straight after it (a slow exit loses its time on that straight), how far your most recent
  20 laps are from your best time there (any lap of the group), on average: the time left
  there per lap, mistakes included. The largest gaps (at least 0.02 s) are where the most
  time is left; the smallest, where you are nearly always at your best (flat-out kinks are
  not counted as strengths). The median gap is shown next to it.
* **Caveats:** a different car (team), weather or a track temperature range wider than 5 °C
  between the two sides of a trend is reported with it: part of the change may come from that.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from pitwall.f1udp.enums import FORMULA, WEATHER, name
from pitwall.history.stats import MAD_SD, MIN_LAPS, NOT_ENOUGH, compare_groups
from pitwall.track.model import TrackModel

RECENT_LAPS = 20
LAP_MIN_CHANGE_S = 0.05
SEGMENT_MIN_CHANGE_S = 0.02
WEAK_MIN_S = 0.02  # typical time left at a corner below this: not listed as a weakness
MAX_LISTED = 3
TEMP_SPREAD_C = 5


def group_id(track_key: str, formula: int | None) -> str:
    return f"{track_key}:{'x' if formula is None else formula}"


def parse_group_id(gid: str) -> tuple[str, int | None]:
    tk, sep, f = gid.rpartition(":")
    if not sep or not tk:
        raise ValueError(f"invalid history group {gid!r}")
    return tk, None if f == "x" else int(f)


def _spread(xs: list[float]) -> float | None:
    """Robust standard deviation (median absolute deviation, scaled)."""
    if len(xs) < 2:
        return None
    m = statistics.median(xs)
    return MAD_SD * statistics.median([abs(x - m) for x in xs])


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def by_session(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Rows split per session, oldest session first."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["session_key"], []).append(r)
    return sorted(out.values(), key=lambda rs: (rs[0]["first_utc"] or "", rs[0]["session_key"]))


def current_rows(rows: list[dict[str, Any]], model: TrackModel | None) -> list[dict[str, Any]]:
    """Rows made with the current corners of the track (the newest model version stored
    when the model itself is not available)."""
    if not rows:
        return []
    version = model.version if model is not None else max(r["model_version"] for r in rows)
    return [r for r in rows if r["model_version"] == version]


def segment_times(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        for s in r["segments"]:
            out[s["id"]].append(float(s["time_s"]))
    return out


def segment_info(
    rows: list[dict[str, Any]], model: TrackModel | None
) -> list[tuple[str, str, str]]:
    """(id, name, kind) of every segment, in track order."""
    if model is not None:
        return [(s.id, s.name, s.kind) for s in model.segments]
    ids: list[str] = []
    for r in rows:
        for s in r["segments"]:
            if s["id"] not in ids:
                ids.append(s["id"])
    return [(i, i, "corner") for i in ids]


# ------------------------------------------------------------------- summaries


def groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One summary per track / car class / real-or-synthetic, latest activity first."""
    by: dict[tuple[str, int | None, int], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[(r["track_key"], r["formula"], int(r["synthetic"]))].append(r)
    out = []
    for (tk, formula, syn), rs in by.items():
        sessions = by_session(rs)
        best = min(rs, key=lambda r: (r["lap_time_ms"], r["first_utc"] or ""))
        latest = sessions[-1]
        latest_best = min(latest, key=lambda r: r["lap_time_ms"])
        out.append(
            {
                "id": group_id(tk, formula),
                "track_key": tk,
                "track_name": rs[-1].get("track_name") or tk,
                "formula": formula,
                "formula_name": name(FORMULA, formula, "formula"),
                "synthetic": bool(syn),
                "sessions": len(sessions),
                "laps": len(rs),
                "first_date": sessions[0][0]["first_utc"],
                "last_date": latest[0]["first_utc"],
                "best_lap_ms": best["lap_time_ms"],
                "best": {
                    "session_key": best["session_key"],
                    "lap_index": best["lap_index"],
                    "lap_number": best["lap_number"],
                    "date": best["first_utc"],
                },
                "latest_best_lap_ms": latest_best["lap_time_ms"],
            }
        )
    out.sort(key=lambda g: (g["last_date"] or "", g["laps"]), reverse=True)
    return out


def session_series(
    rows: list[dict[str, Any]], seg_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """Every session of a group, oldest first: best, median, spread and ideal lap."""
    out = []
    for rs in by_session(rows):
        times = [r["lap_time_ms"] / 1000 for r in rs]
        best = min(rs, key=lambda r: (r["lap_time_ms"], r["lap_index"]))
        ideal = None
        if seg_ids:
            seg = segment_times(rs)
            if all(seg.get(s) for s in seg_ids):
                ideal = sum(min(seg[s]) for s in seg_ids)
        first = rs[0]
        out.append(
            {
                "session_key": first["session_key"],
                "date": first["first_utc"],
                "label": first.get("capture_label"),
                "laps": len(rs),
                "best_lap_ms": best["lap_time_ms"],
                "best_lap_number": best["lap_number"],
                "median_s": _r(statistics.median(times)),
                "spread_s": _r(_spread(times)),
                "ideal_s": _r(ideal),
                "lap_times_s": [round(t, 3) for t in times],
                "team_id": first.get("team_id"),
                "weather": name(WEATHER, first.get("weather"), "weather"),
                "track_temp_c": first.get("track_temp_c"),
            }
        )
    return out


# ---------------------------------------------------------------------- trends


def _caveats(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    out = []
    teams_b = {r.get("team_id") for r in before}
    teams_a = {r.get("team_id") for r in after}
    if teams_b != teams_a:
        out.append(
            "The car (team) differs between the sessions compared: part of the change may come "
            "from the car."
        )
    weather_b = {r.get("weather") for r in before}
    weather_a = {r.get("weather") for r in after}
    if weather_b != weather_a:
        out.append("The weather differs between the sessions compared.")
    temps = [r["track_temp_c"] for r in before + after if r.get("track_temp_c") is not None]
    if temps and max(temps) - min(temps) > TEMP_SPREAD_C:
        out.append(
            f"Track temperature ranges from {min(temps)} to {max(temps)} °C across the sessions "
            "compared."
        )
    return out


def lap_trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest session against all earlier sessions of the group (lap times)."""
    sessions = by_session(rows)
    base: dict[str, Any] = {
        "what": "latest session vs earlier sessions",
        "min_laps": MIN_LAPS,
        "min_change_s": LAP_MIN_CHANGE_S,
    }
    if len(sessions) < 2:
        return {**base, "status": NOT_ENOUGH, "reason": "needs at least 2 sessions"}
    before = [r for rs in sessions[:-1] for r in rs]
    after = sessions[-1]
    change = compare_groups(
        [r["lap_time_ms"] / 1000 for r in before],
        [r["lap_time_ms"] / 1000 for r in after],
        better="lower",
        min_change=LAP_MIN_CHANGE_S,
    )
    out = {**base, **change.to_dict(3), "sessions_before": len(sessions) - 1}
    if change.status == NOT_ENOUGH:
        out["reason"] = f"needs at least {MIN_LAPS} usable laps on each side"
    out["caveats"] = _caveats(before, after)
    return out


def segment_trends(
    rows: list[dict[str, Any]], info: list[tuple[str, str, str]]
) -> list[dict[str, Any]]:
    """Per corner and straight: latest session against earlier sessions (segment time)."""
    sessions = by_session(rows)
    if len(sessions) < 2:
        return []
    before = segment_times([r for rs in sessions[:-1] for r in rs])
    after = segment_times(sessions[-1])
    out = []
    for sid, sname, kind in info:
        change = compare_groups(
            before.get(sid, []),
            after.get(sid, []),
            better="lower",
            min_change=SEGMENT_MIN_CHANGE_S,
        )
        out.append({"segment_id": sid, "segment_name": sname, "kind": kind, **change.to_dict(4)})
    return out


def corner_zones(info: list[tuple[str, str, str]]) -> list[tuple[str, str, str, list[str]]]:
    """Every corner with the straight after it: (id, name, kind, segment ids of the zone).

    A late or slow exit loses most of its time on the following straight, so a corner's
    time left is measured over the corner and its exit straight together."""
    out = []
    for i, (sid, sname, kind) in enumerate(info):
        if kind == "straight":
            continue
        zone = [sid]
        if i + 1 < len(info) and info[i + 1][2] == "straight":
            zone.append(info[i + 1][0])
        out.append((sid, sname, kind, zone))
    return out


def _zone_times(rows: list[dict[str, Any]], zone: list[str]) -> list[float]:
    out = []
    for r in rows:
        t = {s["id"]: float(s["time_s"]) for s in r["segments"]}
        if all(z in t for z in zone):
            out.append(sum(t[z] for z in zone))
    return out


def corner_potential(
    rows: list[dict[str, Any]], info: list[tuple[str, str, str]]
) -> dict[str, Any]:
    """Recent laps against your best time at every corner (with its exit straight), and
    every segment on its own for the table.

    The ranking uses the **mean** gap to your best over the recent laps: the time left there
    per lap on average, mistakes included (one big mistake raises it; the median gap, which
    ignores occasional mistakes, is shown next to it)."""
    recent = rows[-RECENT_LAPS:]
    out: dict[str, Any] = {
        "recent_laps": len(recent),
        "min_laps": MIN_LAPS,
        "segments": [],
        "corners": [],
        "weakest": [],
        "strongest": [],
        "ideal_lap_s": None,
        "typical_lap_s": None,
    }
    if len(recent) < MIN_LAPS:
        out["status"] = NOT_ENOUGH
        return out
    out["status"] = "ok"
    best = segment_times(rows)
    typical = segment_times(recent)
    if all(best.get(sid) for sid, _, _ in info):
        out["ideal_lap_s"] = round(sum(min(best[sid]) for sid, _, _ in info), 3)
    out["typical_lap_s"] = round(statistics.median(r["lap_time_ms"] / 1000 for r in recent), 3)
    names = {sid: sname for sid, sname, _ in info}

    def gaps_item(times: list[float], b: float) -> dict[str, Any]:
        gaps = [t - b for t in times]
        return {
            "best_s": round(b, 4),
            "typical_s": round(statistics.median(times), 4),
            "mean_gap_s": round(statistics.fmean(gaps), 4),
            "median_gap_s": round(statistics.median(gaps), 4),
            "spread_s": _r(_spread(times), 4),
            "laps": len(times),
        }

    for sid, sname, kind in info:
        times = typical.get(sid, [])
        if best.get(sid) and len(times) >= MIN_LAPS:
            out["segments"].append(
                {"segment_id": sid, "segment_name": sname, "kind": kind}
                | gaps_item(times, min(best[sid]))
            )
    for sid, sname, kind, zone in corner_zones(info):
        all_times = _zone_times(rows, zone)
        times = _zone_times(recent, zone)
        if not all_times or len(times) < MIN_LAPS:
            continue
        out["corners"].append(
            {
                "segment_id": sid,
                "segment_name": sname,
                "kind": kind,
                "zone": zone,
                "zone_name": " + ".join(names[z] for z in zone),
            }
            | gaps_item(times, min(all_times))
        )
    corners = out["corners"]
    ranked = sorted(corners, key=lambda c: (-c["mean_gap_s"], c["segment_id"]))
    out["weakest"] = [c for c in ranked if c["mean_gap_s"] >= WEAK_MIN_S][:MAX_LISTED]
    weak_ids = {c["segment_id"] for c in out["weakest"]}
    # flat-out kinks are left out: being at your best there says little about your driving
    out["strongest"] = [
        c
        for c in sorted(corners, key=lambda c: (c["mean_gap_s"], c["segment_id"]))
        if c["segment_id"] not in weak_ids and c["kind"] != "flat"
    ][:MAX_LISTED]
    return out
