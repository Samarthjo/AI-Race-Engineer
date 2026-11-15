"""AI-written texts (data layer L5), stored apart from the measured analysis.

Each row keeps what was shown (the AI text, or the deterministic report when the AI text
failed), how it was checked, and what produced it: provider, model, prompt version, report
version and a hash of the facts the model saw.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from pitwall import __version__
from pitwall.storage import db


def save(
    conn: sqlite3.Connection,
    *,
    kind: str,
    session_key: str,
    lap_index: int,
    reference: str,
    report_version: str,
    result: dict[str, Any],
    question: str | None = None,
) -> str:
    """Store a narrative (``kind="report"``) or an answer (``kind="answer"``)."""
    narrative_id = uuid.uuid4().hex
    db.insert_narrative(
        conn,
        {
            "narrative_id": narrative_id,
            "kind": kind,
            "session_key": session_key,
            "lap_index": lap_index,
            "reference": reference,
            "question": question,
            "status": result["status"],
            "provider": result["provider"],
            "model": result["model"],
            "prompt_version": result["prompt_version"],
            "report_version": report_version,
            "facts_hash": result["facts_hash"],
            "output_json": json.dumps(result["output"], ensure_ascii=False, sort_keys=True),
            "attempts": result["attempts"],
            "log_json": json.dumps(
                {"log": result.get("log", []), "tool_calls": result.get("tool_calls", [])},
                ensure_ascii=False,
            ),
            "error": result.get("error"),
            "duration_s": result.get("duration_s"),
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "app_version": __version__,
        },
    )
    return narrative_id


def row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    log = json.loads(r["log_json"])
    return {
        "narrative_id": r["narrative_id"],
        "kind": r["kind"],
        "reference": r["reference"],
        "question": r["question"],
        "status": r["status"],
        "provider": r["provider"],
        "model": r["model"],
        "prompt_version": r["prompt_version"],
        "report_version": r["report_version"],
        "facts_hash": r["facts_hash"],
        "output": json.loads(r["output_json"]),
        "attempts": r["attempts"],
        "log": log.get("log", []),
        "tool_calls": log.get("tool_calls", []),
        "error": r["error"],
        "duration_s": r["duration_s"],
        "created_utc": r["created_utc"],
    }
