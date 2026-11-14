"""Lap-to-lap consistency per segment, and the session's ideal lap (sum of best segments)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from pitwall.analysis.trace import (
    TRACE_COLUMNS,
    LapTrace,
    NotComparableError,
    build_trace,
    check_comparable,
)
from pitwall.storage import db
from pitwall.track.model import TrackModel

INCONSISTENT_STD_S = 0.10
MIN_LAPS = 3


def load_traces(
    data_dir: Path, session: dict[str, Any], laps: list[dict[str, Any]]
) -> list[LapTrace]:
    """Traces for several laps of one session, reading the frame file only once."""
    path = data_dir / session["frames_path"]
    available = set(pq.read_schema(path).names)
    table = pq.read_table(path, columns=[c for c in TRACE_COLUMNS if c in available])
    lap_index = table["lap_index"].to_numpy()
    s2, s3 = session["sector2_start_m"], session["sector3_start_m"]
    out = []
    for lap in laps:
        try:
            check_comparable(lap)
            rows = table.filter(lap_index == lap["lap_index"])
            out.append(
                build_trace(
                    rows,
                    session_key=session["session_key"],
                    lap_index=lap["lap_index"],
                    lap_number=lap["lap_number"],
                    lap_time_ms=lap["lap_time_ms"],
                    track_length_m=session["track_length_m"] or 0,
                    sector_starts_m=(s2, s3) if s2 and s3 else None,
                    official_sectors_ms=(lap["s1_ms"], lap["s2_ms"], lap["s3_ms"]),
                )
            )
        except NotComparableError:
            continue
    return out


def segment_times(trace: LapTrace, model: TrackModel) -> list[float]:
    d, t = trace.distance, trace.time
    return [float(np.interp(s.end_m, d, t) - np.interp(s.start_m, d, t)) for s in model.segments]


def session_consistency(
    conn: sqlite3.Connection, data_dir: Path, session: dict[str, Any], model: TrackModel
) -> dict[str, Any]:
    laps = [dict(r) for r in db.list_laps(conn, session["session_key"]) if r["usable"]]
    traces = load_traces(data_dir, session, laps)
    result: dict[str, Any] = {
        "laps": len(traces),
        "lap_numbers": [t.lap_number for t in traces],
        "segments": [],
        "ideal_lap_s": None,
        "note": None,
    }
    if len(traces) < 2:
        result["note"] = "Needs at least 2 usable laps in the session."
        return result
    times = np.array([segment_times(t, model) for t in traces])  # laps x segments
    best_idx = times.argmin(axis=0)
    for j, seg in enumerate(model.segments):
        col = times[:, j]
        std = float(col.std(ddof=1))
        result["segments"].append(
            {
                "id": seg.id,
                "name": seg.name,
                "kind": seg.kind,
                "best_s": round(float(col.min()), 4),
                "best_lap_number": traces[int(best_idx[j])].lap_number,
                "mean_s": round(float(col.mean()), 4),
                "worst_s": round(float(col.max()), 4),
                "std_s": round(std, 4),
                "inconsistent": bool(len(col) >= MIN_LAPS and std >= INCONSISTENT_STD_S),
                "times_s": [round(float(x), 4) for x in col],
            }
        )
    result["ideal_lap_s"] = round(float(times.min(axis=0).sum()), 4)
    return result
