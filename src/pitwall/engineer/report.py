"""The deterministic engineer report (5a): no AI, only measured facts and fixed rules.

It is the ground truth for everything the AI may say, and it is useful on its own:

* **headline**: lap time and delta to the reference;
* **focus for the next lap**: at most three findings, ranked by time impact x confidence
  (one per corner; findings from your own laps weigh more than those against real F1 data,
  which are estimates); each keeps the measurement and the suggestion apart;
* **where the time goes**: the segments that lose most, with the findings that explain them;
* **strengths**, **consistency** over the session, **notes** on the data (draft corners, an
  unclean lap, no findings, what real-F1 data cannot show);
* **history at this track** (Stage 7): the driver profile up to this session (trend,
  recurring patterns, how the previous session's recommendations worked out, where the most
  time is left); a focus item that is a recurring pattern points to it.

When the data cannot support a recommendation the report says so instead of guessing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from pitwall.analysis.benchmark import Benchmark
from pitwall.analysis.compare import Comparison, Reference
from pitwall.analysis.corners import CornerAnalysis
from pitwall.analysis.findings import Finding
from pitwall.engineer.facts import Fact, fmt_lap, fmt_s
from pitwall.f1udp.enums import BRAKING_ASSIST, GEARBOX_ASSIST, RACING_LINE, TRACTION_CONTROL
from pitwall.track.model import TrackModel

REPORT_VERSION = "report-2"
MAX_FOCUS = 3
MAX_LOSSES = 5
LOSS_MIN_S = 0.020  # a segment listed under "where the time goes"
STRENGTH_MIN_S = 0.050  # a segment listed as a strength (faster than the reference)
SEGMENT_FACT_MIN_S = 0.010
WEIGHTS = {
    ("self", "high"): 1.0,
    ("self", "medium"): 0.7,
    ("pro_normalised", "high"): 0.6,
    ("pro_normalised", "medium"): 0.45,
}


@dataclass
class FocusItem:
    rank: int
    fact_id: str
    segment_id: str
    segment_name: str
    title: str
    instruction: str  # what to try (the finding's suggestion)
    measured: str  # what was measured (the finding's detail)
    impact_s: float
    confidence: str
    basis: str
    recurring: str | None = None  # history fact id when this is a recurring pattern


@dataclass
class LossItem:
    fact_id: str
    segment_id: str
    segment_name: str
    loss_s: float
    causes: list[str]  # finding fact ids in this segment
    cause_titles: list[str]


@dataclass
class EngineerReport:
    version: str
    headline: str
    session: dict[str, Any]
    lap: dict[str, Any]
    reference: dict[str, Any]
    delta_s: float
    facts: list[Fact]
    focus: list[FocusItem]
    losses: list[LossItem]
    strengths: list[str]
    consistency: list[str]
    notes: list[str]
    segment_names: list[str]
    lap_numbers: list[int]
    pro: dict[str, Any] | None = None
    history_facts: list[str] = field(default_factory=list)  # ids of the hist:* facts
    _index: dict[str, Fact] = field(default_factory=dict, repr=False)

    def fact(self, fact_id: str) -> Fact:
        if not self._index:
            self._index = {f.id: f for f in self.facts}
        return self._index[fact_id]

    def facts_by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "headline": self.headline,
            "session": self.session,
            "lap": self.lap,
            "reference": self.reference,
            "delta_s": round(self.delta_s, 3),
            "facts": [f.to_dict() for f in self.facts],
            "focus": [asdict(f) | {"impact_s": round(f.impact_s, 3)} for f in self.focus],
            "losses": [asdict(x) | {"loss_s": round(x.loss_s, 3)} for x in self.losses],
            "strengths": self.strengths,
            "consistency": self.consistency,
            "notes": self.notes,
            "history_facts": self.history_facts,
            "pro": self.pro,
        }


# ------------------------------------------------------------------------- facts


def _finding_fact(f: Finding, prefix: str) -> Fact:
    if f.basis == "pro_normalised":
        cost = f"Costs about {fmt_s(f.time_impact_s)} beyond the systematic pace difference"
    elif f.time_impact_s > 0.0005:
        cost = f"Costs {fmt_s(f.time_impact_s)}"
    else:
        cost = "Measured as an event (no separate time cost)"
    text = f"{f.segment_name}: {f.title}. {f.detail} {cost} ({f.confidence} confidence)." + (
        f" Suggestion: {f.suggestion}" if f.suggestion else ""
    )
    return Fact(
        id=f"{prefix}:{f.segment_id}:{f.code}",
        kind="finding",
        text=text,
        segment_id=f.segment_id,
        segment_name=f.segment_name,
        basis=f.basis,
        estimate=f.basis != "self",
        code=f.code,
        data={
            "impact_s": round(f.time_impact_s, 3),
            "confidence": f.confidence,
            "measured": f.measured,
            "evidence": f.evidence,
            "phases": f.phases,
            "title": f.title,
            "detail": f.detail,
            "suggestion": f.suggestion,
        },
    )


def _assists_fact(lap: dict[str, Any]) -> Fact | None:
    raw = lap.get("assists_json")
    if not raw:
        return None
    a = json.loads(raw)
    parts = []
    if "anti_lock_brakes" in a:
        parts.append(f"ABS {'on' if a['anti_lock_brakes'] else 'off'}")
    if "traction_control" in a:
        parts.append(f"traction control {TRACTION_CONTROL.get(a['traction_control'], '?')}")
    if "gearbox_assist" in a:
        parts.append(f"gearbox {GEARBOX_ASSIST.get(a['gearbox_assist'], '?')}")
    if "braking_assist" in a:
        parts.append(f"braking assist {BRAKING_ASSIST.get(a['braking_assist'], '?')}")
    if "steering_assist" in a:
        parts.append(f"steering assist {'on' if a['steering_assist'] else 'off'}")
    if "dynamic_racing_line" in a:
        parts.append(f"racing line {RACING_LINE.get(a['dynamic_racing_line'], '?')}")
    if not parts:
        return None
    return Fact("context:assists", "context", "Assists on this lap: " + ", ".join(parts) + ".")


def _against(label: str) -> str:
    """ "the session best (lap 3)", but "lap 2" without an article."""
    return label if label.startswith("lap ") else f"the {label}"


def build_report(
    *,
    session: dict[str, Any],
    lap: dict[str, Any],
    reference: Reference,
    comparison: Comparison,
    analysis: CornerAnalysis,
    findings: list[Finding],
    model: TrackModel,
    lap_numbers: list[int],
    consistency: dict[str, Any] | None = None,
    benchmark: Benchmark | None = None,
    history: list[Fact] | None = None,
) -> EngineerReport:
    names = [s.name for s in model.segments]
    facts: list[Fact] = []
    lap_number = lap["lap_number"]
    lap_ms = lap["lap_time_ms"]
    ref_ms = reference.official_time_ms
    delta = (lap_ms - ref_ms) / 1000 if lap_ms and ref_ms else float(comparison.delta[-1])
    same_lap = (
        any(
            s.get("session_key") == session["session_key"]
            and s.get("lap_index") == lap["lap_index"]
            for s in reference.sources
        )
        and len(reference.sources) == 1
    )
    track = session.get("track_name") or "this track"

    facts.append(
        Fact(
            "lap",
            "lap",
            f"Lap {lap_number} at {track}: {fmt_lap(lap_ms)}, {fmt_s(delta)} against "
            f"{_against(reference.label)} ({fmt_lap(ref_ms)}).",
            data={"lap_time_ms": lap_ms, "reference_ms": ref_ms, "delta_s": round(delta, 3)},
        )
    )
    for sec in comparison.sectors:
        src = "official game times" if sec["delta_source"] == "official" else "from the traces"
        times = (
            f" ({fmt_lap(sec['lap_ms'])} vs {fmt_lap(sec['ref_ms'])})"
            if sec["lap_ms"] is not None and sec["ref_ms"] is not None
            else ""
        )
        facts.append(
            Fact(
                f"sector:{sec['sector']}",
                "sector",
                f"Sector {sec['sector']}: {fmt_s(sec['delta_s'])}{times}, {src}.",
                data={"delta_s": sec["delta_s"]},
            )
        )

    finding_facts = [_finding_fact(f, "find") for f in findings]
    by_segment: dict[str, list[Fact]] = {}
    for ff in finding_facts:
        by_segment.setdefault(ff.segment_id or "", []).append(ff)

    seg_index = {r.segment.id: r for r in analysis.segments}
    for r in analysis.segments:
        total = r.total_s
        if abs(total) < SEGMENT_FACT_MIN_S:
            continue
        seg = r.segment
        extra = ""
        if seg.is_corner and abs(r.carried_s) >= SEGMENT_FACT_MIN_S and r.carried_to:
            nxt = seg_index.get(r.carried_to)
            where = nxt.segment.name if nxt else "the next straight"
            extra = f", including {fmt_s(r.carried_s)} carried down {where} by the exit"
        facts.append(
            Fact(
                f"seg:{seg.id}",
                "segment",
                f"{seg.name}: {fmt_s(total)} against the reference{extra}.",
                segment_id=seg.id,
                segment_name=seg.name,
                data={"total_s": round(total, 3), "carried_s": round(r.carried_s, 3)},
            )
        )
    facts.extend(finding_facts)

    strengths = []
    for r in analysis.segments:
        if r.segment.is_corner and r.total_s <= -STRENGTH_MIN_S:
            fid = f"strength:{r.segment.id}"
            facts.append(
                Fact(
                    fid,
                    "strength",
                    f"{r.segment.name}: {fmt_s(r.total_s)}, faster than the reference here.",
                    segment_id=r.segment.id,
                    segment_name=r.segment.name,
                    data={"total_s": round(r.total_s, 3)},
                )
            )
            strengths.append(fid)

    cons_ids: list[str] = []
    if consistency and consistency.get("laps", 0) >= 3:
        for s in consistency["segments"]:
            if s["inconsistent"]:
                fid = f"cons:{s['id']}"
                facts.append(
                    Fact(
                        fid,
                        "consistency",
                        f"{s['name']} varies from lap to lap: ±{s['std_s']:.3f} s (standard "
                        f"deviation) over {consistency['laps']} usable laps; best "
                        f"{s['best_s']:.3f} s on lap {s['best_lap_number']}.",
                        segment_id=s["id"],
                        segment_name=s["name"],
                        data={"std_s": s["std_s"], "best_s": s["best_s"]},
                    )
                )
                cons_ids.append(fid)
        if consistency.get("ideal_lap_s"):
            facts.append(
                Fact(
                    "cons:ideal",
                    "consistency",
                    "Ideal lap from your best segments of the session: "
                    f"{fmt_lap(round(consistency['ideal_lap_s'] * 1000))}.",
                    data={"ideal_lap_s": consistency["ideal_lap_s"]},
                )
            )
            cons_ids.append("cons:ideal")

    pro_info = None
    pro_findings: list[Fact] = []
    notes: list[str] = []
    if benchmark is not None:
        ref = benchmark.placed.ref
        t = benchmark.systematic["time"]
        corner_pct = t.get("braking_corners", {}).get("pace_pct")
        straight_pct = t.get("straights", {}).get("pace_pct")
        pace = ""
        if corner_pct is not None and straight_pct is not None:
            pace = (
                f"; systematic pace difference (game car vs real car) about {corner_pct:+.1f} % "
                f"in braking corners and {straight_pct:+.1f} % on straights"
            )
        pro_delta = lap_ms / 1000 - ref.lap_time_s if lap_ms else float("nan")
        facts.append(
            Fact(
                "pro:lap",
                "lap",
                f"Against {ref.driver} ({ref.season} {ref.event} {ref.session}"
                f"{' ' + ref.session_part if ref.session_part else ''}, {fmt_lap(ref.lap_time_ms)}"
                f"): {fmt_s(pro_delta)}{pace}.",
                basis="pro_measured",
                estimate=True,
                data={"driver": ref.driver, "ref_key": ref.key},
            )
        )
        pro_findings = [_finding_fact(f, "pro") for f in benchmark.findings]
        facts.extend(pro_findings)
        for s in benchmark.strengths:
            fid = f"pro:strength:{s['segment_id']}"
            facts.append(
                Fact(
                    fid,
                    "strength",
                    f"{s['segment_name']}: about {fmt_s(s['normalised_s'])} against "
                    f"{ref.driver} after the systematic difference, better than your norm.",
                    segment_id=s["segment_id"],
                    segment_name=s["segment_name"],
                    basis="pro_normalised",
                    estimate=True,
                )
            )
            strengths.append(fid)
        facts.append(
            Fact(
                "note:pro-limits",
                "note",
                f"Not compared with {ref.driver}: " + "; ".join(benchmark.not_assessed) + ".",
                basis="pro_measured",
            )
        )
        notes.append("note:pro-limits")
        pro_info = {
            "key": ref.key,
            "driver": ref.driver,
            "label": ref.label,
            "lap_time_ms": ref.lap_time_ms,
            "findings": [f.id for f in pro_findings],
        }

    assists = _assists_fact(lap)
    if assists:
        facts.append(assists)

    if session.get("synthetic"):
        facts.append(Fact("note:synthetic", "note", "Synthetic demo data, not a real lap."))
        notes.append("note:synthetic")
    if not lap.get("usable"):
        issues = ", ".join(json.loads(lap.get("issues_json") or "[]")) or "not a clean lap"
        facts.append(
            Fact(
                "note:lap-not-usable",
                "note",
                f"This lap is not a clean lap ({issues}): treat the comparison with care.",
            )
        )
        notes.append("note:lap-not-usable")
    if model.status == "draft":
        facts.append(
            Fact(
                "note:draft-corners",
                "note",
                "Corners were detected automatically (draft track model, not yet confirmed).",
            )
        )
        notes.append("note:draft-corners")
    if same_lap:
        facts.append(
            Fact("note:same-lap", "note", "This lap is its own reference: nothing to compare.")
        )
        notes.append("note:same-lap")
    elif not findings:
        facts.append(
            Fact(
                "note:no-findings",
                "note",
                "No driving difference above the measurement thresholds against "
                f"{_against(reference.label)}: no corner-specific change can be recommended "
                "from this comparison.",
            )
        )
        notes.append("note:no-findings")

    # focus: best finding per corner, ranked by impact x confidence weight
    candidates = finding_facts + pro_findings
    best: dict[str, tuple[float, Fact]] = {}
    for ff in candidates:
        w = WEIGHTS.get((ff.basis, ff.data["confidence"]), 0.4)
        score = max(ff.data["impact_s"], 0.0) * w
        seg_id = ff.segment_id or ""
        if seg_id not in best or score > best[seg_id][0]:
            best[seg_id] = (score, ff)
    ranked = sorted(best.values(), key=lambda x: -x[0])[:MAX_FOCUS]
    focus = [
        FocusItem(
            rank=i + 1,
            fact_id=ff.id,
            segment_id=ff.segment_id or "",
            segment_name=ff.segment_name or "",
            title=ff.data["title"],
            instruction=ff.data["suggestion"] or ff.data["title"],
            measured=ff.data["detail"],
            impact_s=ff.data["impact_s"],
            confidence=ff.data["confidence"],
            basis=ff.basis,
        )
        for i, (_, ff) in enumerate(ranked)
    ]

    losses = []
    for r in sorted(analysis.segments, key=lambda r: -r.total_s):
        if r.total_s < LOSS_MIN_S or len(losses) >= MAX_LOSSES:
            break
        causes = by_segment.get(r.segment.id, [])
        losses.append(
            LossItem(
                fact_id=f"seg:{r.segment.id}",
                segment_id=r.segment.id,
                segment_name=r.segment.name,
                loss_s=r.total_s,
                causes=[c.id for c in causes],
                cause_titles=[c.data["title"] for c in causes],
            )
        )

    if losses:
        top = losses[0]
        why = f", mainly: {top.cause_titles[0].lower()}" if top.cause_titles else ""
        biggest = f" Biggest loss: {top.segment_name} ({fmt_s(top.loss_s)}){why}."
    else:
        biggest = " No segment loses a measurable amount of time."
    headline = (
        f"Lap {lap_number}: {fmt_lap(lap_ms)}, {fmt_s(delta)} against {_against(reference.label)}."
        + biggest
    )
    history_ids = []
    patterns = {}
    for hf in history or []:
        facts.append(hf)
        history_ids.append(hf.id)
        if hf.id.startswith("hist:pattern:"):
            patterns[(hf.segment_id, hf.code)] = hf.id
    by_id = {ff.id: ff for ff in candidates}
    for item in focus:
        ff = by_id.get(item.fact_id)
        if ff is not None and ff.basis == "self":  # patterns are against your own laps
            item.recurring = patterns.get((item.segment_id, ff.code))

    for f in facts:
        f.bind(names)
    return EngineerReport(
        version=REPORT_VERSION,
        headline=headline,
        session={
            "session_key": session["session_key"],
            "track_name": session.get("track_name"),
            "session_type_name": session.get("session_type_name"),
            "synthetic": bool(session.get("synthetic")),
        },
        lap={
            "lap_index": lap["lap_index"],
            "lap_number": lap_number,
            "lap_time_ms": lap_ms,
            "usable": bool(lap.get("usable")),
        },
        reference={"kind": reference.kind, "label": reference.label, "lap_time_ms": ref_ms},
        delta_s=delta,
        facts=facts,
        focus=focus,
        losses=losses,
        strengths=strengths,
        consistency=cons_ids,
        notes=notes,
        segment_names=names,
        lap_numbers=sorted(set(lap_numbers)),
        pro=pro_info,
        history_facts=history_ids,
    )


# ------------------------------------------------------------------------ text


def render_text(report: EngineerReport) -> str:
    """The report as plain text (terminal, and the fallback when the AI text fails)."""
    lines = [report.headline, ""]
    if report.focus:
        lines.append("Focus for the next lap")
        for f in report.focus:
            tag = " (vs real F1, estimate)" if f.basis != "self" else ""
            lines.append(f" {f.rank}. {f.segment_name} · {f.title}{tag}")
            lines.append(f"    Try: {f.instruction}")
            lines.append(f"    Measured: {f.measured} [{f.fact_id}]")
            if f.recurring:
                lines.append(f"    Recurring here: see [{f.recurring}]")
    else:
        lines.append("Focus for the next lap: nothing specific; see the notes.")
    lines.append("")
    if report.losses:
        lines.append("Where the time goes")
        for x in report.losses:
            why = f" · {', '.join(t.lower() for t in x.cause_titles)}" if x.cause_titles else ""
            lines.append(f" - {x.segment_name}: {fmt_s(x.loss_s)}{why} [{x.fact_id}]")
        lines.append("")
    for title, ids in (
        ("Strengths", report.strengths),
        ("Consistency", report.consistency),
        ("Notes", report.notes),
        ("History at this track", report.history_facts),
    ):
        if ids:
            lines.append(title)
            for fid in ids:
                lines.append(f" - {report.fact(fid).text} [{fid}]")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
