"""Ingest: raw capture (L0) -> decoded player frames (L1) + sessions and laps (L2).

Idempotent and deterministic: re-ingesting a capture replaces its previous results, and
the same capture always produces the same frames and laps.

Output layout::

    <data_dir>/pitwall.db                                   sessions + laps catalogue
    <data_dir>/sessions/<capture_id>/<session_uid>/frames.parquet   player car, per frame
    <data_dir>/sessions/<capture_id>/<session_uid>/session.json     metadata, events, history
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from pitwall.capture.rawlog import CaptureReadSummary, iter_capture
from pitwall.capture.session import read_manifest
from pitwall.ingest.collector import collect
from pitwall.ingest.session_builder import BuiltSession, Lap, build_session
from pitwall.storage import db

PIPELINE_VERSION = "1.0.0"
DB_NAME = "pitwall.db"


@dataclass
class SessionOutcome:
    session_key: str
    summary: dict[str, Any]
    laps: list[Lap]


@dataclass
class IngestResult:
    capture_id: str
    datagrams: int
    decoded: int
    errors: dict[str, int]
    error_examples: list[str]
    read: CaptureReadSummary
    sessions: list[SessionOutcome] = field(default_factory=list)
    skipped_sessions: list[str] = field(default_factory=list)


def session_key(capture_id: str, session_uid: int) -> str:
    return f"{capture_id}/{session_uid:016x}"


def write_frames(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {b"pitwall.pipeline_version": PIPELINE_VERSION.encode()}
    pq.write_table(table.replace_schema_metadata(meta), path, compression="zstd")


def read_frames(path: Path, columns: list[str] | None = None) -> pa.Table:
    return pq.read_table(path, columns=columns)


def ingest_capture(capture_dir: Path, data_dir: Path, conn: sqlite3.Connection) -> IngestResult:
    capture_id = capture_dir.name
    manifest = read_manifest(capture_dir) or {}
    read = CaptureReadSummary()
    collected = collect(iter_capture(capture_dir, read))
    result = IngestResult(
        capture_id=capture_id,
        datagrams=collected.datagrams,
        decoded=collected.decoded,
        errors=dict(collected.errors),
        error_examples=list(collected.error_examples),
        read=read,
    )

    synthetic = int(bool(manifest.get("synthetic")))
    out_root = data_dir / "sessions" / capture_id
    if out_root.exists():
        shutil.rmtree(out_root)

    session_rows: list[dict[str, Any]] = []
    lap_rows: list[dict[str, Any]] = []
    for uid in sorted(collected.sessions):
        built = build_session(collected.sessions[uid])
        key = session_key(capture_id, uid)
        if built is None:
            result.skipped_sessions.append(key)
            continue
        out_dir = out_root / f"{uid:016x}"
        frames_path = out_dir / "frames.parquet"
        details_path = out_dir / "session.json"
        write_frames(built.frames, frames_path)
        details = dict(built.details)
        details["session_key"] = key
        details["pipeline_version"] = PIPELINE_VERSION
        details_path.write_text(
            json.dumps(details, indent=1, sort_keys=True, default=_json_default) + "\n"
        )
        row = _session_row(key, capture_id, built, frames_path, details_path, data_dir)
        row["synthetic"] = synthetic
        row["capture_label"] = manifest.get("label")
        if synthetic:
            row["track_name"] = "Synthetic test track"  # never shown as a real circuit
            built.summary["track_name"] = row["track_name"]
        session_rows.append(row)
        lap_rows.extend(_lap_row(key, lap) for lap in built.laps)
        result.sessions.append(SessionOutcome(key, built.summary, built.laps))

    capture_row = {
        "capture_id": capture_id,
        "path": str(capture_dir),
        "label": manifest.get("label"),
        "note": manifest.get("note"),
        "started_utc": manifest.get("started_utc"),
        "ingested_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "datagrams": collected.datagrams,
        "decoded": collected.decoded,
        "decode_errors": json.dumps(dict(collected.errors), sort_keys=True),
        "pipeline_version": PIPELINE_VERSION,
        "synthetic": synthetic,
    }
    db.replace_capture(conn, capture_row, session_rows, lap_rows)
    return result


def _json_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _session_row(
    key: str, capture_id: str, built: BuiltSession, frames: Path, details: Path, root: Path
) -> dict[str, Any]:
    s = built.summary
    return {
        "session_key": key,
        "capture_id": capture_id,
        "session_uid": s["session_uid"],
        "packet_format": s["packet_format"],
        "game_version": s["game_version"],
        "track_id": s["track_id"],
        "track_name": s["track_name"],
        "track_length_m": s["track_length_m"],
        "session_type": s["session_type"],
        "session_type_name": s["session_type_name"],
        "game_mode": s["game_mode"],
        "game_mode_name": s["game_mode_name"],
        "formula": s["formula"],
        "weather": s["weather"],
        "track_temp_c": s["track_temp_c"],
        "air_temp_c": s["air_temp_c"],
        "sector2_start_m": s["sector2_start_m"],
        "sector3_start_m": s["sector3_start_m"],
        "player_car_index": s["player_car_index"],
        "team_id": s["team_id"],
        "first_utc": s["first_utc"],
        "last_utc": s["last_utc"],
        "packets": s["packets"],
        "lap_count": s["lap_count"],
        "complete_laps": s["complete_laps"],
        "valid_laps": s["valid_laps"],
        "usable_laps": s["usable_laps"],
        "best_usable_lap_ms": s["best_usable_lap_ms"],
        "best_valid_lap_ms": s["best_valid_lap_ms"],
        "flashbacks": s["flashbacks"],
        "timeline_resets": s["timeline_resets"],
        "assists_json": db.to_json_text(s["assists"]),
        "settings_json": db.to_json_text(s["settings"]),
        "frames_path": _rel(frames, root),
        "details_path": _rel(details, root),
        "pipeline_version": PIPELINE_VERSION,
    }


def _bool(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _lap_row(key: str, lap: Lap) -> dict[str, Any]:
    return {
        "session_key": key,
        "lap_index": lap.lap_index,
        "lap_number": lap.lap_number,
        "segment": lap.segment,
        "kind": lap.kind,
        "complete": int(lap.complete),
        "valid": _bool(lap.valid),
        "usable": int(lap.usable),
        "lap_time_ms": lap.lap_time_ms,
        "lap_time_source": lap.lap_time_source,
        "s1_ms": lap.s1_ms,
        "s2_ms": lap.s2_ms,
        "s3_ms": lap.s3_ms,
        "valid_history": _bool(lap.valid_history),
        "invalid_flag_seen": int(lap.invalid_flag_seen),
        "flashbacks": lap.flashbacks,
        "pauses": lap.pauses,
        "frames": lap.frames,
        "missing_frames": lap.missing_frames,
        "loss_pct": lap.loss_pct,
        "max_distance_gap_m": lap.max_distance_gap_m,
        "start_session_time": lap.start_session_time,
        "end_session_time": lap.end_session_time,
        "start_frame": lap.start_frame,
        "end_frame": lap.end_frame,
        "start_distance_m": lap.start_distance_m,
        "end_distance_m": lap.end_distance_m,
        "tyre_actual": lap.tyre_actual,
        "tyre_visual": lap.tyre_visual,
        "tyre_age_laps": lap.tyre_age_laps,
        "fuel_start": lap.fuel_start,
        "fuel_end": lap.fuel_end,
        "tyre_wear_start": db.to_json_text(lap.tyre_wear_start),
        "tyre_wear_end": db.to_json_text(lap.tyre_wear_end),
        "assists_json": db.to_json_text(lap.assists),
        "setup_json": db.to_json_text(lap.setup),
        "issues_json": json.dumps(lap.issues),
        "notes_json": json.dumps(lap.notes),
        "pipeline_version": PIPELINE_VERSION,
    }
