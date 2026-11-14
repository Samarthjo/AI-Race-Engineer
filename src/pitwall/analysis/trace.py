"""Lap traces on a common distance axis.

A trace samples a lap's channels every ``step`` metres, from the start line (0 m) to the
track length. Time comes from the game's own lap timer (``currentLapTimeInMS``), anchored at
0 s on the line and at the official lap time at the finish line. So the lap time, and the
delta at the line, are exact by construction. In between, values are interpolated linearly
between frames (about 1.5 m apart at 330 km/h and 60 Hz). Gear and DRS are steps, not
interpolated.

Only complete flying laps (or race laps) that start at the line can be compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# trace channel name, frame-table column, interpolation
CHANNELS: tuple[tuple[str, str, str], ...] = (
    ("speed", "speed", "linear"),  # km/h
    ("throttle", "throttle", "linear"),  # 0..1
    ("brake", "brake", "linear"),  # 0..1
    ("steer", "steer", "linear"),  # -1 (left) .. 1 (right)
    ("gear", "gear", "step"),  # 1-8, 0 = N, -1 = R
    ("rpm", "engine_rpm", "linear"),
    ("drs", "drs", "step"),  # 0 / 1
    ("x", "world_position_x", "linear"),  # metres, game world
    ("z", "world_position_z", "linear"),
    # Motion Ex wheel slip ratio: negative = wheel slower than the car (locking under brakes),
    # positive = wheel faster (spinning under power).
    ("slip_rl", "wheel_slip_ratio_rl", "linear"),
    ("slip_rr", "wheel_slip_ratio_rr", "linear"),
    ("slip_fl", "wheel_slip_ratio_fl", "linear"),
    ("slip_fr", "wheel_slip_ratio_fr", "linear"),
)
TRACE_COLUMNS = ["lap_index", "lap_distance", "current_lap_time_in_ms"] + [
    c for _, c, _ in CHANNELS
]
COMPARABLE_KINDS = ("flying", "on_track")
MAX_START_DISTANCE_M = 50.0


class NotComparableError(ValueError):
    """The lap cannot be placed on the common distance axis (out-lap, incomplete, ...)."""


@dataclass
class LapTrace:
    session_key: str
    lap_index: int
    lap_number: int
    lap_time_s: float
    track_length_m: float
    distance: np.ndarray  # metres from the start line
    time: np.ndarray  # seconds since the start line, at each distance
    channels: dict[str, np.ndarray]
    sector_starts_m: tuple[float, float] | None  # where sector 2 and sector 3 start
    official_sectors_ms: tuple[int | None, int | None, int | None]
    max_gap_m: float  # longest stretch without a frame
    samples: int

    @property
    def label(self) -> str:
        return f"lap {self.lap_number}"

    def time_at(self, d: float) -> float:
        return float(np.interp(d, self.distance, self.time))


def _col(table: pa.Table, name: str) -> np.ndarray:
    """Column as float64 (nulls -> NaN); a missing optional channel is all NaN."""
    if name not in table.column_names:
        return np.full(table.num_rows, np.nan)
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), dtype=np.float64)


def build_trace(
    table: pa.Table,
    *,
    session_key: str,
    lap_index: int,
    lap_number: int,
    lap_time_ms: int,
    track_length_m: float,
    sector_starts_m: tuple[float, float] | None = None,
    official_sectors_ms: tuple[int | None, int | None, int | None] = (None, None, None),
    step: float = 1.0,
) -> LapTrace:
    """Resample one lap's frames (rows of the frame table) onto the distance grid."""
    if track_length_m <= 0:
        raise NotComparableError("track length unknown")
    length = float(track_length_m)
    lap_time = lap_time_ms / 1000.0
    d = _col(table, "lap_distance")
    t = _col(table, "current_lap_time_in_ms") / 1000.0
    ok = np.isfinite(d) & np.isfinite(t) & (d >= 0) & (d <= length)
    if ok.sum() < 10:
        raise NotComparableError("too few frames on the lap")
    raw = {name: _col(table, col) for name, col, _ in CHANNELS}
    order = np.argsort(t[ok], kind="stable")
    d, t = d[ok][order], t[ok][order]
    raw = {k: v[ok][order] for k, v in raw.items()}

    # Distance must strictly increase along the lap (drop stationary / repeated samples).
    prev_max = np.maximum.accumulate(np.concatenate([[-np.inf], d[:-1]]))
    inc = d > prev_max
    d, t = d[inc], t[inc]
    raw = {k: v[inc] for k, v in raw.items()}
    if d[0] > MAX_START_DISTANCE_M:
        raise NotComparableError(f"lap data starts {d[0]:.0f} m after the line")

    # Anchor the official start (0 m, 0 s) and finish (track length, lap time).
    if d[0] > 0:
        d = np.concatenate([[0.0], d])
        t = np.concatenate([[0.0], t])
        raw = {k: np.concatenate([v[:1], v]) for k, v in raw.items()}
    if d[-1] < length:
        d = np.concatenate([d, [length]])
        t = np.concatenate([t, [lap_time]])
        raw = {k: np.concatenate([v, v[-1:]]) for k, v in raw.items()}
    if np.any(np.diff(t) < 0):
        raise NotComparableError("lap timer goes backwards inside the lap")

    grid = np.arange(0.0, length + step / 2, step)
    if grid[-1] < length:
        grid = np.append(grid, length)
    grid[-1] = length
    channels: dict[str, np.ndarray] = {}
    for name, _, kind in CHANNELS:
        v = raw[name]
        valid = np.isfinite(v)
        if valid.sum() < 2:
            channels[name] = np.full(len(grid), np.nan)
            continue
        dv, vv = d[valid], v[valid]
        if kind == "step":
            idx = np.clip(np.searchsorted(dv, grid, side="right") - 1, 0, len(dv) - 1)
            channels[name] = vv[idx]
        else:
            channels[name] = np.interp(grid, dv, vv)
    return LapTrace(
        session_key=session_key,
        lap_index=lap_index,
        lap_number=lap_number,
        lap_time_s=lap_time,
        track_length_m=length,
        distance=grid,
        time=np.interp(grid, d, t),
        channels=channels,
        sector_starts_m=sector_starts_m,
        official_sectors_ms=official_sectors_ms,
        max_gap_m=float(np.max(np.diff(d))),
        samples=len(d),
    )


