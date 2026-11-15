"""Synthetic "real F1" reference data in the archive's own format. Never real data.

For tests and the demo: laps of the synthetic driver model, recorded the way the real F1 feed
records a car, so the whole reference pipeline (download, checks, normalisation,
registration, benchmark) runs on data with known answers:

* car data (speed, rpm, gear, throttle, on/off brake, DRS) about every 0.24 s with jitter and
  a few longer gaps, as whole numbers;
* position data (x, y, z in 1/10 m) on its own clock, in a "circuit frame": the game frame
  mirrored, rotated, scaled and shifted by a known :class:`FrameTransform`;
* both streams merged on one time axis with interpolation, as FastF1 does, with the lap
  starting on a start line ``start_offset_m`` after the game's.

Everything is labelled synthetic: provider "synthetic", the event name, driver names, and
every stored row (``synthetic = 1``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.reference.service import SYNTHETIC_EVENT
from pitwall.synth.track import (
    CarModel,
    DriverProfile,
    LapProfile,
    Track,
    build_track,
    simulate_lap,
)

SESSION = "Qualifying"
CAR_INTERVAL_S = 0.24
JITTER_S = 0.03
GAP_EVERY = 70  # one longer gap (0.6-0.95 s) about every N samples, as in real data
REAL_BRAKE_ON = 0.05  # brake "on" in the synthetic feed
ELEVATION_M = 120.0


@dataclass(frozen=True)
class FrameTransform:
    """Game frame -> synthetic circuit frame (metres)."""

    reflected: bool = True
    rotation_deg: float = 37.0
    scale: float = 1.0
    tx: float = 2500.0
    ty: float = -800.0

    def game_to_real(self, x: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        p = np.asarray(x, dtype=np.float64) + 1j * np.asarray(z, dtype=np.float64)
        a = math.radians(self.rotation_deg)
        q = self.scale * complex(math.cos(a), math.sin(a)) * p + complex(self.tx, self.ty)
        if self.reflected:
            q = np.conj(q)
        return np.real(q), np.imag(q)


@dataclass(frozen=True)
class SyntheticDriver:
    code: str
    first: str
    last: str
    team: str
    number: str
    driver: DriverProfile = field(default_factory=DriverProfile)
    car: CarModel = field(default_factory=CarModel)


DEFAULT_DRIVERS = (
    SyntheticDriver("PRO", "Synthetic", "Pro", "Synthetic Racing", "98"),
    SyntheticDriver(
        "FST",
        "Synthetic",
        "Fast",
        "Synthetic Racing",
        "99",
        car=CarModel(a_lat=3.1 * 9.81, a_brake=42.0, v_max=94.0, v_top=97.0),
    ),
)


def _sample_times(lap_time: float, rng: np.random.Generator) -> np.ndarray:
    n = int(lap_time / (CAR_INTERVAL_S - JITTER_S)) + 4
    steps = CAR_INTERVAL_S + rng.uniform(-JITTER_S, JITTER_S, n)
    gaps = np.arange(GAP_EVERY // 2, n, GAP_EVERY)
    steps[gaps] = rng.uniform(0.6, 0.95, len(gaps))
    t = rng.uniform(0.0, CAR_INTERVAL_S) + np.concatenate([[0.0], np.cumsum(steps)])
    t = np.round(t, 3)
    return t[(t > 0) & (t < lap_time)]


def _lap_distance(profile: LapProfile, start_m: float, tau: np.ndarray) -> np.ndarray:
    """Game lap distance at time ``tau`` since crossing ``start_m``."""
    t0 = profile.time_at(start_m)
    return profile.distance_at_time((t0 + tau) % profile.lap_time)


def _interp(t: np.ndarray, ts: np.ndarray, vs: np.ndarray) -> np.ndarray:
    """Linear interpolation, extrapolated beyond the first and last sample."""
    out = np.interp(t, ts, vs)
    lo, hi = t < ts[0], t > ts[-1]
    out[lo] = vs[0] + (t[lo] - ts[0]) * (vs[1] - vs[0]) / (ts[1] - ts[0])
    out[hi] = vs[-1] + (t[hi] - ts[-1]) * (vs[-1] - vs[-2]) / (ts[-1] - ts[-2])
    return out


def _merge(
    car_t: np.ndarray, car: dict[str, np.ndarray], pos_t: np.ndarray, pos: dict[str, np.ndarray],
    lap_time: float,
) -> dict[str, Any]:  # fmt: skip
    """One time axis, each stream's gaps filled like FastF1's merged telemetry (the lap's
    first and last rows are extrapolated, so they are not measured samples)."""
    t = np.unique(np.concatenate([[0.0], car_t, pos_t, [round(lap_time, 3)]]))
    out: dict[str, Any] = {"time": [round(float(v), 3) for v in t]}
    for k in ("speed", "rpm", "throttle"):
        out[k] = [float(v) for v in _interp(t, car_t, car[k].astype(np.float64))]
    for k in ("gear", "brake", "drs"):
        idx = np.clip(np.searchsorted(car_t, t, side="right") - 1, 0, len(car_t) - 1)
        out[k] = [int(v) for v in car[k][idx]]
    for k in ("x", "y", "z"):
        out[k] = [float(v) for v in _interp(t, pos_t, pos[k].astype(np.float64))]
    v = np.asarray(out["speed"]) / 3.6
    dist = np.concatenate([[0.0], np.cumsum((v[1:] + v[:-1]) / 2 * np.diff(t))])
    out["distance"] = [float(x) for x in dist]
    return out


def lap_telemetry(
    profile: LapProfile,
    transform: FrameTransform,
    start_offset_m: float,
    rng: np.random.Generator,
) -> dict[str, Any]:
    """One flying lap in the archive's ``<lap>_tel.json`` layout (the ``tel`` object)."""
    lap_time = profile.lap_time
    length = profile.track.length
    start = start_offset_m % length
    car_t = _sample_times(lap_time, rng)
    pos_t = _sample_times(lap_time, rng)
    ch = profile.at(_lap_distance(profile, start, car_t))
    car = {
        "speed": np.round(ch["speed"] * 3.6),
        "rpm": np.round(ch["rpm"] + rng.normal(0.0, 25.0, len(car_t))),  # real rpm is noisy
        "throttle": np.round(ch["throttle"] * 100),
        "gear": ch["gear"].astype(int),
        "brake": (ch["brake"] >= REAL_BRAKE_ON).astype(int),
        "drs": np.zeros(len(car_t), dtype=int),
    }
    gp = profile.at(_lap_distance(profile, start, pos_t))
    rx, ry = transform.game_to_real(gp["x"], gp["z"])
    pos = {
        "x": np.round(rx * 10),
        "y": np.round(ry * 10),
        "z": np.full(len(pos_t), float(round(ELEVATION_M * 10))),
    }
    return _merge(car_t, car, pos_t, pos, lap_time)


