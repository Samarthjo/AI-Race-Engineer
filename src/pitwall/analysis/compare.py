"""Compare a lap with a reference lap on the common distance axis.

delta(d) = time of the lap at distance d - time of the reference at d.
Positive = the lap is behind (losing time); its slope shows *where* time goes.

References (all from your own laps, so both sides have full 60 Hz data and the same physics):

* ``session_best``: fastest usable lap of the same session
* ``personal_best``: fastest usable lap on the same track and car class, across sessions
* ``theoretical_best``: your best sector 1 + best sector 2 + best sector 3 from usable laps
  of the session, stitched into one reference trace at the game's sector lines
* ``lap``: any other comparable lap you choose
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.analysis.trace import LapTrace, NotComparableError, load_trace
from pitwall.storage import db

REFERENCE_KINDS = ("session_best", "personal_best", "theoretical_best", "lap")
SMOOTH_M = 30.0  # smoothing window for the time-loss rate
WINDOW_M = 200.0  # stretch length for "largest gains / losses"
MIN_WINDOW_S = 0.010  # smaller changes are not reported (interpolation noise level)


@dataclass
class Reference:
    kind: str
    label: str
    lap_time_s: float
    distance: np.ndarray
    time: np.ndarray
    channels: dict[str, np.ndarray]
    sources: list[dict[str, Any]]  # which lap(s) the reference is built from
    sector_times_ms: tuple[int | None, int | None, int | None]  # official, where known
    official_time_ms: int | None


@dataclass
class Comparison:
    lap: LapTrace
    ref: Reference
    distance: np.ndarray
    delta: np.ndarray  # s, positive = lap slower than the reference so far
    loss_rate: np.ndarray  # s per 100 m (smoothed slope of delta), positive = losing time
    sectors: list[dict[str, Any]]
    largest_losses: list[dict[str, Any]] = field(default_factory=list)
    largest_gains: list[dict[str, Any]] = field(default_factory=list)


def reference_from_trace(trace: LapTrace, kind: str, label: str) -> Reference:
    return Reference(
        kind=kind,
        label=label,
        lap_time_s=trace.lap_time_s,
        distance=trace.distance,
        time=trace.time,
        channels=trace.channels,
        sources=[
            {
                "session_key": trace.session_key,
                "lap_index": trace.lap_index,
                "lap_number": trace.lap_number,
                "sectors": [1, 2, 3],
            }
        ],
        sector_times_ms=trace.official_sectors_ms,
        official_time_ms=round(trace.lap_time_s * 1000),
    )


def stitch_theoretical_best(
    sector_traces: tuple[LapTrace, LapTrace, LapTrace],
    sector_ms: tuple[int, int, int],
    sector_starts_m: tuple[float, float],
) -> Reference:
    """Best S1 + best S2 + best S3 as one trace: each sector keeps its own lap's timing,
    shifted so the sectors follow each other."""
    grid = sector_traces[0].distance
    for tr in sector_traces[1:]:
        if len(tr.distance) != len(grid) or not np.allclose(tr.distance, grid):
            raise NotComparableError("sector laps are on different distance grids")
    bounds = (0.0, sector_starts_m[0], sector_starts_m[1], float(grid[-1]))
    time = np.empty_like(grid)
    channels = {k: np.empty_like(v) for k, v in sector_traces[0].channels.items()}
    offset = 0.0
    for i, tr in enumerate(sector_traces):
        lo, hi = bounds[i], bounds[i + 1]
        sel = (grid >= lo) & (grid <= hi) if i == 2 else (grid >= lo) & (grid < hi)
        t_lo = tr.time_at(lo)
        time[sel] = tr.time[sel] - t_lo + offset
        for k in channels:
            channels[k][sel] = tr.channels[k][sel]
        offset += tr.time_at(hi) - t_lo
    sources = [
        {
            "session_key": tr.session_key,
            "lap_index": tr.lap_index,
            "lap_number": tr.lap_number,
            "sectors": [i + 1],
        }
        for i, tr in enumerate(sector_traces)
    ]
    total_ms = int(sum(sector_ms))
    return Reference(
        kind="theoretical_best",
        label="theoretical best (best sectors)",
        lap_time_s=float(time[-1]),
        distance=grid,
        time=time,
        channels=channels,
        sources=sources,
        sector_times_ms=(sector_ms[0], sector_ms[1], sector_ms[2]),
        official_time_ms=total_ms,
    )


def _windows(delta: np.ndarray, distance: np.ndarray, window_m: float, n: int, sign: int):
    """Largest non-overlapping changes of delta over ``window_m`` (sign +1 losses, -1 gains)."""
    step = float(distance[1] - distance[0]) if len(distance) > 1 else 1.0
    w = max(1, round(window_m / step))
    if len(delta) <= w:
        return []
    change = (delta[w:] - delta[:-w]) * sign
    taken = np.zeros(len(change), dtype=bool)
    out = []
    for i in np.argsort(-change, kind="stable"):
        if len(out) >= n or change[i] < MIN_WINDOW_S:
            break
        lo, hi = max(0, i - w + 1), min(len(change), i + w)
        if taken[lo:hi].any():
            continue
        taken[lo:hi] = True
        out.append(
            {
                "start_m": round(float(distance[i]), 1),
                "end_m": round(float(distance[i + w]), 1),
                "seconds": round(float(change[i]) * sign, 4),
            }
        )
    return out


def compare(lap: LapTrace, ref: Reference) -> Comparison:
    if len(lap.distance) != len(ref.distance) or not np.allclose(lap.distance, ref.distance):
        raise NotComparableError("lap and reference are on different tracks or distance grids")
    distance = lap.distance
    delta = lap.time - ref.time
    step = float(distance[1] - distance[0])
    k = max(1, round(SMOOTH_M / step))
    smooth = np.convolve(
        np.pad(delta, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, "valid"
    )
    loss_rate = np.gradient(smooth, distance) * 100.0

    sectors = []
    if lap.sector_starts_m:
        b = (0.0, lap.sector_starts_m[0], lap.sector_starts_m[1], float(distance[-1]))
        for i in range(3):
            d_lo = float(np.interp(b[i], distance, delta))
            d_hi = float(np.interp(b[i + 1], distance, delta))
            lap_ms, ref_ms = lap.official_sectors_ms[i], ref.sector_times_ms[i]
            # official game sector times when both exist, else measured from the traces
            if lap_ms is not None and ref_ms is not None:
                sector_delta, source = round((lap_ms - ref_ms) / 1000, 3), "official"
            else:
                sector_delta, source = round(d_hi - d_lo, 3), "trace"
            sectors.append(
                {
                    "sector": i + 1,
                    "start_m": round(b[i], 1),
                    "end_m": round(b[i + 1], 1),
                    "lap_ms": lap_ms,
                    "ref_ms": ref_ms,
                    "delta_s": sector_delta,
                    "delta_source": source,
                }
            )
    return Comparison(
        lap=lap,
        ref=ref,
        distance=distance,
        delta=delta,
        loss_rate=loss_rate,
        sectors=sectors,
        largest_losses=_windows(delta, distance, WINDOW_M, 3, +1),
        largest_gains=_windows(delta, distance, WINDOW_M, 3, -1),
    )


# ------------------------------------------------------------------ choosing a reference


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def resolve_reference(
    conn: sqlite3.Connection,
    data_dir: Path,
    session: dict[str, Any],
    kind: str,
    *,
    ref_session_key: str | None = None,
    ref_lap_index: int | None = None,
    step: float = 1.0,
) -> Reference:
    if kind == "session_best":
        lap = _row(db.best_usable_lap(conn, session_key=session["session_key"]))
        if lap is None:
            raise NotComparableError("this session has no usable lap to compare with")
        tr = load_trace(data_dir, session, lap, step)
        return reference_from_trace(tr, kind, f"session best (lap {lap['lap_number']})")
    if kind == "personal_best":
        found = db.personal_best_lap(
            conn,
            track_id=session["track_id"],
            formula=session["formula"],
            synthetic=session["synthetic"],
        )
        if found is None:
            raise NotComparableError("no usable lap on this track yet")
        ref_session, lap = dict(found[0]), dict(found[1])
        tr = load_trace(data_dir, ref_session, lap, step)
        when = (ref_session["first_utc"] or "")[:10]
        return reference_from_trace(tr, kind, f"personal best (lap {lap['lap_number']}, {when})")
    if kind == "theoretical_best":
        best = db.best_sector_laps(conn, session["session_key"])
        if best is None:
            raise NotComparableError("needs usable laps with all three sector times")
        if not (session["sector2_start_m"] and session["sector3_start_m"]):
            raise NotComparableError("sector line positions unknown for this session")
        traces = tuple(load_trace(data_dir, session, dict(lap), step) for lap, _ in best)
        ms = tuple(int(t) for _, t in best)
        return stitch_theoretical_best(
            traces,  # type: ignore[arg-type]
            ms,  # type: ignore[arg-type]
            (session["sector2_start_m"], session["sector3_start_m"]),
        )
    if kind == "lap":
        if ref_session_key is None or ref_lap_index is None:
            raise NotComparableError("choose the reference lap")
        ref_session = _row(db.get_session(conn, ref_session_key))
        lap = _row(db.get_lap(conn, ref_session_key, ref_lap_index))
        if ref_session is None or lap is None:
            raise NotComparableError("reference lap not found")
        if ref_session["track_id"] != session["track_id"]:
            raise NotComparableError("reference lap is on a different track")
        tr = load_trace(data_dir, ref_session, lap, step)
        return reference_from_trace(tr, kind, f"lap {lap['lap_number']}")
    raise NotComparableError(f"unknown reference {kind!r}; choose from {REFERENCE_KINDS}")
