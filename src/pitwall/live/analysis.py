"""The provisional report seconds after each clean lap.

The completed lap's frames (kept in memory by the live engine) become a lap trace, compared
with your best earlier lap on this track: the personal best from ingested recordings, else
the best other clean lap of this live session. Corners come from the saved track model, or
an unsaved draft when there is none. The result is the engineer's measured report (headline,
focus for the next lap, where the time goes).

It is provisional: ``pitwall ingest`` rebuilds the lap from the raw recording, and the Lap
Analysis and Race Engineer pages are the reference. Comparisons with real F1 laps are left to
the Driver Benchmark page (they need more care than a quick look between laps).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from pitwall.analysis.compare import compare, reference_from_trace
from pitwall.analysis.corners import analyse_corners
from pitwall.analysis.findings import lap_findings
from pitwall.analysis.trace import LapTrace, NotComparableError
from pitwall.engineer.report import build_report
from pitwall.ingest.pipeline import DB_NAME
from pitwall.live.reference import LiveReference, from_trace, personal_best
from pitwall.storage import db
from pitwall.track.draft import draft_model
from pitwall.track.model import load_model, track_key

if TYPE_CHECKING:
    from pitwall.live.engine import LiveEngine, LiveLap


def _best_other(engine: LiveEngine, lap: LiveLap) -> LiveReference | None:
    others = [
        x
        for x in engine.laps
        if x is not lap and x.usable and x.trace is not None and x.lap_time_ms
    ]
    if not others:
        return None
    best = min(others, key=lambda x: x.lap_time_ms or 0)
    assert best.trace is not None
    return from_trace(
        best.trace, "session_best", "session_best", f"session best (lap {best.number})"
    )


def analyse_lap(
    engine: LiveEngine, lap: LiveLap, trace: LapTrace, session: dict[str, Any]
) -> dict[str, Any]:
    assert engine.data_dir is not None
    data_dir = engine.data_dir
    conn = db.connect(data_dir / DB_NAME)
    try:
        ref = personal_best(conn, data_dir, session) or _best_other(engine, lap)
        if ref is None or ref.trace is None:
            return {"status": "skipped", "reason": "no earlier clean lap on this track yet"}
        reference = reference_from_trace(ref.trace, ref.kind, ref.label)
        try:
            comparison = compare(trace, reference)
        except NotComparableError as exc:
            return {"status": "skipped", "reason": str(exc)}
        synthetic = bool(session.get("synthetic"))
        key = track_key(session["track_id"], session["track_name"], synthetic)
        model = load_model(data_dir, key)
        notes = []
        if model is None or not model.fits(session["track_length_m"]):
            model = draft_model(
                ref.trace,
                track_id=session["track_id"],
                track_name=session["track_name"],
                synthetic=synthetic,
                source={"live": True},
            )
            notes.append("corners detected from the reference lap (draft, not saved)")
        analysis = analyse_corners(comparison, model)
        findings = lap_findings(analysis)
        report = build_report(
            session=session,
            lap={
                "lap_index": lap.number,
                "lap_number": lap.number,
                "lap_time_ms": lap.lap_time_ms,
                "usable": lap.usable,
                "issues_json": json.dumps(lap.issues),
            },
            reference=reference,
            comparison=comparison,
            analysis=analysis,
            findings=findings,
            model=model,
            lap_numbers=[x.number for x in engine.laps] + [ref.trace.lap_number],
        )
    finally:
        conn.close()
    return {
        "status": "ready",
        "provisional": True,
        "reference": ref.label,
        "headline": report.headline,
        "delta_s": round(report.delta_s, 3),
        "focus": [asdict(f) | {"impact_s": round(f.impact_s, 3)} for f in report.focus],
        "losses": [
            {"segment_name": x.segment_name, "loss_s": round(x.loss_s, 3), "causes": x.cause_titles}
            for x in report.losses
        ],
        "notes": notes + [report.fact(n).text for n in report.notes],
    }
