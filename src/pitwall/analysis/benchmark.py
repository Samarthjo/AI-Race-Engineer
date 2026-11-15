"""Driver benchmark: your lap against a real Formula 1 lap (Stage 4).

1. **Place** the reference on your track (``track.registration``): every real sample gets a
   lap distance on your lap's distance axis.
2. **Reference trace** on your distance grid, starting at *your* start line: the real lap is
   shifted cyclically by the start-line offset (the part before your line comes from the end
   of the real lap), so its lap time is unchanged.
3. **Delta, sectors and segment losses** exactly as for your own laps; sector deltas are
   measured on your game's sector lines (the real sector lines may differ).
4. **Corner metrics of the reference** are estimated from its samples
   (``analysis.sampled``), corrected by the sampling bias measured on your own lap at the
   same corner, with error bars: measured sampling error, plus the along-track uncertainty
   of the registration for positions.
5. **Physics-normalised view.** F1 25 is not real life, and a real 2025 car is not the
   game's car. The median difference over all corners (for times: the median time ratio,
   separately for braking corners and for straights) is taken as *systematic*; what remains,
   corner by corner, is the driver-specific part.
6. **Findings** only where the normalised difference is larger than its threshold *and*
   than two standard deviations of its uncertainty, and the corner loses time beyond the
   systematic pace difference. Comparisons the reference data cannot support (brake pressure,
   steering, wheel slip, coasting) are listed as not assessed, never guessed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from pitwall.analysis.compare import Comparison, Reference, compare
from pitwall.analysis.corners import CornerMetrics, corner_metrics
from pitwall.analysis.findings import Finding
from pitwall.analysis.sampled import (
    FIELDS,
    ErrorStats,
    SampledMetrics,
    error_bars,
    sampled_corner_metrics,
)
from pitwall.analysis.trace import CHANNELS, LapTrace, NotComparableError
from pitwall.reference.laps import RefLap
from pitwall.track.model import Segment, TrackModel
from pitwall.track.registration import Placement, RegistrationError, register

PRO_MIN_IMPACT_S = 0.050  # normalised corner loss needed for a finding
PRO_BRAKE_M = 10.0
PRO_MIN_SPEED_KMH = 4.0
PRO_THROTTLE_M = 12.0
PRO_EXIT_KMH = 5.0
Z_FINDING = 2.0  # findings need |normalised| >= 2 sigma
Z_HIGH = 3.0
MIN_BASELINE = 3  # values needed to estimate a systematic offset (median)
USER_RESOLUTION_M = 1.0  # your own position metrics (60 Hz frames, 1 m grid)
SPEED_FLOOR_KMH = 0.5  # whole-km/h speeds
TIME_FLOOR_S = 0.005
MEDIAN_SE = 1.2533  # standard error of a median: 1.2533 * sigma / sqrt(n)
MAD_TO_SD = 1.4826
BRAKING_KINDS = ("corner", "complex")

METRICS: dict[str, tuple[str, str]] = {
    "brake_onset_m": ("Brake point", "m"),
    "entry_speed_kmh": ("Speed at the brake point", "km/h"),
    "min_speed_kmh": ("Minimum speed", "km/h"),
    "throttle_pickup_m": ("Throttle pickup (20 %)", "m"),
    "full_throttle_m": ("Full throttle", "m"),
    "exit_speed_kmh": ("Exit speed", "km/h"),
}
NOT_ASSESSED_EXTRA = (
    "exit carry down the next straight (a real car accelerates differently from the game's)"
)


def _confidence(z: float | None) -> str:
    if z is None:
        return "n/a"
    if z >= Z_HIGH:
        return "high"
    if z >= Z_FINDING:
        return "medium"
    return "within uncertainty"


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


# ---------------------------------------------------------------- placing the reference


@dataclass
class ProSamples:
    """Reference car samples on your lap's distance axis (0..L from your line, sorted)."""

    d: np.ndarray
    t: np.ndarray  # s since the reference crossed *your* start line
    speed: np.ndarray
    throttle: np.ndarray
    brake: np.ndarray
    gear: np.ndarray
    rpm: np.ndarray
    drs: np.ndarray


