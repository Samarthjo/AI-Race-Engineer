"""Corner metrics from sparse samples (real F1 data, about 4 Hz) and their measured error bars.

Real reference data has a car-data sample about every 0.24 s (about 20 m at 300 km/h, and
gaps of up to about 1 s) and an on/off brake. Metrics are estimated from the samples
themselves, without assuming how the pedals moved between two samples:

* brake point, throttle pickup (20 %) and full throttle (98 %, held for 20 m): halfway
  between the last sample before the event and the first one after it; the event can be
  anywhere in between, so its own resolution is the gap divided by sqrt(12) (1 sd);
* minimum speed: the lowest sample (the true minimum can fall between two samples);
* speed at the brake point and at the corner exit: linear interpolation between samples.

How wrong can these estimates be? That is **measured, not assumed**: your own 60 Hz lap is
degraded to the reference's sampling (its real sample intervals, whole km/h, whole-percent
throttle, on/off brake) at many random sampling phases, the same estimators are applied, and
the results are compared with the exact 60 Hz metrics of the same corner. The mean error
(bias) and its spread (standard deviation) become the error bars of the reference's values at
that corner. The runs use a fixed random seed, so results are reproducible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from pitwall.analysis.corners import (
    BRAKE_ON,
    FULL,
    FULL_HOLD_M,
    LOOKAHEAD_M,
    THROTTLE_PICKUP,
    corner_metrics,
)
from pitwall.analysis.trace import LapTrace
from pitwall.track.model import Segment

FIELDS = (
    "brake_onset_m",
    "entry_speed_kmh",
    "min_speed_kmh",
    "throttle_pickup_m",
    "full_throttle_m",
    "exit_speed_kmh",
)
RUNS = 40
SEED = 20250907
MIN_RUNS = 5  # fewer defined estimates: no error bar, so no comparison
BRAKE_SAMPLE_ON = 0.5  # sampled brake is 0 / 1
SQRT12 = 12**0.5  # sd of a uniform position within a gap = gap / sqrt(12)


@dataclass
class SampledMetrics:
    brake_onset_m: float | None
    brake_release_m: float | None
    entry_speed_kmh: float | None
    min_speed_kmh: float | None
    min_speed_m: float | None
    gear_at_apex: int | None
    throttle_pickup_m: float | None
    full_throttle_m: float | None
    exit_speed_kmh: float | None
    # local resolution: spacing of the two samples around each event (m), and the speed
    # change between the two samples around the brake point (km/h)
    gaps_m: dict[str, float] = field(default_factory=dict)
    entry_span_kmh: float | None = None
    notes: list[str] = field(default_factory=list)

    def resolution(self, key: str) -> float | None:
        """1-sd resolution of an estimate from the spacing of its own samples."""
        if key == "entry_speed_kmh":
            return None if self.entry_span_kmh is None else self.entry_span_kmh / SQRT12
        gap = self.gaps_m.get(key)
        return None if gap is None else gap / SQRT12

    def to_dict(self) -> dict[str, Any]:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}


def sampled_corner_metrics(
    d: np.ndarray,
    speed: np.ndarray,
    throttle: np.ndarray,
    brake: np.ndarray,
    gear: np.ndarray,
    seg: Segment,
) -> SampledMetrics:
    """Corner metrics from samples at increasing lap distances ``d`` (metres)."""
    n = len(d)
    notes: list[str] = []
    in_seg = np.flatnonzero((d >= seg.start_m) & (d <= seg.end_m))
    if len(in_seg) == 0:
        return SampledMetrics(None, None, None, None, None, None, None, None, None,
                              notes=["no reference samples in this segment"])  # fmt: skip
    i_min = int(in_seg[np.argmin(speed[in_seg])])
    gear_apex = int(gear[i_min]) if np.isfinite(gear[i_min]) else None
    gaps: dict[str, float] = {}

    def between(i: int, key: str) -> float:
        """Event between samples i-1 and i: the midpoint, keeping the gap."""
        gaps[key] = float(d[i] - d[i - 1])
        return float((d[i - 1] + d[i]) / 2)

    onset = release = entry = span = None
    on = brake >= BRAKE_SAMPLE_ON
    j = next((i for i in range(int(in_seg[0]), i_min + 1) if on[i]), None)
    if j is not None:
        if j == 0 or on[j - 1]:
            notes.append("the reference brakes before this segment starts")
        else:
            onset = between(j, "brake_onset_m")
            entry = float(np.interp(onset, d[j - 1 : j + 1], speed[j - 1 : j + 1]))
            span = float(abs(speed[j - 1] - speed[j]))
            r = j
            while r + 1 < n and on[r + 1]:
                r += 1
            if r + 1 < n:
                release = float((d[r] + d[r + 1]) / 2)

    pickup = full = None
    limit = seg.end_m + LOOKAHEAD_M
    k = next((i for i in range(i_min, n) if d[i] <= limit and throttle[i] >= THROTTLE_PICKUP), None)
    if k is not None and k > 0:
        pickup = between(k, "throttle_pickup_m")
        for i in range(k, n):
            if d[i] > limit:
                break
            if throttle[i] < FULL or i == 0:
                continue
            ahead = (d > d[i]) & (d <= d[i] + FULL_HOLD_M)
            if np.all(throttle[ahead] >= FULL):
                # held full from sample i; it started after the last sample below full
                full = between(i, "full_throttle_m") if throttle[i - 1] < FULL else float(d[i])
                break
    return SampledMetrics(
        brake_onset_m=onset,
        brake_release_m=release,
        entry_speed_kmh=entry,
        min_speed_kmh=float(speed[i_min]),
        min_speed_m=float(d[i_min]),
        gear_at_apex=gear_apex,
        throttle_pickup_m=pickup,
        full_throttle_m=full,
        exit_speed_kmh=float(np.interp(seg.end_m, d, speed)),
        gaps_m=gaps,
        entry_span_kmh=span,
        notes=notes,
    )


# ------------------------------------------------------------------- degradation


@dataclass
class DegradedLap:
    d: np.ndarray
    t: np.ndarray
    speed: np.ndarray
    throttle: np.ndarray
    brake: np.ndarray
    gear: np.ndarray


def degrade(trace: LapTrace, intervals: np.ndarray, phase_s: float, offset: int) -> DegradedLap:
    """Your lap as a real-F1 data feed would record it: samples at the reference's own
    intervals (starting ``offset`` into them, ``phase_s`` after the line), whole km/h,
    whole-percent throttle, brake on/off at the brake-point threshold."""
    iv = np.asarray(intervals, dtype=np.float64)
    iv = iv[iv > 0]
    if len(iv) == 0:
        raise ValueError("no sample intervals")
    n = int(trace.lap_time_s / iv.min()) + 2
    steps = np.resize(np.roll(iv, -offset), n)
    t = phase_s + np.concatenate([[0.0], np.cumsum(steps)])
    t = t[t <= trace.lap_time_s]
    d = np.interp(t, trace.time, trace.distance)
    ch = trace.channels
    grid = trace.distance
    idx = np.clip(np.searchsorted(grid, d, side="right") - 1, 0, len(grid) - 1)
    throttle = np.interp(d, grid, np.nan_to_num(ch["throttle"]))
    brake = np.interp(d, grid, np.nan_to_num(ch["brake"]))
    return DegradedLap(
        d=d,
        t=t,
        speed=np.round(np.interp(d, grid, ch["speed"])),
        throttle=np.round(throttle * 100) / 100,
        brake=(brake >= BRAKE_ON).astype(float),
        gear=np.asarray(ch["gear"], dtype=np.float64)[idx],
    )


@dataclass(frozen=True)
class ErrorStats:
    bias: float  # mean of (estimate - exact)
    sd: float  # spread of the error (1 standard deviation)
    n: int  # runs with both values
    detected: float  # share of runs where the estimate exists when the exact value does

    def to_dict(self) -> dict[str, Any]:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}


def error_bars(
    trace: LapTrace,
    segments: list[Segment],
    intervals: np.ndarray,
    runs: int = RUNS,
    seed: int = SEED,
) -> dict[str, dict[str, ErrorStats]]:
    """Measured estimator errors per corner and metric (see the module docstring)."""
    corners = [s for s in segments if s.is_corner]
    truth = {s.id: corner_metrics(trace.distance, trace.channels, trace.time, s) for s in corners}
    errors: dict[str, dict[str, list[float]]] = {s.id: {f: [] for f in FIELDS} for s in corners}
    totals: dict[str, dict[str, int]] = {s.id: dict.fromkeys(FIELDS, 0) for s in corners}
    iv = np.asarray(intervals, dtype=np.float64)
    iv = iv[iv > 0]
    rng = np.random.default_rng(seed)
    for _ in range(runs):
        offset = int(rng.integers(len(iv)))
        lap = degrade(trace, iv, float(rng.uniform(0, iv[offset])), offset)
        for s in corners:
            est = sampled_corner_metrics(lap.d, lap.speed, lap.throttle, lap.brake, lap.gear, s)
            exact = truth[s.id]
            for f in FIELDS:
                tv = getattr(exact, f)
                if tv is None:
                    continue
                totals[s.id][f] += 1
                ev = getattr(est, f)
                if ev is not None:
                    errors[s.id][f].append(ev - tv)
    out: dict[str, dict[str, ErrorStats]] = {}
    for s in corners:
        out[s.id] = {}
        for f in FIELDS:
            e = np.asarray(errors[s.id][f])
            if len(e) < MIN_RUNS:
                continue
            out[s.id][f] = ErrorStats(
                bias=float(np.mean(e)),
                sd=float(np.std(e, ddof=1)),
                n=len(e),
                detected=len(e) / max(1, totals[s.id][f]),
            )
    return out
