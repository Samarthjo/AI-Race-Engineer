"""Everything the engineer needs for one lap: comparison, corners, findings, consistency, an
optional real-F1 benchmark, and the deterministic report built from them."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pitwall.analysis.benchmark import Benchmark, benchmark, place_reference
from pitwall.analysis.compare import Comparison, Reference, compare, resolve_reference
from pitwall.analysis.consistency import session_consistency
from pitwall.analysis.corners import CornerAnalysis, analyse_corners
from pitwall.analysis.findings import Finding, lap_findings
from pitwall.analysis.trace import LapTrace, NotComparableError, load_trace
from pitwall.engineer import store
from pitwall.engineer.facts import Fact
from pitwall.engineer.llm import LLMProvider
from pitwall.engineer.narrator import Narrative, narrate
from pitwall.engineer.qa import Answer, QATools, ask
from pitwall.engineer.report import EngineerReport, build_report
from pitwall.engineer.validate import Grounding
from pitwall.reference.service import check_pairing, load_reference
from pitwall.storage import db
from pitwall.track.model import TrackModel
from pitwall.track.service import model_for_session


@dataclass
class LapContext:
    session: dict[str, Any]
    lap: dict[str, Any]
    trace: LapTrace
    reference: Reference
    comparison: Comparison
    model: TrackModel
    analysis: CornerAnalysis
    findings: list[Finding]
    consistency: dict[str, Any] | None
    benchmark: Benchmark | None
    report: EngineerReport
    ref_kind: str
    ref_lap: int | None
    pro_key: str | None

    @property
    def reference_tag(self) -> str:
        """Short description of the comparison, stored with every AI text."""
        tag = self.ref_kind if self.ref_lap is None else f"{self.ref_kind}:{self.ref_lap}"
        return tag + (f"+pro:{self.pro_key}" if self.pro_key else "")


def build_context(
    conn: sqlite3.Connection,
    data_dir: Path,
    session_key: str,
    lap_index: int,
    *,
    ref_kind: str = "session_best",
    ref_lap: int | None = None,
    pro_key: str | None = None,
    with_consistency: bool = True,
) -> LapContext:
    """Raises LookupError (unknown session/lap/reference) or NotComparableError (with the
    reason the comparison cannot be made)."""
    srow = db.get_session(conn, session_key)
    if srow is None:
        raise LookupError(f"session {session_key!r} not found")
    session = dict(srow)
    lrow = db.get_lap(conn, session_key, lap_index)
    if lrow is None:
        raise LookupError(f"lap {lap_index} not found in the session")
    lap = dict(lrow)
    trace = load_trace(data_dir, session, lap)
    reference = resolve_reference(
        conn, data_dir, session, ref_kind, ref_session_key=session_key, ref_lap_index=ref_lap
    )
    comparison = compare(trace, reference)
    model = model_for_session(conn, data_dir, session)
    analysis = analyse_corners(comparison, model)
    findings = lap_findings(analysis)
    consistency = None
    if with_consistency:
        try:
            consistency = session_consistency(conn, data_dir, session, model)
        except NotComparableError:
            consistency = None
    bench = None
    if pro_key:
        ref = load_reference(conn, data_dir, pro_key)
        check_pairing(session, ref)
        bench = benchmark(trace, place_reference(ref, trace), model)
    lap_numbers = [r["lap_number"] for r in db.list_laps(conn, session_key)]
    lap_numbers += [s.get("lap_number") for s in reference.sources if s.get("lap_number")]
    if bench is not None:
        lap_numbers.append(bench.placed.ref.lap_number)
    report = build_report(
        session=session,
        lap=lap,
        reference=reference,
        comparison=comparison,
        analysis=analysis,
        findings=findings,
        model=model,
        lap_numbers=lap_numbers,
        consistency=consistency,
        benchmark=bench,
    )
    return LapContext(
        session=session,
        lap=lap,
        trace=trace,
        reference=reference,
        comparison=comparison,
        model=model,
        analysis=analysis,
        findings=findings,
        consistency=consistency,
        benchmark=bench,
        report=report,
        ref_kind=ref_kind,
        ref_lap=ref_lap,
        pro_key=pro_key,
    )


# ------------------------------------------------------------------ AI helpers


def grounding_for(ctx: LapContext) -> Grounding:
    return Grounding(
        facts=ctx.report.facts_by_id(),
        segments=[(s.id, s.name) for s in ctx.model.segments],
        lap_numbers=set(ctx.report.lap_numbers),
    )


def comparison_facts(
    conn: sqlite3.Connection, data_dir: Path, ctx: LapContext, lap_number: int
) -> list[Fact]:
    """Facts for this lap against another lap of the session (ids prefixed ``vsN:``)."""
    rows = [dict(r) for r in db.list_laps(conn, ctx.session["session_key"])]
    other = next((r for r in rows if r["lap_number"] == lap_number), None)
    if other is None:
        raise LookupError(f"lap {lap_number} is not in this session")
    if other["lap_index"] == ctx.lap["lap_index"]:
        raise ValueError("that is the same lap")
    try:
        sub = build_context(
            conn,
            data_dir,
            ctx.session["session_key"],
            ctx.lap["lap_index"],
            ref_kind="lap",
            ref_lap=other["lap_index"],
            with_consistency=False,
        )
    except NotComparableError as exc:
        raise ValueError(f"lap {lap_number} cannot be compared: {exc}") from exc
    names = [s.name for s in ctx.model.segments]
    out = []
    for f in sub.report.facts:
        if f.kind not in ("lap", "sector", "segment", "finding", "strength"):
            continue
        out.append(
            Fact(
                id=f"vs{lap_number}:{f.id}",
                kind=f.kind,
                text=f"(This lap vs lap {lap_number}) {f.text}",
                segment_id=f.segment_id,
                segment_name=f.segment_name,
                basis=f.basis,
                estimate=f.estimate,
                code=f.code,
                data=f.data,
            ).bind(names)
        )
    return out


def narrate_lap(
    conn: sqlite3.Connection, ctx: LapContext, provider: LLMProvider
) -> tuple[Narrative, str]:
    """AI write-up of the lap's report, checked and stored. Returns it with its id."""
    result = narrate(ctx.report, provider, grounding_for(ctx))
    nid = store.save(
        conn,
        kind="report",
        session_key=ctx.session["session_key"],
        lap_index=ctx.lap["lap_index"],
        reference=ctx.reference_tag,
        report_version=ctx.report.version,
        result=result.to_dict(),
    )
    return result, nid


def ask_lap(
    conn: sqlite3.Connection,
    data_dir: Path,
    ctx: LapContext,
    provider: LLMProvider,
    question: str,
) -> tuple[Answer, str]:
    tools = QATools(
        facts=ctx.report.facts_by_id(),
        segments=[(s.id, s.name) for s in ctx.model.segments],
        compare=lambda n: comparison_facts(conn, data_dir, ctx, n),
    )
    result = ask(question, tools, grounding_for(ctx), provider)
    nid = store.save(
        conn,
        kind="answer",
        session_key=ctx.session["session_key"],
        lap_index=ctx.lap["lap_index"],
        reference=ctx.reference_tag,
        report_version=ctx.report.version,
        result=result.to_dict(),
        question=question,
    )
    return result, nid