@dataclass
class PlacedReference:
    ref: RefLap
    placement: Placement
    samples: ProSamples
    reference: Reference  # on your lap's distance grid
    along_sigma_m: float  # along-track uncertainty of mapped positions (1 sd)


def _wrap(
    g: np.ndarray, t: np.ndarray, d0: float, length: float, lap_time: float, tau_c: float
) -> tuple[np.ndarray, np.ndarray]:
    """Unwrapped distance/time since the real line -> distance/time since your line."""
    c = length if d0 > 0 else 0.0
    after = g >= c
    return (
        np.where(after, g - c, g - c + length),
        np.where(after, t - tau_c, t - tau_c + lap_time),
    )


def _step(xp: np.ndarray, fp: np.ndarray, x: np.ndarray) -> np.ndarray:
    idx = np.clip(np.searchsorted(xp, x, side="right") - 1, 0, len(xp) - 1)
    return fp[idx]


def place_reference(ref: RefLap, lap: LapTrace) -> PlacedReference:
    s = ref.samples
    try:
        placement = register(lap.distance, lap.channels["x"], lap.channels["z"], s.pos_t, s.x, s.y)
    except RegistrationError as exc:
        raise NotComparableError(f"cannot place the reference on your track: {exc}") from exc
    reg = placement.registration
    if reg.quality == "poor":
        raise NotComparableError(
            "cannot place the reference on your track: " + "; ".join(reg.notes)
        )
    length, lap_time, d0 = lap.track_length_m, ref.lap_time_s, reg.start_offset_m

    # time against unwrapped distance, from the position samples; start and end anchored
    tt = np.concatenate([[0.0], placement.pos_t, [lap_time]])
    gg = np.concatenate([[d0], placement.pos_d, [d0 + length]])
    inside = (tt > 0) & (tt < lap_time)
    inside[0] = inside[-1] = True
    tt = tt[inside]
    gg = np.clip(np.maximum.accumulate(gg[inside]), d0, d0 + length)
    tau_c = float(np.interp(length if d0 > 0 else 0.0, gg, tt))
    gw, tw = _wrap(gg[:-1], tt[:-1], d0, length, lap_time, tau_c)  # the end = the start
    order = np.argsort(gw, kind="stable")
    gw = np.concatenate([[0.0], gw[order], [length]])
    tw = np.concatenate([[0.0], tw[order], [lap_time]])
    tw = np.maximum.accumulate(tw)

    gc = np.clip(placement.distance_at(s.car_t), d0, d0 + length)
    dc, tc = _wrap(gc, s.car_t, d0, length, lap_time, tau_c)
    oc = np.argsort(dc, kind="stable")
    samples = ProSamples(
        d=dc[oc],
        t=tc[oc],
        speed=s.speed[oc],
        throttle=s.throttle[oc],
        brake=s.brake[oc],
        gear=s.gear[oc],
        rpm=s.rpm[oc],
        drs=s.drs[oc],
    )
    dp, _ = _wrap(placement.pos_d, placement.pos_t, d0, length, lap_time, tau_c)
    op = np.argsort(dp, kind="stable")

    grid = lap.distance
    channels: dict[str, np.ndarray] = {name: np.full(len(grid), np.nan) for name, _, _ in CHANNELS}
    for name in ("speed", "throttle", "rpm"):
        channels[name] = np.interp(grid, samples.d, getattr(samples, name))
    for name in ("brake", "gear", "drs"):
        channels[name] = _step(samples.d, getattr(samples, name), grid)
    channels["x"] = np.interp(grid, dp[op], placement.gx[op])
    channels["z"] = np.interp(grid, dp[op], placement.gz[op])
    reference = Reference(
        kind="pro",
        label=ref.label,
        lap_time_s=lap_time,
        distance=grid,
        time=np.interp(grid, gw, tw),
        channels=channels,
        sources=[{"ref_key": ref.key, "driver": ref.driver, "lap_number": ref.lap_number}],
        sector_times_ms=(None, None, None),
        official_time_ms=ref.lap_time_ms,
    )
    return PlacedReference(
        ref=ref,
        placement=placement,
        samples=samples,
        reference=reference,
        along_sigma_m=max(USER_RESOLUTION_M, reg.median_m),
    )