def check_comparable(lap: dict[str, Any]) -> None:
    if not lap["complete"] or not lap["lap_time_ms"]:
        raise NotComparableError("lap is not complete")
    if lap["kind"] not in COMPARABLE_KINDS:
        raise NotComparableError(f"{lap['kind']} laps start or end in the pits")
    if lap["start_distance_m"] is not None and lap["start_distance_m"] > MAX_START_DISTANCE_M:
        raise NotComparableError("lap does not start at the line")


def read_lap_frames(frames_path: Path, lap_index: int, columns: list[str]) -> pa.Table:
    """One lap's rows; columns the file does not have (e.g. no Motion Ex data) are skipped."""
    available = set(pq.read_schema(frames_path).names)
    cols = [c for c in columns if c in available]
    return pq.read_table(frames_path, columns=cols, filters=[("lap_index", "=", lap_index)])


def load_trace(
    data_dir: Path, session: dict[str, Any], lap: dict[str, Any], step: float = 1.0
) -> LapTrace:
    """Trace for a lap row of the catalogue (session/lap as dict-like rows)."""
    check_comparable(lap)
    frames = read_lap_frames(data_dir / session["frames_path"], lap["lap_index"], TRACE_COLUMNS)
    s2, s3 = session["sector2_start_m"], session["sector3_start_m"]
    return build_trace(
        frames,
        session_key=session["session_key"],
        lap_index=lap["lap_index"],
        lap_number=lap["lap_number"],
        lap_time_ms=lap["lap_time_ms"],
        track_length_m=session["track_length_m"] or 0,
        sector_starts_m=(s2, s3) if s2 and s3 else None,
        official_sectors_ms=(lap["s1_ms"], lap["s2_ms"], lap["s3_ms"]),
        step=step,
    )
