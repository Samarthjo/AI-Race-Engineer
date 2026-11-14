"""Local web API and UI host (``pitwall serve``).

JSON endpoints under ``/api``; the built web UI (``web/`` -> ``static/``) is served at ``/``.
Everything runs on this machine and reads the local data directory.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from pitwall import __version__
from pitwall.analysis.compare import REFERENCE_KINDS, Comparison, compare, resolve_reference
from pitwall.analysis.consistency import session_consistency
from pitwall.analysis.corners import DEFINITIONS as CORNER_DEFINITIONS
from pitwall.analysis.corners import analyse_corners
from pitwall.analysis.findings import lap_findings
from pitwall.analysis.metrics import DEFINITIONS, load_lap_metrics
from pitwall.analysis.trace import NotComparableError, check_comparable, load_trace
from pitwall.api.live import LiveStatus
from pitwall.ingest.pipeline import DB_NAME
from pitwall.storage import db
from pitwall.track.model import TrackModel, confirm_model, list_models
from pitwall.track.service import model_for_session

STATIC_DIR = Path(__file__).parent / "static"
TRACE_CHANNELS = ("speed", "throttle", "brake", "gear", "steer", "rpm")
CHANNEL_DECIMALS = {"speed": 1, "throttle": 3, "brake": 3, "gear": 0, "steer": 3, "rpm": 0}


def _num(x: Any, nd: int) -> float | int | None:
    if x is None:
        return None
    f = float(x)
    if not math.isfinite(f):
        return None
    return round(f) if nd == 0 else round(f, nd)


def _arr(a: np.ndarray, nd: int, take: np.ndarray) -> list[float | int | None]:
    return [_num(v, nd) for v in a[take]]


def session_payload(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_key": s["session_key"],
        "capture_id": s["capture_id"],
        "session_uid": s["session_uid"],
        "label": s.get("capture_label"),
        "synthetic": bool(s.get("synthetic")),
        "track_id": s["track_id"],
        "track_name": s["track_name"],
        "track_length_m": s["track_length_m"],
        "session_type_name": s["session_type_name"],
        "game_mode_name": s["game_mode_name"],
        "game_version": s["game_version"],
        "first_utc": s["first_utc"],
        "lap_count": s["lap_count"],
        "usable_laps": s["usable_laps"],
        "best_usable_lap_ms": s["best_usable_lap_ms"],
        "sector_starts_m": (
            [s["sector2_start_m"], s["sector3_start_m"]]
            if s["sector2_start_m"] and s["sector3_start_m"]
            else None
        ),
    }


def lap_payload(lap: dict[str, Any]) -> dict[str, Any]:
    try:
        check_comparable(lap)
        comparable, reason = True, None
    except NotComparableError as exc:
        comparable, reason = False, str(exc)
    return {
        "lap_index": lap["lap_index"],
        "lap_number": lap["lap_number"],
        "kind": lap["kind"],
        "complete": bool(lap["complete"]),
        "valid": None if lap["valid"] is None else bool(lap["valid"]),
        "usable": bool(lap["usable"]),
        "lap_time_ms": lap["lap_time_ms"],
        "sectors_ms": [lap["s1_ms"], lap["s2_ms"], lap["s3_ms"]],
        "issues": json.loads(lap["issues_json"]),
        "notes": json.loads(lap["notes_json"]),
        "comparable": comparable,
        "not_comparable_reason": reason,
    }


def comparison_payload(
    c: Comparison,
    session: dict[str, Any],
    lap: dict[str, Any],
    lap_metrics: dict[str, Any],
    ref_metrics: dict[str, Any] | None,
    step_m: float,
) -> dict[str, Any]:
    dist = c.distance
    base_step = float(dist[1] - dist[0]) if len(dist) > 1 else 1.0
    every = max(1, round(step_m / base_step))
    take = np.arange(0, len(dist), every)
    if take[-1] != len(dist) - 1:
        take = np.append(take, len(dist) - 1)

    def channels(src: dict[str, np.ndarray]) -> dict[str, list[float | int | None]]:
        return {k: _arr(src[k], CHANNEL_DECIMALS[k], take) for k in TRACE_CHANNELS if k in src}

    ref = c.ref
    return {
        "session": session_payload(session),
        "lap": {
            **lap_payload(lap),
            "metrics": lap_metrics,
            "max_gap_m": round(c.lap.max_gap_m, 2),
        },
        "reference": {
            "kind": ref.kind,
            "label": ref.label,
            "lap_time_ms": ref.official_time_ms,
            "trace_time_s": round(ref.lap_time_s, 4),
            "sectors_ms": list(ref.sector_times_ms),
            "sources": ref.sources,
            "metrics": ref_metrics,
        },
        "delta_s": round(c.lap.lap_time_s - (ref.official_time_ms or 0) / 1000, 3)
        if ref.official_time_ms
        else round(float(c.delta[-1]), 3),
        "sectors": c.sectors,
        "largest_losses": c.largest_losses,
        "largest_gains": c.largest_gains,
        "metric_definitions": DEFINITIONS,
        "trace": {
            "step_m": base_step * every,
            "distance": _arr(dist, 1, take),
            "delta": _arr(c.delta, 4, take),
            "loss_rate": _arr(c.loss_rate, 4, take),
            "x": _arr(c.lap.channels["x"], 2, take),
            "z": _arr(c.lap.channels["z"], 2, take),
            "lap": channels(c.lap.channels),
            "ref": channels(ref.channels),
        },
    }


def model_payload(m: TrackModel) -> dict[str, Any]:
    return {
        "key": m.key,
        "track_name": m.track_name,
        "status": m.status,
        "version": m.version,
        "synthetic": m.synthetic,
        "length_m": m.length_m,
        "source": m.source,
        "updated_utc": m.updated_utc,
        "segments": [
            {
                "id": seg.id,
                "name": seg.name,
                "kind": seg.kind,
                "start_m": seg.start_m,
                "end_m": seg.end_m,
                "apexes_m": seg.apexes_m,
            }
            for seg in m.segments
        ],
    }


def create_app(data_dir: Path, live: LiveStatus | None = None) -> FastAPI:
    data_dir = Path(data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if live is not None:
            live.start()
        yield
        if live is not None:
            live.stop()

    app = FastAPI(title="Pitwall AI", version=__version__, lifespan=lifespan)

    @contextmanager
    def database() -> Iterator[sqlite3.Connection]:
        conn = db.connect(data_dir / DB_NAME)
        try:
            yield conn
        finally:
            conn.close()

    def find_session(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
        row = db.get_session(conn, key)
        if row is None:
            raise HTTPException(404, f"session {key!r} not found")
        return dict(row)

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "version": __version__,
            "data_dir": str(data_dir),
            "reference_kinds": list(REFERENCE_KINDS),
            "live": live is not None,
        }

    @app.get("/api/sessions")
    def sessions(limit: int = 200) -> list[dict[str, Any]]:
        with database() as conn:
            return [session_payload(dict(r)) for r in db.list_sessions(conn, limit=limit)]

    @app.get("/api/laps")
    def laps(session: str) -> dict[str, Any]:
        with database() as conn:
            s = find_session(conn, session)
            rows = [lap_payload(dict(r)) for r in db.list_laps(conn, session)]
        return {"session": session_payload(s), "laps": rows}

    @app.get("/api/compare")
    def compare_laps(
        session: str,
        lap: int,
        ref: str = "session_best",
        ref_session: str | None = None,
        ref_lap: int | None = None,
        step: float = Query(2.0, ge=1.0, le=20.0),
    ) -> dict[str, Any]:
        with database() as conn:
            s = find_session(conn, session)
            lap_row = db.get_lap(conn, session, lap)
            if lap_row is None:
                raise HTTPException(404, f"lap {lap} not found in session")
            lap_d = dict(lap_row)
            try:
                trace = load_trace(data_dir, s, lap_d)
                reference = resolve_reference(
                    conn,
                    data_dir,
                    s,
                    ref,
                    ref_session_key=ref_session or session,
                    ref_lap_index=ref_lap,
                )
                comparison = compare(trace, reference)
            except NotComparableError as exc:
                raise HTTPException(422, str(exc)) from exc
            lap_metrics = load_lap_metrics(data_dir, s, lap_d)
            ref_metrics = None
            if len(reference.sources) == 1:
                src = reference.sources[0]
                src_session = dict(db.get_session(conn, src["session_key"]) or {})
                src_lap = db.get_lap(conn, src["session_key"], src["lap_index"])
                if src_session and src_lap is not None:
                    ref_metrics = load_lap_metrics(data_dir, src_session, dict(src_lap))
            payload = comparison_payload(comparison, s, lap_d, lap_metrics, ref_metrics, step)
            payload["track_model"] = None
            payload["track_model_error"] = None
            payload["corners"] = []
            payload["findings"] = []
            payload["corner_definitions"] = CORNER_DEFINITIONS
            try:
                model = model_for_session(conn, data_dir, s)
            except NotComparableError as exc:
                payload["track_model_error"] = str(exc)
            else:
                analysis = analyse_corners(comparison, model)
                payload["track_model"] = model_payload(model)
                payload["corners"] = [r.to_dict() for r in analysis.segments]
                payload["findings"] = [f.to_dict() for f in lap_findings(analysis)]
        return payload

    @app.get("/api/consistency")
    def consistency(session: str) -> dict[str, Any]:
        with database() as conn:
            s = find_session(conn, session)
            try:
                model = model_for_session(conn, data_dir, s)
            except NotComparableError as exc:
                raise HTTPException(422, str(exc)) from exc
            return {
                "track_model": model_payload(model),
                **session_consistency(conn, data_dir, s, model),
            }

    @app.get("/api/tracks")
    def tracks() -> list[dict[str, Any]]:
        return [model_payload(m) for m in list_models(data_dir)]

    @app.post("/api/tracks/{key}/confirm")
    def confirm_track(key: str) -> dict[str, Any]:
        try:
            return model_payload(confirm_model(data_dir, key))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/live")
    def live_status() -> dict[str, Any]:
        if live is None:
            return {"listening": False, "receiving": False, "error": "live status disabled"}
        return live.snapshot()

    if (STATIC_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    else:

        @app.get("/")
        def no_ui() -> dict[str, str]:
            return {"message": "Pitwall API is running. The web UI has not been built (see web/)."}

    return app