# ------------------------------------------------------------------------- results


@dataclass
class MetricComparison:
    key: str
    label: str
    unit: str
    lap: float | None
    ref: float | None  # reference estimate, corrected for the measured sampling bias
    ref_raw: float | None  # straight from the samples
    ref_sigma: float | None  # 1 sd of the reference estimate
    delta: float | None  # lap - ref
    offset: float | None = None  # systematic part (median over corners) at this corner
    normalised: float | None = None  # delta - offset
    sigma: float | None = None  # 1 sd of the normalised value (of the delta before that)
    z: float | None = None
    confidence: str = "n/a"
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = _r(v, 2)
        return d


@dataclass
class SegmentBenchmark:
    segment: Segment
    lap_time_s: float
    ref_time_s: float
    loss_s: float  # measured: lap minus reference over the segment
    loss_sigma_s: float
    expected_s: float | None = None  # systematic pace difference over this segment
    normalised_s: float | None = None
    normalised_sigma_s: float | None = None
    z: float | None = None
    confidence: str = "n/a"
    metrics: dict[str, MetricComparison] = field(default_factory=dict)
    lap_metrics: CornerMetrics | None = None
    ref_sampled: SampledMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment": asdict(self.segment),
            "lap_time_s": _r(self.lap_time_s, 4),
            "ref_time_s": _r(self.ref_time_s, 4),
            "loss_s": _r(self.loss_s, 4),
            "loss_sigma_s": _r(self.loss_sigma_s, 4),
            "expected_s": _r(self.expected_s, 4),
            "normalised_s": _r(self.normalised_s, 4),
            "normalised_sigma_s": _r(self.normalised_sigma_s, 4),
            "z": _r(self.z, 2),
            "confidence": self.confidence,
            "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            "ref_notes": self.ref_sampled.notes if self.ref_sampled else [],
        }


@dataclass
class Benchmark:
    placed: PlacedReference
    comparison: Comparison
    segments: list[SegmentBenchmark]
    systematic: dict[str, Any]
    findings: list[Finding]
    strengths: list[dict[str, Any]]
    not_assessed: list[str]
    error_bars: dict[str, dict[str, ErrorStats]]

    @property
    def corners(self) -> list[SegmentBenchmark]:
        return [r for r in self.segments if r.segment.is_corner]

    def ref_sector_times(self) -> list[float] | None:
        """Reference time in each of *your* game's sectors (from its trace)."""
        lap = self.comparison.lap
        if not lap.sector_starts_m:
            return None
        ref = self.placed.reference
        b = (0.0, lap.sector_starts_m[0], lap.sector_starts_m[1], float(ref.distance[-1]))
        t = np.interp(b, ref.distance, ref.time)
        return [round(float(t[i + 1] - t[i]), 3) for i in range(3)]


# ----------------------------------------------------------------------- analysis


