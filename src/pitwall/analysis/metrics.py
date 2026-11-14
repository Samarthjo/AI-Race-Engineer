"""Lap overview metrics, measured directly from the frames of one lap.

Every metric has a written definition (``DEFINITIONS``) so a number can always be traced
back to how it was measured. Shares of the lap are time-weighted: each frame counts for the
time until the next frame (the last frame: until the official lap time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from pitwall.analysis.trace import read_lap_frames
from pitwall.f1udp.f125 import WHEELS

FULL_THROTTLE = 0.98
BRAKE_ON = 0.05
PEDAL_OFF = 0.05

DEFINITIONS: dict[str, str] = {
    "top_speed_kmh": "Highest speed in the lap (km/h)",
    "avg_speed_kmh": "Track length divided by lap time (km/h)",
    "full_throttle_pct": f"Share of lap time with throttle >= {FULL_THROTTLE:.0%}",
    "braking_pct": f"Share of lap time with brake >= {BRAKE_ON:.0%}",
    "coasting_pct": f"Share of lap time with throttle and brake both below {PEDAL_OFF:.0%}",
    "gear_changes": "Number of gear changes",
    "max_rpm": "Highest engine RPM",
    "drs_open_pct": "Share of lap time with DRS open",
    "fuel_used_kg": "Fuel mass at the lap start minus at the lap end (kg)",
    "ers_deployed_mj": "ERS energy deployed this lap, game counter at lap end (MJ)",
    "ers_harvested_mj": "ERS energy harvested this lap (MGU-K + MGU-H), at lap end (MJ)",
    "tyre_surface_temp_c": "Average tyre surface temperature per wheel (RL, RR, FL, FR), °C",
    "brake_temp_max_c": "Highest brake temperature of any wheel (°C)",
}

METRIC_COLUMNS = [
    "current_lap_time_in_ms",
    "speed",
    "throttle",
    "brake",
    "gear",
    "engine_rpm",
    "drs",
    "fuel_in_tank",
    "ers_deployed_this_lap",
    "ers_harvested_this_lap_mguk",
    "ers_harvested_this_lap_mguh",
    *(f"tyres_surface_temperature_{w}" for w in WHEELS),
    *(f"brakes_temperature_{w}" for w in WHEELS),
]


def _col(table: pa.Table, name: str) -> np.ndarray:
    if name not in table.column_names:
        return np.full(table.num_rows, np.nan)
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), dtype=np.float64)


def _nanmax(a: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    return float(a.max()) if len(a) else None


def _nanmean(a: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    return float(a.mean()) if len(a) else None


def _r(x: float | None, nd: int = 2) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    return round(float(x), nd)


def lap_metrics(table: pa.Table, lap_time_s: float, track_length_m: float) -> dict[str, Any]:
    t = _col(table, "current_lap_time_in_ms") / 1000.0
    order = np.argsort(t, kind="stable")
    t = t[order]

    def c(name: str) -> np.ndarray:
        return _col(table, name)[order]

    dt = np.diff(np.append(t, lap_time_s))
    dt = np.clip(dt, 0.0, None)
    total = float(dt.sum()) or 1.0

    def share(mask: np.ndarray) -> float | None:
        mask = mask & np.isfinite(dt)
        return _r(100.0 * float(dt[mask].sum()) / total, 1)

    throttle, brake = c("throttle"), c("brake")
    gear = c("gear")
    g = gear[np.isfinite(gear)]
    fuel = c("fuel_in_tank")
    fuel = fuel[np.isfinite(fuel)]
    ers_dep = c("ers_deployed_this_lap")
    ers_k = c("ers_harvested_this_lap_mguk")
    ers_h = c("ers_harvested_this_lap_mguh")
    temps = [c(f"tyres_surface_temperature_{w}") for w in WHEELS]
    brakes = np.concatenate([c(f"brakes_temperature_{w}") for w in WHEELS])

    def last(a: np.ndarray) -> float | None:
        a = a[np.isfinite(a)]
        return float(a[-1]) if len(a) else None

    dep, hk, hh = last(ers_dep), last(ers_k), last(ers_h)
    return {
        "top_speed_kmh": _r(_nanmax(c("speed")), 1),
        "avg_speed_kmh": _r(track_length_m / lap_time_s * 3.6, 1) if lap_time_s > 0 else None,
        "full_throttle_pct": share(throttle >= FULL_THROTTLE),
        "braking_pct": share(brake >= BRAKE_ON),
        "coasting_pct": share((throttle < PEDAL_OFF) & (brake < PEDAL_OFF)),
        "gear_changes": int(np.count_nonzero(np.diff(g))) if len(g) > 1 else None,
        "max_rpm": _r(_nanmax(c("engine_rpm")), 0),
        "drs_open_pct": share(c("drs") >= 1),
        "fuel_used_kg": _r(fuel[0] - fuel[-1], 3) if len(fuel) > 1 else None,
        "ers_deployed_mj": _r(dep / 1e6, 3) if dep is not None else None,
        "ers_harvested_mj": _r((hk + hh) / 1e6, 3) if hk is not None and hh is not None else None,
        "tyre_surface_temp_c": [_r(_nanmean(x), 1) for x in temps],
        "brake_temp_max_c": _r(_nanmax(brakes), 0),
    }


def load_lap_metrics(
    data_dir: Path, session: dict[str, Any], lap: dict[str, Any]
) -> dict[str, Any]:
    frames = read_lap_frames(
        data_dir / session["frames_path"], lap["lap_index"], ["lap_index", *METRIC_COLUMNS]
    )
    return lap_metrics(frames, lap["lap_time_ms"] / 1000.0, float(session["track_length_m"] or 0))
