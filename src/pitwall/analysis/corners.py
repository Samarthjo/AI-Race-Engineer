"""Corner-by-corner analysis: driving metrics per corner and where the time goes.

All metrics are measured on the lap's distance trace (1 m grid, built from 60 Hz frames),
with fixed, documented thresholds (``DEFINITIONS``). Positions are interpolated to the exact
threshold crossing, so they are accurate to about one frame (1-1.5 m at high speed).

Time accounting (exact by construction):

* every segment (corner or straight) gets ``loss = delta(end) - delta(start)``; over the lap
  these add up to the lap-time difference;
* a corner is split into four **phases** at the *reference* lap's brake onset, brake release
  and throttle pickup (approach, braking, mid-corner, exit); phase losses add up to the
  corner's loss;
* **exit carry**: on the straight after a corner, the time lost while the car is still slower
  than the reference since the exit (speeds not yet within 1 km/h) is credited to that
  corner's exit, not to the straight.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from pitwall.analysis.compare import Comparison
from pitwall.track.model import Segment, TrackModel

BRAKE_ON = 0.10
LIFT_BELOW = 0.90
THROTTLE_PICKUP = 0.20
FULL = 0.98
FULL_HOLD_M = 20.0
PEDAL_OFF = 0.05
STEER_TRAIL = 0.10
LOCK_SLIP = -0.20
SPIN_SLIP = 0.20
SUSTAIN_M = 3.0
EXIT_CARRY_MIN_KMH = 2.0
CONVERGED_KMH = 1.0
LOOKAHEAD_M = 300.0

DEFINITIONS = {
    "brake_onset_m": (
        f"Where braking starts: brake >= {BRAKE_ON:.0%} for at least {SUSTAIN_M:.0f} m"
    ),
    "brake_release_m": f"Where the brake is fully released (back under {BRAKE_ON:.0%})",
    "braking_distance_m": "Distance from brake onset to release",
    "entry_speed_kmh": "Speed where braking (or lifting) starts",
    "peak_brake": "Highest brake application in the braking zone (0-1)",
    "time_to_peak_s": "Time from brake onset to 90 % of the peak",
    "release_rate": "Brake released per 10 m, from the peak to full release (0-1)",
    "trail_brake_m": f"Distance braking while steering more than {STEER_TRAIL:.0%}",
    "lift_m": f"Where the throttle drops under {LIFT_BELOW:.0%} when there is no braking",
    "min_speed_kmh": "Lowest speed in the corner",
    "min_speed_m": "Where the lowest speed is",
    "gear_at_apex": "Gear at the lowest-speed point",
    "throttle_pickup_m": f"First throttle >= {THROTTLE_PICKUP:.0%} after the lowest speed",
    "full_throttle_m": f"Where throttle >= {FULL:.0%} is reached and held for {FULL_HOLD_M:.0f} m",
    "hesitation": "Largest throttle drop between pickup and full throttle (0-1)",
    "coasting_m": f"Distance with throttle and brake both under {PEDAL_OFF:.0%}",
    "exit_speed_kmh": "Speed at the end of the corner segment",
    "lockup_m": f"Distance with a front wheel locking (slip ratio <= {LOCK_SLIP})",
    "wheelspin_m": (
        f"Distance with a rear wheel spinning (slip ratio >= {SPIN_SLIP}) after the apex"
    ),
}


@dataclass
class CornerMetrics:
    brake_onset_m: float | None
    brake_release_m: float | None
    braking_distance_m: float | None
    entry_speed_kmh: float | None
    peak_brake: float | None
    time_to_peak_s: float | None
    release_rate: float | None
    trail_brake_m: float
    lift_m: float | None
    min_speed_kmh: float
    min_speed_m: float
    gear_at_apex: int | None
    throttle_pickup_m: float | None
    full_throttle_m: float | None
    hesitation: float
    coasting_m: float
    exit_speed_kmh: float
    lockup_m: float | None  # None: no wheel-slip data in this lap
    wheelspin_m: float | None


@dataclass
class SegmentResult:
    segment: Segment
    lap_time_s: float  # time spent in the segment
    ref_time_s: float
    loss_s: float  # delta change across the segment (lap minus reference)
    carried_s: float = 0.0  # corners: loss on the next straight caused by the exit
    carried_to: str | None = None
    residual_s: float | None = None  # straights: own loss after the carry is taken out
    phases: dict[str, float] = field(default_factory=dict)
    phase_bounds_m: list[float] = field(default_factory=list)
    lap: CornerMetrics | None = None
    ref: CornerMetrics | None = None

    @property
    def total_s(self) -> float:
        """Loss charged to this segment (corners include the carried exit loss)."""
        if self.segment.is_corner:
            return self.loss_s + self.carried_s
        return self.residual_s if self.residual_s is not None else self.loss_s

    def to_dict(self) -> dict[str, Any]:
        d = {
            "segment": asdict(self.segment),
            "lap_time_s": round(self.lap_time_s, 4),
            "ref_time_s": round(self.ref_time_s, 4),
            "loss_s": round(self.loss_s, 4),
            "carried_s": round(self.carried_s, 4),
            "carried_to": self.carried_to,
            "residual_s": None if self.residual_s is None else round(self.residual_s, 4),
            "total_s": round(self.total_s, 4),
            "phases": {k: round(v, 4) for k, v in self.phases.items()},
            "phase_bounds_m": [round(b, 1) for b in self.phase_bounds_m],
            "lap": _metrics_dict(self.lap),
            "ref": _metrics_dict(self.ref),
        }
        return d


def _metrics_dict(m: CornerMetrics | None) -> dict[str, Any] | None:
    if m is None:
        return None
    out: dict[str, Any] = {}
    for k, v in asdict(m).items():
        out[k] = round(v, 3) if isinstance(v, float) else v
    return out


# ------------------------------------------------------------------------- helpers


def _crossing(d: np.ndarray, y: np.ndarray, i: int, level: float) -> float:
    """Distance where y crosses ``level`` between samples i-1 and i (linear)."""
    if i <= 0:
        return float(d[0])
    y0, y1 = float(y[i - 1]), float(y[i])
    if y1 == y0:
        return float(d[i])
    f = min(1.0, max(0.0, (level - y0) / (y1 - y0)))
    return float(d[i - 1] + f * (d[i] - d[i - 1]))


def _first_sustained(mask: np.ndarray, start: int, stop: int, hold: int) -> int | None:
    for i in range(start, max(start, stop)):
        if mask[i] and mask[i : i + hold].all():
            return i
    return None


def corner_metrics(
    d: np.ndarray, ch: dict[str, np.ndarray], time: np.ndarray, seg: Segment
) -> CornerMetrics:
    step = float(d[1] - d[0])
    n = len(d)
    i0 = int(np.searchsorted(d, seg.start_m, "left"))
    i1 = min(n - 1, int(np.searchsorted(d, seg.end_m, "right")) - 1)
    speed = ch["speed"]
    brake = np.nan_to_num(ch["brake"])
    throttle = np.nan_to_num(ch["throttle"], nan=1.0)
    steer = np.nan_to_num(ch["steer"])
    hold = max(1, round(SUSTAIN_M / step))

    window = speed[i0 : i1 + 1]
    i_min = i0 + int(np.nanargmin(window))
    min_speed = float(speed[i_min])

    # braking zone: first sustained braking before the lowest speed
    on = brake >= BRAKE_ON
    ib = _first_sustained(on, i0, i_min + 1, hold)
    onset = release = peak = ttp = rate = None
    trail = 0.0
    entry = None
    lift = None
    if ib is not None:
        onset = _crossing(d, brake, ib, BRAKE_ON)
        ie = ib
        while ie < n and on[ie]:
            ie += 1
        release = _crossing(d, brake, ie, BRAKE_ON) if ie < n else float(d[-1])
        zone = slice(ib, ie)
        ip = ib + int(np.argmax(brake[zone]))
        peak = float(brake[ip])
        i90 = ib + int(np.argmax(brake[zone] >= 0.9 * peak))
        ttp = float(time[i90] - np.interp(onset, d, time))
        if release > float(d[ip]):
            rate = (peak - BRAKE_ON) / (release - float(d[ip])) * 10.0
        trail = float(np.count_nonzero(np.abs(steer[zone]) >= STEER_TRAIL) * step)
        entry = float(np.interp(onset, d, speed))
    else:
        lifting = throttle < LIFT_BELOW
        il = _first_sustained(lifting, i0, i_min + 1, max(1, round(5.0 / step)))
        if il is not None:
            lift = _crossing(d, throttle, il, LIFT_BELOW)
            entry = float(np.interp(lift, d, speed))

    # throttle back on after the lowest speed (may continue past the segment end)
    stop = min(n, i1 + 1 + round(LOOKAHEAD_M / step))
    pickup = full = None
    hesitation = 0.0
    ipk = next((i for i in range(i_min, stop) if throttle[i] >= THROTTLE_PICKUP), None)
    if ipk is not None:
        pickup = _crossing(d, throttle, ipk, THROTTLE_PICKUP)
        full_hold = max(1, round(FULL_HOLD_M / step))
        ift = _first_sustained(throttle >= FULL, ipk, stop, full_hold)
        if ift is not None:
            full = _crossing(d, throttle, ift, FULL)
            seg_t = throttle[ipk : ift + 1]
            hesitation = float(np.max(np.maximum.accumulate(seg_t) - seg_t)) if len(seg_t) else 0.0

    sl = slice(i0, i1 + 1)
    coasting = float(np.count_nonzero((throttle[sl] < PEDAL_OFF) & (brake[sl] < PEDAL_OFF)) * step)
    gear = ch.get("gear")
    gear_apex = None
    if gear is not None and np.isfinite(gear[i_min]):
        gear_apex = int(gear[i_min])

    lockup = wheelspin = None
    if all(k in ch for k in ("slip_fl", "slip_fr", "slip_rl", "slip_rr")):
        front = np.fmin(ch["slip_fl"], ch["slip_fr"])
        rear = np.fmax(ch["slip_rl"], ch["slip_rr"])
        if np.isfinite(front[sl]).any():
            lockup = float(np.count_nonzero(front[sl] <= LOCK_SLIP) * step)
            wheelspin = float(np.count_nonzero(rear[i_min : i1 + 1] >= SPIN_SLIP) * step)

    return CornerMetrics(
        brake_onset_m=onset,
        brake_release_m=release,
        braking_distance_m=None if onset is None or release is None else release - onset,
        entry_speed_kmh=entry,
        peak_brake=peak,
        time_to_peak_s=ttp,
        release_rate=rate,
        trail_brake_m=trail,
        lift_m=lift,
        min_speed_kmh=min_speed,
        min_speed_m=float(d[i_min]),
        gear_at_apex=gear_apex,
        throttle_pickup_m=pickup,
        full_throttle_m=full,
        hesitation=hesitation,
        coasting_m=coasting,
        exit_speed_kmh=float(speed[i1]),
        lockup_m=lockup,
        wheelspin_m=wheelspin,
    )


@dataclass
class CornerAnalysis:
    model: TrackModel
    segments: list[SegmentResult]
    lap_delta_s: float

    @property
    def corners(self) -> list[SegmentResult]:
        return [r for r in self.segments if r.segment.is_corner]

    def accounted_s(self) -> float:
        return sum(r.total_s for r in self.segments)


def analyse_corners(comparison: Comparison, model: TrackModel) -> CornerAnalysis:
    lap, ref = comparison.lap, comparison.ref
    d = comparison.distance
    delta = comparison.delta

    def delta_at(m: float) -> float:
        return float(np.interp(m, d, delta))

    results: list[SegmentResult] = []
    for seg in model.segments:
        lap_t = float(np.interp(seg.end_m, d, lap.time) - np.interp(seg.start_m, d, lap.time))
        ref_t = float(np.interp(seg.end_m, d, ref.time) - np.interp(seg.start_m, d, ref.time))
        r = SegmentResult(seg, lap_t, ref_t, delta_at(seg.end_m) - delta_at(seg.start_m))
        if seg.is_corner:
            r.lap = corner_metrics(d, lap.channels, lap.time, seg)
            r.ref = corner_metrics(d, ref.channels, ref.time, seg)
            b1 = r.ref.brake_onset_m if r.ref.brake_onset_m is not None else r.ref.lift_m
            b2 = r.ref.brake_release_m if r.ref.brake_release_m is not None else b1
            b3 = r.ref.throttle_pickup_m
            bounds = [seg.start_m]
            for b in (b1, b2, b3):
                v = bounds[-1] if b is None else min(max(b, bounds[-1]), seg.end_m)
                bounds.append(v)
            bounds.append(seg.end_m)
            r.phase_bounds_m = bounds
            names = ("approach", "braking", "mid_corner", "exit")
            r.phases = {
                name: delta_at(bounds[k + 1]) - delta_at(bounds[k]) for k, name in enumerate(names)
            }
        results.append(r)

    # exit carry: straight after a corner
    sp_lap, sp_ref = lap.channels["speed"], ref.channels["speed"]
    for i, r in enumerate(results):
        if r.segment.is_corner:
            continue
        r.residual_s = r.loss_s
        if i == 0 or not results[i - 1].segment.is_corner:
            continue
        a, b = r.segment.start_m, r.segment.end_m
        sel = (d >= a) & (d <= b)
        dv = sp_lap[sel] - sp_ref[sel]
        if not len(dv):
            continue
        n0 = max(1, round(10.0 / float(d[1] - d[0])))
        dv0 = float(np.nanmean(dv[:n0]))
        if abs(dv0) < EXIT_CARRY_MIN_KMH:
            continue
        same_side = dv * np.sign(dv0) > CONVERGED_KMH
        k = int(np.argmin(same_side)) if not same_side.all() else len(dv) - 1
        d_conv = float(d[sel][k])
        carried = delta_at(d_conv) - delta_at(a)
        prev = results[i - 1]
        prev.carried_s = carried
        prev.carried_to = r.segment.id
        r.residual_s = r.loss_s - carried
    return CornerAnalysis(model, results, float(delta[-1]))
