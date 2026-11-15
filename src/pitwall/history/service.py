"""Reading the history: keeps the cache current, then builds the per-track and overview views.

All views are computed from the history cache (:mod:`pitwall.history.build`); sessions whose
cache is missing or out of date are analysed first.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from pitwall.analysis.benchmark import PRO_MIN_IMPACT_S, Z_FINDING, benchmark, place_reference
from pitwall.analysis.trace import NotComparableError, load_trace
from pitwall.history import build as hbuild
from pitwall.history import patterns, recommendations, trends
from pitwall.reference.archive import FetchError
from pitwall.reference.laps import ReferenceDataError
from pitwall.reference.service import check_pairing, load_reference
from pitwall.storage import db
from pitwall.track.model import load_model
from pitwall.track.service import model_for_session, session_track_key

MAX_PRO_LISTED = 3
_BUILD_LOCK = threading.Lock()  # pages load in parallel: one build at a time, then no work left


def ensure(conn: sqlite3.Connection, data_dir: Path, track_key: str | None = None) -> dict:
    """Analyse sessions whose history is missing or out of date (only one track's with
    ``track_key``). Cheap when everything is current."""
    with _BUILD_LOCK:
        todo = hbuild.stale(conn, data_dir)
        if track_key is not None:
            todo = [s for s in todo if session_track_key(s) == track_key]
        if not todo:
            return {"sessions": 0, "laps": 0, "seconds": 0.0}
        return hbuild.build(conn, data_dir, todo)


def rebuild(conn: sqlite3.Connection, data_dir: Path, *, force: bool = False) -> dict[str, Any]:
    """Analyse every stale session (every session with ``force``), one build at a time."""
    with _BUILD_LOCK:
        return hbuild.build(conn, data_dir, force=force)


def cache_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """How the history cache stands: sessions per build status, and the sessions that could
    not be analysed with the reason (for example a track model of another length)."""
    st = db.history_state(conn)
    return {
        "analysis_version": hbuild.ANALYSIS_VERSION,
        "sessions": len(st),
        "by_status": dict(Counter(v["status"] for v in st.values())),
        "problems": [
            {
                "session_key": k,
                "track_key": v["track_key"],
                "status": v["status"],
                "reason": v["reason"],
            }
            for k, v in sorted(st.items())
            if v["status"] == "not comparable"
        ],
    }


def load_rows(
    conn: sqlite3.Connection,
    track_key: str | None = None,
    formula: int | None = None,
    *,
    any_formula: bool = False,
) -> list[dict[str, Any]]:
    rows = hbuild.history_rows(db.lap_history(conn, track_key=track_key))
    if any_formula:
        return rows
    return [r for r in rows if r["formula"] == formula]


def until(rows: list[dict[str, Any]], session: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of sessions up to and including ``session`` (by start time)."""
    mark = (session.get("first_utc") or "", session["session_key"])
    return [r for r in rows if (r["first_utc"] or "", r["session_key"]) <= mark]


def track_view(
    rows: list[dict[str, Any]], model: Any, *, synthetic: bool | None = None
) -> dict[str, Any]:
    """Everything the History page shows for one group (rows already filtered)."""
    if synthetic is not None:
        rows = [r for r in rows if bool(r["synthetic"]) == synthetic]
    cur = trends.current_rows(rows, model)
    info = trends.segment_info(cur, model)
    seg_ids = [i for i, _, _ in info]
    sessions = trends.by_session(cur)
    rec_sessions = [
        {"session_key": rs[0]["session_key"], "date": rs[0]["first_utc"]} for rs in sessions
    ]
    rows_by = {rs[0]["session_key"]: rs for rs in sessions}
    groups = trends.groups(rows)
    return {
        "group": groups[0] if groups else None,
        "model": None
        if model is None
        else {
            "key": model.key,
            "version": model.version,
            "status": model.status,
            "segments": [{"id": s.id, "name": s.name, "kind": s.kind} for s in model.segments],
        },
        "excluded_laps": len(rows) - len(cur),
        "sessions": trends.session_series(cur, seg_ids),
        "trend": trends.lap_trend(cur),
        "segment_trends": trends.segment_trends(cur, info),
        "corners": trends.corner_potential(cur, info),
        "patterns": patterns.recurring(cur),
        "recommendations": recommendations.track_recommendations(rec_sessions, rows_by),
        "thresholds": thresholds(),
    }