def _columns(rows: list[dict]) -> dict[str, list]:
    keys = list(rows[0])
    return {k: [r[k] for r in rows] for k in keys}


def _lap_rows(d: SyntheticDriver, profile: LapProfile, start_m: float) -> list[dict]:
    """Out lap, the flying lap (2), in lap, and a faster lap deleted for track limits."""
    lap_time = round(profile.lap_time, 3)
    length = profile.track.length
    marks = [start_m + length / 3, start_m + 2 * length / 3]
    t0 = profile.time_at(start_m % length)

    def since_start(d_m: float) -> float:
        return (profile.time_at(d_m % length) - t0) % profile.lap_time

    s1 = round(since_start(marks[0]), 3)
    s2 = round(since_start(marks[1]) - since_start(marks[0]), 3)
    s3 = round(lap_time - s1 - s2, 3)
    base = {
        "compound": "SOFT", "stint": 1, "life": 1, "pos": "None", "status": "1",
        "drv": d.code, "dNum": d.number, "team": d.team, "del": False, "delR": "None",
        "ff1G": False, "qs": "Q1",
    }  # fmt: skip
    return [
        {**base, "lap": 1, "time": "None", "s1": "None", "s2": "None", "s3": "None",
         "pb": False, "pout": 900.0, "pin": "None", "iacc": False},
        {**base, "lap": 2, "time": lap_time, "s1": s1, "s2": s2, "s3": s3, "life": 2,
         "pb": True, "pout": "None", "pin": "None", "iacc": True},
        {**base, "lap": 3, "time": "None", "s1": "None", "s2": "None", "s3": "None",
         "life": 3, "pb": False, "pout": "None", "pin": 1100.0, "iacc": False},
        {**base, "lap": 4, "time": round(lap_time - 0.3, 3), "s1": "None", "s2": "None",
         "s3": "None", "life": 4, "pb": False, "pout": "None", "pin": "None", "iacc": True,
         "del": True, "delR": "TRACK LIMITS (synthetic)"},
    ]  # fmt: skip