def _segment_rows(
    lap: LapTrace,
    placed: PlacedReference,
    comparison: Comparison,
    model: TrackModel,
    bars: dict[str, dict[str, ErrorStats]],
) -> list[SegmentBenchmark]:
    d, delta = comparison.distance, comparison.delta
    ref = placed.reference
    sig_d = placed.along_sigma_m
    s = placed.samples
    rows = []
    for seg in model.segments:
        a, b = seg.start_m, seg.end_m
        lap_t = float(np.interp(b, d, lap.time) - np.interp(a, d, lap.time))
        ref_t = float(np.interp(b, d, ref.time) - np.interp(a, d, ref.time))
        loss = float(np.interp(b, d, delta) - np.interp(a, d, delta))
        va = max(float(np.interp(a, d, ref.channels["speed"])) / 3.6, 5.0)
        vb = max(float(np.interp(b, d, ref.channels["speed"])) / 3.6, 5.0)
        sigma_t = max(TIME_FLOOR_S, math.hypot(sig_d / va, sig_d / vb))
        row = SegmentBenchmark(seg, lap_t, ref_t, loss, sigma_t)
        if seg.is_corner:
            lm = corner_metrics(d, lap.channels, lap.time, seg)
            sm = sampled_corner_metrics(s.d, s.speed, s.throttle, s.brake, s.gear, seg)
            row.lap_metrics, row.ref_sampled = lm, sm
            # exit speed is read at a fixed distance: add the registration error times the
            # speed gradient there (from your lap)
            i_end = min(len(d) - 1, int(np.searchsorted(d, b)))
            slope = abs(float(np.gradient(lap.channels["speed"], d)[i_end]))
            for key in FIELDS:
                label, unit = METRICS[key]
                lv, rv = getattr(lm, key), getattr(sm, key)
                st = bars.get(seg.id, {}).get(key)
                m = MetricComparison(key, label, unit, lv, None, rv, None, None)
                if rv is not None and st is not None:
                    # sampling error: measured on your lap, or this sample's own spacing
                    sd = max(st.sd, sm.resolution(key) or 0.0)
                    m.ref = rv - st.bias
                    if unit == "m":
                        m.ref_sigma = math.hypot(sd, sig_d)
                    elif key == "exit_speed_kmh":
                        m.ref_sigma = max(SPEED_FLOOR_KMH, math.hypot(sd, slope * sig_d))
                    else:
                        m.ref_sigma = max(SPEED_FLOOR_KMH, sd)
                elif rv is not None:
                    m.note = "no error bar here (rarely measurable at the reference's sampling)"
                if lv is not None and m.ref is not None and m.ref_sigma is not None:
                    m.delta = lv - m.ref
                    m.sigma = math.hypot(m.ref_sigma, USER_RESOLUTION_M if unit == "m" else 0.0)
                row.metrics[key] = m
        rows.append(row)
    return rows