def thresholds() -> dict[str, Any]:
    return {
        "analysis_version": hbuild.ANALYSIS_VERSION,
        "trend_min_laps": trends.MIN_LAPS,
        "lap_min_change_s": trends.LAP_MIN_CHANGE_S,
        "segment_min_change_s": trends.SEGMENT_MIN_CHANGE_S,
        "recent_laps": trends.RECENT_LAPS,
        "weak_min_s": trends.WEAK_MIN_S,
        "pattern_min_laps": patterns.MIN_LAPS,
        "pattern_min_count": patterns.MIN_COUNT,
        "pattern_share": patterns.SHARE,
        "pattern_window": patterns.WINDOW,
        "recommendation_min_laps": recommendations.MIN_LAPS,
        "recommendation_min_count": recommendations.MIN_COUNT,
        "recommendation_share": recommendations.SHARE,
        "evaluation_min_laps": recommendations.EVAL_MIN_LAPS,
    }


def track_history(
    conn: sqlite3.Connection,
    data_dir: Path,
    gid: str,
    *,
    pro_key: str | None = None,
) -> dict[str, Any]:
    """The History page for one track and car class. Raises LookupError when there is no
    history for it; pro comparison errors are reported inside the result."""
    track_key, formula = trends.parse_group_id(gid)
    ensure(conn, data_dir, track_key)
    rows = load_rows(conn, track_key, formula)
    if not rows:
        raise LookupError(f"no history for {gid!r}")
    model = load_model(data_dir, track_key)
    view = track_view(rows, model)
    if pro_key:
        try:
            view["pro"] = pro_corners(conn, data_dir, trends.current_rows(rows, model), pro_key)
        except (LookupError, ValueError, NotComparableError, FetchError, ReferenceDataError) as exc:
            view["pro"] = {"key": pro_key, "error": str(exc)}
    return view


def pro_corners(
    conn: sqlite3.Connection, data_dir: Path, rows: list[dict[str, Any]], pro_key: str
) -> dict[str, Any]:
    """Strongest and weakest corners of your best lap here against a real-F1 lap, after the
    systematic pace difference is taken out (the Driver Benchmark's normalised view)."""
    if not rows:
        raise LookupError("no laps analysed with the current corners")
    best = min(rows, key=lambda r: (r["lap_time_ms"], r["first_utc"] or ""))
    srow = db.get_session(conn, best["session_key"])
    lrow = db.get_lap(conn, best["session_key"], best["lap_index"])
    if srow is None or lrow is None:
        raise LookupError("the best lap is no longer in the catalogue")
    session, lap = dict(srow), dict(lrow)
    ref = load_reference(conn, data_dir, pro_key)
    check_pairing(session, ref)
    trace = load_trace(data_dir, session, lap)
    model = model_for_session(conn, data_dir, session)
    bench = benchmark(trace, place_reference(ref, trace), model)
    corners = []
    for r in bench.corners:
        if r.normalised_s is None or r.normalised_sigma_s is None:
            continue
        need = max(PRO_MIN_IMPACT_S, Z_FINDING * r.normalised_sigma_s)
        if r.normalised_s >= need:
            verdict = "weaker"
        elif -r.normalised_s >= need:
            verdict = "stronger"
        else:
            verdict = "within uncertainty"
        corners.append(
            {
                "segment_id": r.segment.id,
                "segment_name": r.segment.name,
                "normalised_s": round(r.normalised_s, 3),
                "sigma_s": round(r.normalised_sigma_s, 3),
                "verdict": verdict,
            }
        )
    weaker = sorted(
        (c for c in corners if c["verdict"] == "weaker"), key=lambda c: -c["normalised_s"]
    )
    stronger = sorted(
        (c for c in corners if c["verdict"] == "stronger"), key=lambda c: c["normalised_s"]
    )
    return {
        "key": pro_key,
        "driver": ref.driver_name or ref.driver,
        "season": ref.season,
        "event": ref.event,
        "session": ref.session,
        "synthetic": ref.synthetic,
        "lap": {
            "session_key": best["session_key"],
            "lap_index": best["lap_index"],
            "lap_number": best["lap_number"],
            "lap_time_ms": best["lap_time_ms"],
        },
        "ref_lap_time_ms": ref.lap_time_ms,
        "systematic": bench.systematic.get("time"),
        "corners": corners,
        "weakest": weaker[:MAX_PRO_LISTED],
        "strongest": stronger[:MAX_PRO_LISTED],
        "rule": (
            f"a corner is listed when its normalised time is at least {PRO_MIN_IMPACT_S} s and "
            f"{Z_FINDING:g} standard deviations from zero"
        ),
    }


