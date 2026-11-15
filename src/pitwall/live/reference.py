"""References for the live delta: which lap "ahead / behind" is measured against.

* ``personal_best``: your fastest usable lap on this track and car class from the recordings
  already ingested (real and synthetic never mixed; the track length must match).
* ``session_best``: the fastest usable lap of the live session so far.
* ``pro:<key>``: a stored real F1 lap of this event, placed on your track using the path of
  your personal best (or the session's best lap). Its delta is an estimate.

The live delta at lap distance ``d`` is your current lap time minus the reference's time at
``d``. At the line it is computed from the official lap time, so it equals the post-lap delta.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.analysis.benchmark import place_reference
from pitwall.analysis.trace import LapTrace, NotComparableError, load_trace
from pitwall.reference.service import check_pairing, event_for, load_reference, season_for
from pitwall.storage import db

AUTO = "auto"


@dataclass
class LiveReference:
    key: str  # "personal_best", "session_best" or "pro:<ref key>"
    kind: str
    label: str
    lap_time_s: float
    distance: np.ndarray
    time: np.ndarray
    track_length_m: float
    trace: LapTrace | None  # your own lap (for the corner analysis), None for real F1
    estimate: bool = False

    def time_at(self, d: float) -> float:
        return float(np.interp(d, self.distance, self.time))

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "lap_time_ms": round(self.lap_time_s * 1000),
            "estimate": self.estimate,
        }


def from_trace(trace: LapTrace, key: str, kind: str, label: str) -> LiveReference:
    return LiveReference(
        key=key,
        kind=kind,
        label=label,
        lap_time_s=trace.lap_time_s,
        distance=trace.distance,
        time=trace.time,
        track_length_m=trace.track_length_m,
        trace=trace,
    )


def personal_best(
    conn: sqlite3.Connection, data_dir: Path, session: dict[str, Any]
) -> LiveReference | None:
    """Fastest usable ingested lap on this track and car class (same length)."""
    found = db.personal_best_lap(
        conn,
        track_id=session.get("track_id"),
        formula=session.get("formula"),
        synthetic=int(bool(session.get("synthetic"))),
    )
    if found is None:
        return None
    ref_session, lap = dict(found[0]), dict(found[1])
    if ref_session.get("track_length_m") != session.get("track_length_m"):
        return None
    try:
        trace = load_trace(data_dir, ref_session, lap)
    except (NotComparableError, OSError):
        return None
    when = (ref_session.get("first_utc") or "")[:10]
    label = f"personal best (lap {lap['lap_number']}{', ' + when if when else ''})"
    return from_trace(trace, "personal_best", "personal_best", label)


def pro(
    conn: sqlite3.Connection,
    data_dir: Path,
    session: dict[str, Any],
    ref_key: str,
    path: LapTrace,
) -> LiveReference:
    """A stored real F1 lap placed on your track along ``path`` (one of your laps)."""
    ref = load_reference(conn, data_dir, ref_key)
    check_pairing(session, ref)
    placed = place_reference(ref, path)
    return LiveReference(
        key=f"pro:{ref_key}",
        kind="pro",
        label=f"{ref.driver} ({ref.season} {ref.event} {ref.session}"
        f"{' ' + ref.session_part if ref.session_part else ''})",
        lap_time_s=ref.lap_time_s,
        distance=placed.reference.distance,
        time=placed.reference.time,
        track_length_m=path.track_length_m,
        trace=None,
        estimate=True,
    )


def options(conn: sqlite3.Connection, session: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the live delta can be measured against for this session."""
    out = [
        {"key": AUTO, "label": "Automatic: personal best, else this session's best"},
        {"key": "personal_best", "label": "Personal best (ingested laps, this track)"},
        {"key": "session_best", "label": "Best lap of this live session"},
    ]
    if session is None:
        return out
    refs = db.list_ref_laps(conn, synthetic=bool(session.get("synthetic")))
    try:
        event, season = event_for(session), season_for(session)
    except NotComparableError:
        return out
    for r in refs:
        if r["event"] == event and r["season"] == season:
            out.append(
                {
                    "key": f"pro:{r['ref_key']}",
                    "label": f"Real F1: {r['driver']} · {r['session']}"
                    f"{' ' + r['session_part'] if r['session_part'] else ''} (estimate)",
                }
            )
    return out
