"""Findings: measured, typed differences between a lap and its reference, corner by corner.

Rules are deterministic. A finding is only created when:

* the measured difference is larger than its threshold (several times the measurement
  resolution: about 1.5 m between frames at 330 km/h, 1 km/h speed steps), and
* it costs at least ``MIN_IMPACT_S`` in the phase where it happens (lock-ups and wheelspin
  are reported as events even when the time cost is small);
* the channels it needs exist in both laps.

Each finding keeps the measurement (``measured``) separate from the ``suggestion``, which is
an incremental target (half the gap first), never a full jump to the reference.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from pitwall.analysis.corners import CornerAnalysis, SegmentResult

MIN_IMPACT_S = 0.020
HIGH_IMPACT_S = 0.050
BRAKE_POINT_M = 6.0
PEAK_BRAKE = 0.10
MIN_SPEED_KMH = 3.0
PICKUP_M = 8.0
HESITATION = 0.30
COASTING_EXTRA_M = 10.0
COASTING_MIN_M = 15.0
EXIT_SPEED_KMH = 4.0
LOCKUP_MIN_M = 3.0
WHEELSPIN_MIN_M = 3.0

TITLES = {
    "EARLY_BRAKE": "Brakes earlier than the reference",
    "LATE_BRAKE": "Brakes later but loses the apex",
    "SOFT_BRAKING": "Softer initial braking",
    "LOW_MIN_SPEED": "Lower minimum speed",
    "LATE_THROTTLE": "Later on the throttle",
    "THROTTLE_HESITATION": "Throttle hesitation on exit",
    "COASTING": "Coasting (no throttle, no brake)",
    "EXIT_DEFICIT": "Slower exit, carried down the straight",
    "LOCKUP": "Front wheel lock-up",
    "WHEELSPIN": "Rear wheelspin on exit",
}


@dataclass
class Finding:
    code: str
    segment_id: str
    segment_name: str
    title: str
    detail: str
    phase: str
    phases: list[str]  # phase losses this finding explains ("carried" = next straight)
    time_impact_s: float
    confidence: str  # "high" | "medium"
    measured: dict[str, Any]
    evidence: dict[str, float]
    suggestion: str | None
    basis: str = "self"  # "self": your own laps (60 Hz); "pro_normalised": real F1 lap

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time_impact_s"] = round(self.time_impact_s, 3)
        return d


def _confidence(size: float, threshold: float, impact: float) -> str:
    return "high" if size >= 2 * threshold and impact >= HIGH_IMPACT_S else "medium"


def _half(x: float) -> int:
    return max(1, round(abs(x) / 2))


def corner_findings(r: SegmentResult) -> list[Finding]:
    """Findings for one corner, by root cause.

    * Entry: softer braking explains an earlier brake point, so it is reported instead of
      "brakes earlier" (whose advice, brake later, would be wrong with low pressure).
    * Exit: a slower exit is a consequence; it is reported on its own only when neither a
      later throttle nor a lower minimum speed explains it.
    * Each finding lists the phases whose time it explains; impacts of findings in the same
      corner can overlap and must not be added up.
    """
    lap, ref, seg = r.lap, r.ref, r.segment
    if lap is None or ref is None:
        return []
    out: list[Finding] = []
    ph = r.phases
    lo, hi = seg.start_m, seg.end_m

    def add(
        code: str,
        detail: str,
        phases: list[str],
        impact: float,
        size: float,
        threshold: float,
        measured: dict[str, Any],
        evidence: tuple[float, float],
        suggestion: str | None,
        confidence: str | None = None,
    ) -> None:
        a, b = sorted(evidence)
        out.append(
            Finding(
                code=code,
                segment_id=seg.id,
                segment_name=seg.name,
                title=TITLES[code],
                detail=detail,
                phase=phases[0],
                phases=phases,
                time_impact_s=impact,
                confidence=confidence or _confidence(size, threshold, impact),
                measured=measured,
                evidence={"start_m": round(a, 1), "end_m": round(b, 1)},
                suggestion=suggestion,
            )
        )

    # ---- entry
    entry_loss = ph.get("approach", 0.0) + ph.get("braking", 0.0)
    early = None
    if lap.brake_onset_m is not None and ref.brake_onset_m is not None:
        early = ref.brake_onset_m - lap.brake_onset_m  # > 0: lap brakes earlier
    soft = None
    if lap.peak_brake is not None and ref.peak_brake is not None:
        soft = ref.peak_brake - lap.peak_brake
    if soft is not None and soft >= PEAK_BRAKE and entry_loss >= MIN_IMPACT_S:
        assert lap.peak_brake is not None and ref.peak_brake is not None
        extra = (
            f" Because of that, braking starts {early:.0f} m earlier."
            if early is not None and early >= BRAKE_POINT_M
            else ""
        )
        add(
            "SOFT_BRAKING",
            f"Peak brake {lap.peak_brake:.0%} vs {ref.peak_brake:.0%}: the car slows more "
            f"gently.{extra}",
            ["approach", "braking"],
            entry_loss,
            soft,
            PEAK_BRAKE,
            {
                "lap": round(lap.peak_brake, 3),
                "ref": round(ref.peak_brake, 3),
                "delta": round(-soft, 3),
                "unit": "brake 0-1",
                "brake_point_delta_m": None if early is None else round(-early, 1),
            },
            (lap.brake_onset_m or lo, lap.brake_release_m or hi),
            "Press the brake trigger harder at the start of the braking zone, then ease off as "
            "the car slows; the brake point can then move later.",
        )
    elif early is not None and early >= BRAKE_POINT_M and entry_loss >= MIN_IMPACT_S:
        assert lap.brake_onset_m is not None and ref.brake_onset_m is not None
        add(
            "EARLY_BRAKE",
            f"Braking starts {early:.0f} m earlier ({lap.brake_onset_m:.0f} m vs "
            f"{ref.brake_onset_m:.0f} m into the lap) with similar brake pressure.",
            ["approach", "braking"],
            entry_loss,
            early,
            BRAKE_POINT_M,
            {
                "lap_m": round(lap.brake_onset_m, 1),
                "ref_m": round(ref.brake_onset_m, 1),
                "delta_m": round(-early, 1),
                "unit": "m",
            },
            (lap.brake_onset_m, ref.brake_onset_m),
            f"Try braking about {_half(early)} m later (half the gap) with the same pressure, "
            "keeping the same apex speed; then check the next lap.",
        )
    lost_apex = (
        lap.min_speed_kmh - ref.min_speed_kmh <= -MIN_SPEED_KMH
        or lap.min_speed_m - ref.min_speed_m >= 10.0
    )
    late_loss = ph.get("mid_corner", 0.0) + ph.get("exit", 0.0)
    if early is not None and -early >= BRAKE_POINT_M and lost_apex and late_loss >= MIN_IMPACT_S:
        assert lap.brake_onset_m is not None and ref.brake_onset_m is not None
        add(
            "LATE_BRAKE",
            f"Braking starts {-early:.0f} m later, but the apex suffers: minimum speed "
            f"{lap.min_speed_kmh:.0f} vs {ref.min_speed_kmh:.0f} km/h.",
            ["mid_corner", "exit"],
            late_loss,
            -early,
            BRAKE_POINT_M,
            {
                "lap_m": round(lap.brake_onset_m, 1),
                "ref_m": round(ref.brake_onset_m, 1),
                "delta_m": round(-early, 1),
                "unit": "m",
            },
            (ref.brake_onset_m, lap.min_speed_m),
            "Brake slightly earlier to make the apex: the late braking costs more after the "
            "apex than it gains before it.",
        )

    # ---- apex and exit
    exit_loss = ph.get("exit", 0.0)
    late_parts: list[tuple[str, float, float, float]] = []  # what, lap_m, ref_m, delay
    if lap.throttle_pickup_m is not None and ref.throttle_pickup_m is not None:
        late_parts.append(
            (
                "pickup",
                lap.throttle_pickup_m,
                ref.throttle_pickup_m,
                lap.throttle_pickup_m - ref.throttle_pickup_m,
            )
        )
    if lap.full_throttle_m is not None and ref.full_throttle_m is not None:
        late_parts.append(
            (
                "full",
                lap.full_throttle_m,
                ref.full_throttle_m,
                lap.full_throttle_m - ref.full_throttle_m,
            )
        )
    worst = max(late_parts, key=lambda p: p[3], default=None)
    late_throttle = (
        worst is not None and worst[3] >= PICKUP_M and exit_loss + r.carried_s >= MIN_IMPACT_S
    )
    if late_throttle and worst is not None:
        what, lap_m, ref_m, delay = worst
        label = "Throttle comes back" if what == "pickup" else "Full throttle comes"
        add(
            "LATE_THROTTLE",
            f"{label} {delay:.0f} m later ({lap_m:.0f} m vs {ref_m:.0f} m into the lap).",
            ["exit", "carried"],
            exit_loss + r.carried_s,
            delay,
            PICKUP_M,
            {
                "lap_m": round(lap_m, 1),
                "ref_m": round(ref_m, 1),
                "delta_m": round(delay, 1),
                "unit": "m",
                "point": "throttle >= 20%" if what == "pickup" else "full throttle",
            },
            (ref_m, lap_m),
            f"Start squeezing the throttle about {_half(delay)} m earlier, as soon as the car "
            "is pointing out of the corner.",
        )
    dmin = lap.min_speed_kmh - ref.min_speed_kmh
    low_impact = ph.get("mid_corner", 0.0) + (0.0 if late_throttle else exit_loss + r.carried_s)
    low_min = dmin <= -MIN_SPEED_KMH and low_impact >= MIN_IMPACT_S
    if low_min:
        add(
            "LOW_MIN_SPEED",
            f"Minimum speed {lap.min_speed_kmh:.0f} vs {ref.min_speed_kmh:.0f} km/h "
            f"({dmin:+.0f} km/h).",
            ["mid_corner"] if late_throttle else ["mid_corner", "exit", "carried"],
            low_impact,
            -dmin,
            MIN_SPEED_KMH,
            {
                "lap_kmh": round(lap.min_speed_kmh, 1),
                "ref_kmh": round(ref.min_speed_kmh, 1),
                "delta_kmh": round(dmin, 1),
                "unit": "km/h",
            },
            (
                min(lap.min_speed_m, ref.min_speed_m) - 30,
                max(lap.min_speed_m, ref.min_speed_m) + 30,
            ),
            f"Aim to carry about {_half(dmin)} km/h more through the apex: release the brake "
            "a little more gradually into the corner.",
        )
    if (
        lap.hesitation >= HESITATION
        and ref.hesitation < HESITATION / 2
        and exit_loss >= MIN_IMPACT_S
    ):
        add(
            "THROTTLE_HESITATION",
            f"The throttle drops by {lap.hesitation:.0%} after it was picked up.",
            ["exit"],
            exit_loss,
            lap.hesitation,
            HESITATION,
            {
                "lap": round(lap.hesitation, 3),
                "ref": round(ref.hesitation, 3),
                "unit": "throttle 0-1",
            },
            (lap.throttle_pickup_m or lo, lap.full_throttle_m or hi),
            "Squeeze the throttle on progressively once you start; avoid lifting again.",
        )
    extra_coast = lap.coasting_m - ref.coasting_m
    if (
        extra_coast >= COASTING_EXTRA_M
        and lap.coasting_m >= COASTING_MIN_M
        and r.loss_s >= MIN_IMPACT_S
    ):
        add(
            "COASTING",
            f"{lap.coasting_m:.0f} m with neither throttle nor brake (reference "
            f"{ref.coasting_m:.0f} m).",
            ["approach", "braking", "mid_corner", "exit"],
            r.loss_s,
            extra_coast,
            COASTING_EXTRA_M,
            {
                "lap_m": round(lap.coasting_m, 1),
                "ref_m": round(ref.coasting_m, 1),
                "delta_m": round(extra_coast, 1),
                "unit": "m",
            },
            (lo, hi),
            "Link braking and throttle: stay on one pedal or the other through the corner.",
        )
    dexit = lap.exit_speed_kmh - ref.exit_speed_kmh
    if (
        not late_throttle
        and not low_min
        and dexit <= -EXIT_SPEED_KMH
        and r.carried_s >= MIN_IMPACT_S
    ):
        add(
            "EXIT_DEFICIT",
            f"Exit speed {lap.exit_speed_kmh:.0f} vs {ref.exit_speed_kmh:.0f} km/h; the deficit "
            f"costs {r.carried_s:.3f} s on the following straight.",
            ["carried"],
            r.carried_s,
            -dexit,
            EXIT_SPEED_KMH,
            {
                "lap_kmh": round(lap.exit_speed_kmh, 1),
                "ref_kmh": round(ref.exit_speed_kmh, 1),
                "delta_kmh": round(dexit, 1),
                "unit": "km/h",
            },
            (hi, hi + 200),
            "A better exit pays off all the way down the next straight: look at the line "
            "and throttle out of this corner.",
        )
    if (
        lap.lockup_m is not None
        and ref.lockup_m is not None
        and lap.lockup_m >= LOCKUP_MIN_M
        and ref.lockup_m < 1.0
    ):
        add(
            "LOCKUP",
            f"A front wheel locks for {lap.lockup_m:.0f} m under braking.",
            ["braking"],
            max(0.0, ph.get("braking", 0.0)),
            lap.lockup_m,
            LOCKUP_MIN_M,
            {"lap_m": round(lap.lockup_m, 1), "ref_m": round(ref.lockup_m, 1), "unit": "m"},
            (lap.brake_onset_m or lo, lap.min_speed_m),
            "Ease the brake trigger off a little earlier as the car slows (no ABS).",
            confidence="high",
        )
    if (
        lap.wheelspin_m is not None
        and ref.wheelspin_m is not None
        and lap.wheelspin_m >= WHEELSPIN_MIN_M
        and ref.wheelspin_m < 1.0
    ):
        add(
            "WHEELSPIN",
            f"A rear wheel spins for {lap.wheelspin_m:.0f} m on the exit.",
            ["exit"],
            max(0.0, exit_loss),
            lap.wheelspin_m,
            WHEELSPIN_MIN_M,
            {"lap_m": round(lap.wheelspin_m, 1), "ref_m": round(ref.wheelspin_m, 1), "unit": "m"},
            (lap.min_speed_m, hi),
            "Feed the throttle in more gently out of the corner (no traction control).",
            confidence="high",
        )
    return out


def lap_findings(analysis: CornerAnalysis) -> list[Finding]:
    """All findings of a lap, biggest time impact first (high confidence first on ties)."""
    findings = [f for r in analysis.corners for f in corner_findings(r)]
    findings.sort(key=lambda f: (-round(f.time_impact_s, 3), f.confidence != "high"))
    return findings
