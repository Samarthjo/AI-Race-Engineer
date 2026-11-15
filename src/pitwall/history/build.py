"""The history cache: what was measured on every usable lap, kept so that history stays quick.

For each usable lap of a session: the time spent in every segment of the track model, the
corner metrics of every corner (absolute values: brake point, minimum speed, exit speed...),
and the findings against the session's best lap (what went differently on this lap). Rows
carry the track model's key and version and ``ANALYSIS_VERSION``; when either changes the
session is analysed again. Every session also gets a build state (``ok``, ``no laps`` or
``not comparable`` with the reason), so a session that cannot be analysed is not retried
until something changes. Everything is rebuilt from the recordings, nothing is edited.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from pitwall.analysis.compare import compare, reference_from_trace
from pitwall.analysis.consistency import load_traces
from pitwall.analysis.corners import analyse_corners
from pitwall.analysis.findings import lap_findings
from pitwall.analysis.trace import NotComparableError
from pitwall.storage import db
from pitwall.track.model import TrackModel, load_model
from pitwall.track.service import model_for_session, session_track_key

ANALYSIS_VERSION = "1.0.0"
METRICS = (
    "brake_onset_m",
    "entry_speed_kmh",
    "peak_brake",
    "min_speed_kmh",
    "gear_at_apex",
    "throttle_pickup_m",
    "full_throttle_m",
    "hesitation",
    "coasting_m",
    "exit_speed_kmh",
    "lockup_m",
    "wheelspin_m",
)


def _r(v: Any, nd: int = 3) -> Any:
    return round(v, nd) if isinstance(v, float) else v


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state(
    track_key: str, model: TrackModel | None, laps: int, status: str, reason: str | None
) -> dict[str, Any]:
    return {
        "track_key": track_key,
        "model_version": None if model is None else model.version,
        "analysis_version": ANALYSIS_VERSION,
        "laps": laps,
        "status": status,
        "reason": reason,
        "created_utc": _now(),
    }


def analyse_session(conn: sqlite3.Connection, data_dir: Path, session: dict[str, Any]) -> int:
    """Store the history rows of one session (replacing older ones); returns the lap count."""
    key = session["session_key"]
    tkey = session_track_key(session)
    usable = [dict(r) for r in db.list_laps(conn, key) if r["usable"] and r["lap_time_ms"]]
    traces = load_traces(data_dir, session, usable) if usable else []
    if not traces:
        state = _state(tkey, load_model(data_dir, tkey), 0, "no laps", "no usable lap to analyse")
        db.replace_lap_history(conn, key, [], state)
        return 0
    try:
        model = model_for_session(conn, data_dir, session)
    except NotComparableError as exc:
        state = _state(tkey, load_model(data_dir, tkey), 0, "not comparable", str(exc))
        db.replace_lap_history(conn, key, [], state)
        return 0
    best = min(traces, key=lambda t: (t.lap_time_s, t.lap_index))
    reference = reference_from_trace(best, "session_best", f"session best (lap {best.lap_number})")
    now = _now()
    rows = []
    for tr in traces:
        analysis = analyse_corners(compare(tr, reference), model)
        segments = []
        for r in analysis.segments:
            item: dict[str, Any] = {
                "id": r.segment.id,
                "time_s": round(r.lap_time_s, 4),
                "loss_s": round(r.total_s, 4),
            }
            if r.lap is not None:
                item["metrics"] = {k: _r(getattr(r.lap, k)) for k in METRICS}
            segments.append(item)
        findings = [
            {
                "code": f.code,
                "segment_id": f.segment_id,
                "segment_name": f.segment_name,
                # every lap here is compared with its session's best lap
                "title": f.title.replace("than the reference", "than your best lap"),
                "impact_s": round(f.time_impact_s, 3),
                "confidence": f.confidence,
                "measured": f.measured,
                "suggestion": f.suggestion,
            }
            for f in lap_findings(analysis)
        ]
        rows.append(
            {
                "session_key": key,
                "lap_index": tr.lap_index,
                "lap_number": tr.lap_number,
                "track_key": model.key,
                "model_version": model.version,
                "synthetic": int(bool(session.get("synthetic"))),
                "formula": session.get("formula"),
                "first_utc": session.get("first_utc"),
                "reference_lap": best.lap_index,
                "lap_time_ms": round(tr.lap_time_s * 1000),
                "delta_s": round(float(analysis.lap_delta_s), 4),
                "segments_json": json.dumps(segments, sort_keys=True),
                "findings_json": json.dumps(findings, sort_keys=True),
                "analysis_version": ANALYSIS_VERSION,
                "created_utc": now,
            }
        )
    db.replace_lap_history(conn, key, rows, _state(model.key, model, len(rows), "ok", None))
    return len(rows)


def stale(conn: sqlite3.Connection, data_dir: Path) -> list[dict[str, Any]]:
    """Sessions whose history is missing or was made by older code or another version of the
    track's corners."""
    state = db.history_state(conn)
    models: dict[str, TrackModel | None] = {}
    out = []
    for row in db.list_sessions(conn):
        s = dict(row)
        if not s.get("usable_laps"):
            continue
        tkey = session_track_key(s)
        if tkey not in models:
            models[tkey] = load_model(data_dir, tkey)
        model = models[tkey]
        have = state.get(s["session_key"])
        if (
            have is None
            or have["analysis_version"] != ANALYSIS_VERSION
            or have["model_version"] != (None if model is None else model.version)
        ):
            out.append(s)
    return out


def build(
    conn: sqlite3.Connection,
    data_dir: Path,
    sessions: list[dict[str, Any]] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Analyse the given sessions (default: every stale one; every session with ``force``)."""
    if sessions is None:
        if force:
            sessions = [dict(r) for r in db.list_sessions(conn) if r["usable_laps"]]
        else:
            sessions = stale(conn, data_dir)
    t0 = time.monotonic()
    laps = 0
    for s in sorted(sessions, key=lambda s: (s.get("first_utc") or "", s["session_key"])):
        laps += analyse_session(conn, data_dir, s)
    return {"sessions": len(sessions), "laps": laps, "seconds": round(time.monotonic() - t0, 2)}


def history_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Rows with their JSON columns decoded."""
    out = []
    for r in rows:
        d = dict(r)
        d["segments"] = json.loads(d.pop("segments_json"))
        d["findings"] = json.loads(d.pop("findings_json"))
        out.append(d)
    return out