def _normalise_times(rows: list[SegmentBenchmark]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    groups = {
        "braking_corners": [r for r in rows if r.segment.kind in BRAKING_KINDS],
        "straights": [r for r in rows if r.segment.kind not in BRAKING_KINDS],
    }
    for name, group in groups.items():
        ratios = np.array([r.lap_time_s / r.ref_time_s for r in group if r.ref_time_s > 0])
        if len(ratios) < MIN_BASELINE:
            out[name] = {"segments": len(ratios), "pace_ratio": None}
            continue
        ratio = float(np.median(ratios))
        mad = float(np.median(np.abs(ratios - ratio)))
        sigma_ratio = MEDIAN_SE * MAD_TO_SD * mad / math.sqrt(len(ratios))
        out[name] = {
            "segments": len(ratios),
            "pace_ratio": round(ratio, 5),
            "pace_pct": round(100 * (ratio - 1), 2),
        }
        for r in group:
            r.expected_s = (ratio - 1) * r.ref_time_s
            r.normalised_s = r.loss_s - r.expected_s
            r.normalised_sigma_s = math.hypot(r.loss_sigma_s, sigma_ratio * r.ref_time_s)
            r.z = abs(r.normalised_s) / r.normalised_sigma_s
            r.confidence = _confidence(r.z)
    return out


def _normalise_metrics(rows: list[SegmentBenchmark]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    corners = [r for r in rows if r.segment.is_corner]
    for key in FIELDS:
        unit = METRICS[key][1]
        vals = [
            r.metrics[key] for r in corners if key in r.metrics and r.metrics[key].delta is not None
        ]
        if len(vals) < MIN_BASELINE:
            out[key] = {"offset": None, "unit": "m" if unit == "m" else "%", "corners": len(vals)}
            continue
        deltas = np.array([m.delta for m in vals], dtype=np.float64)
        sigmas = np.array([m.sigma for m in vals], dtype=np.float64)
        refs = np.array([m.ref for m in vals], dtype=np.float64)
        if unit == "m":
            off = float(np.median(deltas))
            se = MEDIAN_SE * math.sqrt(float(np.mean(sigmas**2)) / len(vals))
            for m in vals:
                assert m.delta is not None and m.sigma is not None
                m.offset = off
                m.normalised = m.delta - off
                m.sigma = math.hypot(m.sigma, se)
            out[key] = {"offset": round(off, 1), "unit": "m", "corners": len(vals)}
        else:
            off_pct = float(np.median(100 * deltas / refs))
            se_pct = MEDIAN_SE * math.sqrt(float(np.mean((100 * sigmas / refs) ** 2)) / len(vals))
            for m in vals:
                assert m.delta is not None and m.sigma is not None and m.ref is not None
                m.offset = off_pct / 100 * m.ref
                m.normalised = m.delta - m.offset
                m.sigma = math.hypot(m.sigma, se_pct / 100 * m.ref)
            out[key] = {"offset": round(off_pct, 2), "unit": "%", "corners": len(vals)}
        for m in vals:
            assert m.normalised is not None and m.sigma is not None
            m.z = abs(m.normalised) / m.sigma if m.sigma > 0 else None
            m.confidence = _confidence(m.z)
    return out


def _significant(m: MetricComparison | None, threshold: float, sign: int) -> bool:
    if m is None or m.normalised is None or m.sigma is None:
        return False
    return sign * m.normalised >= max(threshold, Z_FINDING * m.sigma)


@dataclass
class _Corner:
    """What every finding of one corner shares."""

    seg: Segment
    impact: float  # normalised time loss of the corner (s)
    time_z: float
    ref: RefLap

    def finding(
        self,
        code: str,
        title: str,
        detail: str,
        phases: list[str],
        m: MetricComparison,
        evidence: tuple[float, float],
        suggestion: str,
    ) -> Finding:
        a, b = sorted(evidence)
        z = m.z or 0.0
        return Finding(
            code=code,
            segment_id=self.seg.id,
            segment_name=self.seg.name,
            title=title,
            detail=detail,
            phase=phases[0],
            phases=phases,
            time_impact_s=self.impact,
            confidence="high" if z >= Z_HIGH and self.time_z >= Z_HIGH else "medium",
            measured={
                "lap": _r(m.lap, 1),
                "ref": _r(m.ref, 1),
                "delta": _r(m.delta, 1),
                "systematic": _r(m.offset, 1),
                "normalised": _r(m.normalised, 1),
                "sigma": _r(m.sigma, 1),
                "unit": m.unit,
                "reference": self.ref.key,
            },
            evidence={"start_m": round(a, 1), "end_m": round(b, 1)},
            suggestion=suggestion,
            basis="pro_normalised",
        )


def _corner_findings(r: SegmentBenchmark, ref: RefLap) -> list[Finding]:
    seg, who = r.segment, ref.driver
    if not seg.is_corner or r.normalised_s is None or r.normalised_sigma_s is None:
        return []
    impact = r.normalised_s
    if impact < max(PRO_MIN_IMPACT_S, Z_FINDING * r.normalised_sigma_s):
        return []
    c = _Corner(seg, impact, impact / r.normalised_sigma_s, ref)
    out: list[Finding] = []
    explained = False  # a slower exit is reported only when nothing else explains it

    brake = r.metrics.get("brake_onset_m")
    if brake is not None and _significant(brake, PRO_BRAKE_M, -1):
        assert brake.normalised is not None and brake.offset is not None
        usual = (
            f"{-brake.offset:.0f} m earlier" if brake.offset < 0 else f"{brake.offset:.0f} m later"
        )
        out.append(
            c.finding(
                "EARLY_BRAKE",
                f"Brakes earlier than {who}, beyond your usual offset",
                f"Braking starts {-(brake.delta or 0.0):.0f} m before {who}'s brake point "
                f"(± {brake.sigma:.0f} m); across the lap you brake {usual} than {who}, so "
                f"this corner is {-brake.normalised:.0f} m earlier than your norm. The "
                "reference brake is on/off only, so a softer first application cannot be told "
                "apart from an earlier one.",
                ["approach", "braking"],
                brake,
                (brake.lap or seg.start_m, brake.ref or seg.start_m),
                f"Brake about {max(1, round(-brake.normalised / 2))} m later here (half the "
                "extra gap), then check the next lap.",
            )
        )
    vmin = r.metrics.get("min_speed_kmh")
    if vmin is not None and _significant(vmin, PRO_MIN_SPEED_KMH, -1):
        assert vmin.normalised is not None
        explained = True
        out.append(
            c.finding(
                "LOW_MIN_SPEED",
                f"Lower minimum speed than {who}, beyond your usual difference",
                f"Minimum speed {vmin.lap:.0f} vs about {vmin.ref:.0f} km/h; after the "
                f"systematic difference this corner is {-vmin.normalised:.0f} km/h "
                f"(± {vmin.sigma:.0f}) slower than your norm.",
                ["mid_corner"],
                vmin,
                (seg.start_m, seg.end_m),
                f"Aim to carry about {max(1, round(-vmin.normalised / 2))} km/h more through "
                "the apex.",
            )
        )
    late: list[MetricComparison] = []
    for key in ("throttle_pickup_m", "full_throttle_m"):
        m = r.metrics.get(key)
        if m is not None and _significant(m, PRO_THROTTLE_M, +1):
            late.append(m)
    if late:
        m = max(late, key=lambda x: x.z or 0.0)
        assert m.normalised is not None
        explained = True
        what = "Throttle pickup" if m.key == "throttle_pickup_m" else "Full throttle"
        out.append(
            c.finding(
                "LATE_THROTTLE",
                f"Later on the throttle than {who}, beyond your usual offset",
                f"{what} {m.delta:+.0f} m relative to {who} (± {m.sigma:.0f} m); this corner "
                f"is {m.normalised:.0f} m later than your norm.",
                ["exit"],
                m,
                (m.ref or seg.end_m, m.lap or seg.end_m),
                f"Start squeezing the throttle about {max(1, round(m.normalised / 2))} m "
                "earlier out of this corner.",
            )
        )
    vexit = r.metrics.get("exit_speed_kmh")
    if not explained and vexit is not None and _significant(vexit, PRO_EXIT_KMH, -1):
        assert vexit.normalised is not None
        out.append(
            c.finding(
                "EXIT_DEFICIT",
                f"Slower out of the corner than {who}, beyond your usual difference",
                f"Exit speed {vexit.lap:.0f} vs about {vexit.ref:.0f} km/h; after the "
                f"systematic difference this exit is {-vexit.normalised:.0f} km/h "
                f"(± {vexit.sigma:.0f}) slower than your norm. The reference samples are too "
                "far apart here to tell whether the throttle came later or the apex was slower.",
                ["exit"],
                vexit,
                (seg.end_m - 60, seg.end_m),
                "Look at your throttle application out of this corner on the charts: the exit "
                "speed carries down the next straight.",
            )
        )
    return out


def _findings(rows: list[SegmentBenchmark], ref: RefLap) -> list[Finding]:
    out = [f for r in rows for f in _corner_findings(r, ref)]
    out.sort(key=lambda f: (-round(f.time_impact_s, 3), f.confidence != "high"))
    return out


def benchmark(lap: LapTrace, placed: PlacedReference, model: TrackModel) -> Benchmark:
    comparison = compare(lap, placed.reference)
    bars = error_bars(lap, model.segments, np.diff(placed.ref.samples.car_t))
    rows = _segment_rows(lap, placed, comparison, model, bars)
    systematic = {"time": _normalise_times(rows), "metrics": _normalise_metrics(rows)}
    findings = _findings(rows, placed.ref)
    strengths = [
        {
            "segment_id": r.segment.id,
            "segment_name": r.segment.name,
            "normalised_s": _r(r.normalised_s, 3),
            "sigma_s": _r(r.normalised_sigma_s, 3),
        }
        for r in rows
        if r.segment.is_corner
        and r.normalised_s is not None
        and r.normalised_sigma_s is not None
        and -r.normalised_s >= max(PRO_MIN_IMPACT_S, Z_FINDING * r.normalised_sigma_s)
    ]
    not_assessed = [*placed.ref.capabilities.not_comparable(), NOT_ASSESSED_EXTRA]
    return Benchmark(
        placed=placed,
        comparison=comparison,
        segments=rows,
        systematic=systematic,
        findings=findings,
        strengths=strengths,
        not_assessed=not_assessed,
        error_bars=bars,
    )
