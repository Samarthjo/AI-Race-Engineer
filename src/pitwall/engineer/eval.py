"""The Stage 5 evaluation: write up many of your laps with a model, measure how often the
text passes the fact check, and produce a sheet for you to rate accuracy and usefulness.

The gate of Stage 5 is zero unsupported claims: the fact check measures that automatically
(a write-up that fails twice is replaced by the measured report), and your ratings judge
whether what passes is also right and useful. Run it once per candidate model and compare.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pitwall.analysis.trace import NotComparableError, check_comparable
from pitwall.engineer.llm import LLMProvider
from pitwall.engineer.narrator import PROMPT_VERSION, render_narrative
from pitwall.engineer.report import REPORT_VERSION, render_text
from pitwall.engineer.service import build_context, narrate_lap
from pitwall.reference.laps import slug
from pitwall.storage import db


def eval_laps(
    conn: sqlite3.Connection,
    sessions: list[dict[str, Any]] | None,
    *,
    limit: int,
    synthetic: bool,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Usable, comparable laps, newest sessions first; each session's best lap is skipped
    (it is the reference)."""
    chosen = sessions or [dict(r) for r in db.list_sessions(conn)]
    out = []
    for s in chosen:
        if s.get("synthetic") and not synthetic:
            continue
        best = db.best_usable_lap(conn, s["session_key"])
        for row in db.list_laps(conn, s["session_key"]):
            lap = dict(row)
            if not lap["usable"] or (best is not None and lap["lap_index"] == best["lap_index"]):
                continue
            try:
                check_comparable(lap)
            except NotComparableError:
                continue
            out.append((s, lap))
            if len(out) >= limit:
                return out
    return out


def run_eval(
    conn: sqlite3.Connection,
    data_dir: Path,
    provider: LLMProvider,
    *,
    limit: int = 20,
    sessions: list[dict[str, Any]] | None = None,
    synthetic: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[dict[str, Any], Path]:
    laps = eval_laps(conn, sessions, limit=limit, synthetic=synthetic)
    rows: list[dict[str, Any]] = []
    blocks: list[str] = []
    for i, (s, lap) in enumerate(laps, 1):
        label = f"{s.get('track_name')} · {s['session_key']} · lap {lap['lap_number']}"
        if progress:
            progress(i, len(laps), label)
        try:
            ctx = build_context(conn, data_dir, s["session_key"], lap["lap_index"])
        except (NotComparableError, LookupError) as exc:
            rows.append({"label": label, "status": "skipped", "note": str(exc)})
            continue
        result, nid = narrate_lap(conn, ctx, provider)
        first = result.log[0]["violations"] if result.log else []
        rows.append(
            {
                "label": label,
                "status": result.status,
                "attempts": result.attempts,
                "first_problems": len(first),
                "duration_s": result.duration_s,
                "narrative_id": nid,
            }
        )
        blocks.append(
            f"## {i}. {label}\n\n"
            f"**Measured report** ({REPORT_VERSION})\n\n```\n{render_text(ctx.report)}```\n\n"
            f"**AI write-up** ({result.status}, {result.attempts} attempt(s), "
            f"{result.duration_s:.1f} s, id {nid[:8]})\n\n"
            f"```\n{render_narrative(result.output)}```\n\n"
            + (
                "First attempt rejected for:\n"
                + "".join(f"- {v['where']}: {v['detail']}\n" for v in first)
                + "\n"
                if first
                else ""
            )
            + "**Your rating**: accuracy (1-5): __ · usefulness (1-5): __ · "
            "unsupported claims you spotted: __\n"
        )
    done = [r for r in rows if r["status"] != "skipped"]
    summary = {
        "laps": len(done),
        "grounded_first": sum(1 for r in done if r["status"] == "grounded" and r["attempts"] == 1),
        "grounded": sum(1 for r in done if r["status"] == "grounded"),
        "fallback": sum(1 for r in done if r["status"] == "fallback"),
        "unavailable": sum(1 for r in done if r["status"] == "unavailable"),
        "median_s": statistics.median([r["duration_s"] for r in done]) if done else 0.0,
        "model": provider.model,
        "prompt_version": PROMPT_VERSION,
    }
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    path = Path(data_dir) / "engineer" / f"eval-{stamp}-{slug(provider.model)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = "\n".join(
        f"| {k + 1} | {r['label']} | {r['status']} | {r.get('attempts', '')} | "
        f"{r.get('first_problems', '')} | {r.get('duration_s', 0):.1f} |"
        for k, r in enumerate(rows)
    )
    path.write_text(
        f"# Engineer eval: {provider.model}\n\n"
        f"Prompt {PROMPT_VERSION}, report {REPORT_VERSION}, {when}.\n\n"
        f"- Laps: {summary['laps']}\n"
        f"- Passed the fact check first time: {summary['grounded_first']}\n"
        f"- Passed after the one retry: {summary['grounded']}\n"
        f"- Failed twice (measured report shown instead): {summary['fallback']}\n"
        f"- Median time per lap: {summary['median_s']:.1f} s\n\n"
        "| # | lap | result | attempts | problems at first try | seconds |\n"
        "|---|---|---|---|---|---|\n"
        f"{table}\n\n"
        "Rate each write-up below: is everything it says right (accuracy), and would it help "
        "you on the next lap (usefulness)? Note any claim that the measured report does not "
        "support.\n\n" + "\n".join(blocks)
    )
    return summary, path