def write_archive(
    root: Path,
    drivers: tuple[SyntheticDriver, ...] = DEFAULT_DRIVERS,
    *,
    track: Track | None = None,
    season: int = 2025,
    event: str = SYNTHETIC_EVENT,
    session: str = SESSION,
    transform: FrameTransform | None = None,
    start_offset_m: float = 12.0,
    seed: int = 7,
) -> dict[str, str]:
    """Write a synthetic archive under ``root``; returns the base and schedule URLs to read
    it with (``file://``)."""
    track = track or build_track()
    transform = transform or FrameTransform()
    rng = np.random.default_rng(seed)
    sdir = root / str(season) / event / session
    sdir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    for d in drivers:
        profile = simulate_lap(track, d.driver, d.car)
        rows = _lap_rows(d, profile, start_offset_m)
        all_rows += rows
        ddir = sdir / d.code
        ddir.mkdir(exist_ok=True)
        (ddir / "laptimes.json").write_text(json.dumps(_columns(rows)))
        tel = lap_telemetry(profile, transform, start_offset_m, rng)
        tel["dataKey"] = f"{season}-{event}-{session}-{d.code}-2 (synthetic)"
        (ddir / "2_tel.json").write_text(json.dumps({"tel": tel}))
    (sdir / "session_laptimes.json").write_text(json.dumps(_columns(all_rows)))
    (sdir / "drivers.json").write_text(
        json.dumps(
            {
                "drivers": [
                    {
                        "driver": d.code,
                        "team": d.team,
                        "dn": d.number,
                        "fn": d.first,
                        "ln": d.last,
                        "tc": "888888",
                        "url": None,
                    }
                    for d in drivers
                ]
            }
        )
    )
    apex = [c.apex_m for c in track.corners]
    ax, ay = transform.game_to_real(
        np.interp(apex, track.s, track.x), np.interp(apex, track.s, track.z)
    )
    (sdir / "corners.json").write_text(
        json.dumps(
            {
                "CornerNumber": list(range(1, len(apex) + 1)),
                "X": [float(v * 10) for v in ax],
                "Y": [float(v * 10) for v in ay],
                "Distance": [float((a - start_offset_m) % track.length) for a in apex],
                "CircuitName": "Synthetic test track",
                "synthetic": True,
            }
        )
    )
    schedule: dict[str, dict[str, Any]] = {
        "round_number": {"0": 1},
        "country": {"0": "Synthetic"},
        "location": {"0": "Synthetic test track"},
        "event_name": {"0": event},
        "event_date": {"0": f"{season}-06-01T14:00:00"},
        "event_format": {"0": "conventional"},
        **{f"session{i}": {"0": None} for i in range(1, 6)},
    }
    schedule["session4"] = {"0": session}
    (root / f"schedule_{season}.json").write_text(json.dumps(schedule))
    return {
        "base_url": root.resolve().as_uri() + "/{season}/",
        "schedule_url": root.resolve().as_uri() + "/schedule_{season}.json",
    }