def overview(conn: sqlite3.Connection, data_dir: Path) -> dict[str, Any]:
    """The Dashboard: totals, every track's summary and trend, open recommendations."""
    built = ensure(conn, data_dir)
    rows = load_rows(conn, any_formula=True)
    sessions = [dict(r) for r in db.list_sessions(conn)]
    out_groups = []
    open_recs = []
    for g in trends.groups(rows):
        track_key, formula = trends.parse_group_id(g["id"])
        grow = [r for r in rows if r["track_key"] == track_key and r["formula"] == formula]
        model = load_model(data_dir, track_key)
        cur = trends.current_rows(grow, model)
        sess = trends.by_session(cur)
        trend = trends.lap_trend(cur)
        pat = patterns.recurring(cur)
        latest_recs = recommendations.session_recommendations(sess[-1]) if sess else []
        for rec in latest_recs:
            open_recs.append(
                {
                    **{k: v for k, v in rec.items() if k != "score"},
                    "group": g["id"],
                    "track_name": g["track_name"],
                    "synthetic": g["synthetic"],
                    "session_key": sess[-1][0]["session_key"],
                    "session_date": sess[-1][0]["first_utc"],
                }
            )
        out_groups.append(
            {
                **g,
                "new_best_in_latest": g["sessions"] > 1
                and g["latest_best_lap_ms"] == g["best_lap_ms"],
                "trend": {k: trend.get(k) for k in ("status", "delta", "before", "after")},
                "top_pattern": pat["patterns"][0] if pat["patterns"] else None,
                "series": [
                    {"date": s["date"], "best_lap_ms": s["best_lap_ms"], "median_s": s["median_s"]}
                    for s in trends.session_series(cur)
                ],
            }
        )
    real = [s for s in sessions if not s.get("synthetic")]
    synthetic = [s for s in sessions if s.get("synthetic")]
    # the latest of your own sessions; a synthetic one only when there is none
    latest = max(real or synthetic, key=lambda s: s.get("first_utc") or "", default=None)
    return {
        "totals": {
            "sessions": len(sessions),
            "sessions_with_laps": sum(1 for s in sessions if s.get("usable_laps")),
            "real_sessions": len(real),
            "synthetic_sessions": len(synthetic),
            "usable_laps": sum(s.get("usable_laps") or 0 for s in real),
            "synthetic_usable_laps": sum(s.get("usable_laps") or 0 for s in synthetic),
            "tracks": len({g["track_key"] for g in out_groups if not g["synthetic"]}),
            "synthetic_tracks": len({g["track_key"] for g in out_groups if g["synthetic"]}),
        },
        "built": built,
        "latest_session": None
        if latest is None
        else {
            "session_key": latest["session_key"],
            "date": latest.get("first_utc"),
            "track_name": latest.get("track_name"),
            "label": latest.get("capture_label"),
            "synthetic": bool(latest.get("synthetic")),
            "usable_laps": latest.get("usable_laps"),
            "best_usable_lap_ms": latest.get("best_usable_lap_ms"),
        },
        "groups": out_groups,
        "open_recommendations": open_recs,
        "cache": cache_state(conn),
        "thresholds": thresholds(),
    }
