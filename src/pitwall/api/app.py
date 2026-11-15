"""Local web API and UI host (``pitwall serve``).

JSON endpoints under ``/api``; the built web UI (``web/`` -> ``static/``) is served at ``/``.
Everything runs on this machine and reads the local data directory.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pitwall import __version__
from pitwall.analysis.benchmark import METRICS as BENCHMARK_METRICS
from pitwall.analysis.benchmark import Benchmark, benchmark, place_reference
from pitwall.analysis.compare import REFERENCE_KINDS, Comparison, compare, resolve_reference
from pitwall.analysis.consistency import session_consistency
from pitwall.analysis.corners import DEFINITIONS as CORNER_DEFINITIONS
from pitwall.analysis.corners import analyse_corners
from pitwall.analysis.findings import lap_findings
from pitwall.analysis.metrics import DEFINITIONS, load_lap_metrics
from pitwall.analysis.trace import NotComparableError, check_comparable, load_trace
from pitwall.config import EngineerConfig, ReferenceConfig
from pitwall.engineer import store as narratives
from pitwall.engineer.llm import LLMProvider, make_provider
from pitwall.engineer.narrator import PROMPT_VERSION
from pitwall.engineer.qa import QA_PROMPT_VERSION
from pitwall.engineer.report import render_text
from pitwall.engineer.service import LapContext, ask_lap, build_context, narrate_lap
from pitwall.history import service as history_service
from pitwall.ingest.pipeline import DB_NAME
from pitwall.live import reference as live_refs
from pitwall.live.demo import DemoReplay
from pitwall.live.engine import LiveEngine
from pitwall.reference.archive import Archive, FetchError
from pitwall.reference.laps import ReferenceDataError, classification, parse_lap_rows
from pitwall.reference.service import (
    DEFAULT_SESSION,
    check_pairing,
    event_for,
    fetch_reference,
    load_reference,
    make_archive,
    season_for,
)
from pitwall.reference.store import ref_lap_summary
from pitwall.storage import db
from pitwall.track.model import TrackModel, confirm_model, list_models, load_model, track_key
from pitwall.track.service import model_for_session

STATIC_DIR = Path(__file__).parent / "static"
TRACE_CHANNELS = ("speed", "throttle", "brake", "gear", "steer", "rpm")
FEED_INTERVAL_S = 0.04  # live feed: up to 25 updates a second
HEARTBEAT_S = 1.0
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


def benchmark_payload(
    b: Benchmark,
    session: dict[str, Any],
    lap: dict[str, Any],
    lap_metrics: dict[str, Any],
    model: TrackModel,
    step_m: float,
) -> dict[str, Any]:
    """Comparison payload (same shape as /api/compare, so the charts and map are shared)
    plus the reference's provenance, the registration, and the normalised corner table."""
    payload = comparison_payload(b.comparison, session, lap, lap_metrics, None, step_m)
    ref = b.placed.ref
    reg = b.placed.placement.registration
    payload["reference"] = {
        **payload["reference"],
        "key": ref.key,
        "provider": ref.provider,
        "synthetic": ref.synthetic,
        "season": ref.season,
        "event": ref.event,
        "session": ref.session,
        "session_part": ref.session_part,
        "driver": ref.driver,
        "driver_name": ref.driver_name,
        "driver_number": ref.driver_number,
        "team": ref.team,
        "lap_number": ref.lap_number,
        "official_sectors_ms": list(ref.sectors_ms),
        "sectors_on_your_lines_s": b.ref_sector_times(),
        "compound": ref.compound,
        "tyre_life": ref.tyre_life,
        "track_status": ref.track_status,
        "capabilities": ref.capabilities.to_dict(),
        "checks": ref.checks,
        "fetched_utc": ref.fetched_utc,
        "sources": [
            {k: v for k, v in src.items() if k in ("url", "sha256", "fetched_utc")}
            for src in ref.sources
        ],
    }
    dist = b.comparison.distance
    base_step = float(dist[1] - dist[0]) if len(dist) > 1 else 1.0
    every = max(1, round(step_m / base_step))
    take = np.arange(0, len(dist), every)
    if take[-1] != len(dist) - 1:
        take = np.append(take, len(dist) - 1)
    ch = b.placed.reference.channels
    payload["trace"]["ref_x"] = _arr(ch["x"], 2, take)
    payload["trace"]["ref_z"] = _arr(ch["z"], 2, take)
    payload["trace"]["ref_sample_m"] = [round(float(v), 1) for v in b.placed.samples.d]
    payload["registration"] = {**reg.to_dict(), "along_sigma_m": round(b.placed.along_sigma_m, 2)}
    payload["systematic"] = b.systematic
    payload["segments"] = [r.to_dict() for r in b.segments]
    payload["findings"] = [f.to_dict() for f in b.findings]
    payload["strengths"] = b.strengths
    payload["not_assessed"] = b.not_assessed
    payload["metric_labels"] = {
        k: {"label": v[0], "unit": v[1]} for k, v in BENCHMARK_METRICS.items()
    }
    payload["track_model"] = model_payload(model)
    return payload


class AskBody(BaseModel):
    session: str
    lap: int
    question: str = Field(min_length=1, max_length=500)
    ref: str = "session_best"
    ref_lap: int | None = None
    pro: str | None = None


def create_app(
    data_dir: Path,
    live: LiveEngine | None = None,
    reference: ReferenceConfig | None = None,
    engineer: EngineerConfig | None = None,
    llm: LLMProvider | None = None,
) -> FastAPI:
    data_dir = Path(data_dir)
    ref_cfg = reference or ReferenceConfig()
    eng_cfg = engineer or EngineerConfig()
    provider: LLMProvider = llm or make_provider(
        eng_cfg.provider,
        model=eng_cfg.model,
        base_url=eng_cfg.base_url,
        timeout_s=eng_cfg.timeout_s,
        temperature=eng_cfg.temperature,
        num_ctx=eng_cfg.num_ctx,
        think=eng_cfg.think,
    )

    def archive(season: int, synthetic: bool) -> Archive:
        if synthetic:
            return make_archive(data_dir, season, synthetic=True)
        return make_archive(
            data_dir,
            season,
            base_url=ref_cfg.base_url,
            schedule_url=ref_cfg.schedule_url,
            timeout_s=ref_cfg.timeout_s,
        )

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

    # ------------------------------------------------------------ real-F1 references

    @app.get("/api/references")
    def references(session: str | None = None) -> dict[str, Any]:
        """Stored reference laps; with ``session``, the ones that may be compared with it,
        and where new ones come from (season, event, sessions of the event)."""
        with database() as conn:
            rows = [ref_lap_summary(dict(r)) for r in db.list_ref_laps(conn)]
            if session is None:
                return {"stored": rows}
            s = find_session(conn, session)
        synthetic = bool(s.get("synthetic"))
        try:
            season, event = season_for(s), event_for(s)
        except NotComparableError as exc:
            return {"stored": [], "season": None, "event": None, "error": str(exc)}
        matching = [
            r
            for r in rows
            if r["synthetic"] == synthetic and r["season"] == season and r["event"] == event
        ]
        out: dict[str, Any] = {
            "stored": matching,
            "season": season,
            "event": event,
            "synthetic": synthetic,
            "sessions": [DEFAULT_SESSION],
            "error": None,
        }
        try:
            ev = archive(season, synthetic).find_event(event)
            out["sessions"] = list(ev.sessions) or [DEFAULT_SESSION]
        except (FetchError, LookupError) as exc:
            out["error"] = f"event calendar unavailable: {exc}"
        return out

    @app.get("/api/references/drivers")
    def reference_drivers(session: str, ref_session: str = DEFAULT_SESSION) -> dict[str, Any]:
        """Each driver's fastest lap that can be a reference, for the session's event."""
        with database() as conn:
            s = find_session(conn, session)
        try:
            season, event = season_for(s), event_for(s)
            data = archive(season, bool(s.get("synthetic"))).session_laptimes(event, ref_session)
            rows = classification(parse_lap_rows(data))
        except NotComparableError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (FetchError, ReferenceDataError) as exc:
            raise HTTPException(502, str(exc)) from exc
        return {
            "season": season,
            "event": event,
            "session": ref_session,
            "drivers": [
                {
                    "driver": r.driver,
                    "team": r.team,
                    "lap_number": r.lap_number,
                    "lap_time_ms": r.lap_time_ms,
                    "session_part": r.session_part,
                }
                for r in rows
            ],
        }

    @app.post("/api/references/fetch")
    def reference_fetch(
        session: str,
        driver: str,
        ref_session: str = DEFAULT_SESSION,
        lap: int | None = None,
        part: str | None = None,
    ) -> dict[str, Any]:
        """Download (or read from the cache), check and store a reference lap for the
        session's event: the driver's fastest valid lap unless ``lap`` is given."""
        with database() as conn:
            s = find_session(conn, session)
            try:
                season, event = season_for(s), event_for(s)
                ref = fetch_reference(
                    conn,
                    data_dir,
                    archive(season, bool(s.get("synthetic"))),
                    event,
                    ref_session,
                    driver,
                    lap=lap,
                    part=part,
                )
            except NotComparableError as exc:
                raise HTTPException(422, str(exc)) from exc
            except (ReferenceDataError, LookupError) as exc:
                raise HTTPException(422, str(exc)) from exc
            except FetchError as exc:
                raise HTTPException(502, str(exc)) from exc
            row = db.get_ref_lap(conn, ref.key)
            assert row is not None
            return ref_lap_summary(dict(row))

    @app.get("/api/benchmark")
    def benchmark_lap(
        session: str,
        lap: int,
        ref: str,
        step: float = Query(2.0, ge=1.0, le=20.0),
    ) -> dict[str, Any]:
        with database() as conn:
            s = find_session(conn, session)
            lap_row = db.get_lap(conn, session, lap)
            if lap_row is None:
                raise HTTPException(404, f"lap {lap} not found in session")
            lap_d = dict(lap_row)
            try:
                reference_lap = load_reference(conn, data_dir, ref)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except (FetchError, ReferenceDataError) as exc:
                raise HTTPException(502, str(exc)) from exc
            try:
                check_pairing(s, reference_lap)
                trace = load_trace(data_dir, s, lap_d)
                model = model_for_session(conn, data_dir, s)
                result = benchmark(trace, place_reference(reference_lap, trace), model)
            except NotComparableError as exc:
                raise HTTPException(422, str(exc)) from exc
            lap_metrics = load_lap_metrics(data_dir, s, lap_d)
        return benchmark_payload(result, s, lap_d, lap_metrics, model, step)

    # ------------------------------------------------------------ race engineer

    def lap_context(
        conn: sqlite3.Connection,
        session: str,
        lap: int,
        ref: str,
        ref_lap: int | None,
        pro: str | None,
    ) -> LapContext:
        try:
            return build_context(
                conn, data_dir, session, lap, ref_kind=ref, ref_lap=ref_lap, pro_key=pro or None
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except NotComparableError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (FetchError, ReferenceDataError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/engineer/status")
    def engineer_status() -> dict[str, Any]:
        return {
            **provider.status(),
            "prompt_versions": {"report": PROMPT_VERSION, "answer": QA_PROMPT_VERSION},
        }

    @app.get("/api/engineer/report")
    def engineer_report(
        session: str,
        lap: int,
        ref: str = "session_best",
        ref_lap: int | None = None,
        pro: str | None = None,
    ) -> dict[str, Any]:
        """The deterministic report (no AI): facts, focus, losses, strengths, notes."""
        with database() as conn:
            ctx = lap_context(conn, session, lap, ref, ref_lap, pro)
            stored = [narratives.row_payload(r) for r in db.list_narratives(conn, session, lap, 20)]
        return {**ctx.report.to_dict(), "text": render_text(ctx.report), "history": stored}

    @app.post("/api/engineer/narrate")
    def engineer_narrate(
        session: str,
        lap: int,
        ref: str = "session_best",
        ref_lap: int | None = None,
        pro: str | None = None,
    ) -> dict[str, Any]:
        """AI write-up of the report, checked against the facts (or the measured report
        when it fails the check or no model is available). Stored with its provenance."""
        with database() as conn:
            ctx = lap_context(conn, session, lap, ref, ref_lap, pro)
            result, nid = narrate_lap(conn, ctx, provider)
        return {"narrative_id": nid, **result.to_dict()}

    @app.post("/api/engineer/ask")
    def engineer_ask(body: AskBody) -> dict[str, Any]:
        with database() as conn:
            ctx = lap_context(conn, body.session, body.lap, body.ref, body.ref_lap, body.pro)
            result, nid = ask_lap(conn, data_dir, ctx, provider, body.question)
        return {"narrative_id": nid, **result.to_dict()}

    # ------------------------------------------------------- history (Stage 7)

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        """Totals, every track's summary and trend, open recommendations (the history cache
        is brought up to date first)."""
        with database() as conn:
            return history_service.overview(conn, data_dir)

    @app.get("/api/history")
    def history(group: str, pro: str | None = None) -> dict[str, Any]:
        """One track and car class: sessions over time, trend, corners, recurring patterns,
        recommendation outcomes; with ``pro``, strongest and weakest corners against a stored
        real-F1 lap (normalised)."""
        with database() as conn:
            try:
                return history_service.track_history(conn, data_dir, group, pro_key=pro or None)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(404, str(exc).strip("'\"")) from exc

    @app.post("/api/history/build")
    def history_rebuild(force: bool = False) -> dict[str, Any]:
        """Analyse sessions whose history is missing or out of date (every session with
        ``force``)."""
        with database() as conn:
            result = history_service.rebuild(conn, data_dir, force=force)
            return {**result, "cache": history_service.cache_state(conn)}

    @app.get("/api/live")
    def live_status() -> dict[str, Any]:
        if live is None:
            return {"listening": False, "receiving": False, "error": "live status disabled"}
        return live.snapshot()

    def need_live() -> LiveEngine:
        if live is None:
            raise HTTPException(409, "live telemetry is switched off (pitwall serve --no-live)")
        return live

    @app.websocket("/api/live/ws")
    async def live_feed(ws: WebSocket) -> None:
        """Live state at up to 25 updates a second, plus events (lap completed, report
        ready, reference changed). Each message carries the server's send time (ms) and
        the age of the newest packet, so the page can show how fresh it is."""
        await ws.accept()
        if live is None:
            await ws.send_json({"type": "error", "error": "live telemetry is switched off"})
            await ws.close()
            return
        last_seq, last_sent, last_event = -1, 0.0, live.last_event_id
        try:
            while True:
                now = time.monotonic()
                seq = live.seq
                if seq != last_seq or now - last_sent >= HEARTBEAT_S:
                    await ws.send_json(
                        {"type": "state", "sent_ms": time.time() * 1000, "data": live.snapshot()}
                    )
                    last_seq, last_sent = seq, now
                for ev in live.events_after(last_event):
                    await ws.send_json({"type": "event", "sent_ms": time.time() * 1000, **ev})
                    last_event = ev["id"]
                await asyncio.sleep(FEED_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.get("/api/live/references")
    def live_references() -> dict[str, Any]:
        engine = need_live()
        with database() as conn:
            opts = live_refs.options(conn, engine.session)
        return {
            "options": opts,
            "choice": engine.reference_choice,
            "reference": None if engine.reference is None else engine.reference.summary(),
            "note": engine.reference_note,
        }

    @app.post("/api/live/reference")
    def live_reference(key: str) -> dict[str, Any]:
        engine = need_live()
        if key not in ("auto", "personal_best", "session_best") and not key.startswith("pro:"):
            raise HTTPException(422, f"unknown reference {key!r}")
        engine.choose_reference(key)
        return {"choice": key}

    @app.get("/api/live/track")
    def live_track() -> dict[str, Any]:
        """Your track's outline for the live map: the reference lap's path, else the best
        lap of the session, with the corners of the saved track model."""
        engine = need_live()
        path = engine.track_path()
        if path is None or engine.session is None:
            return {"available": False}
        s = engine.session
        model = load_model(data_dir, track_key(s["track_id"], s["track_name"], s["synthetic"]))
        take = np.arange(0, len(path.distance), 5)
        return {
            "available": True,
            "track_length_m": path.track_length_m,
            "distance": _arr(path.distance, 1, take),
            "x": _arr(path.channels["x"], 2, take),
            "z": _arr(path.channels["z"], 2, take),
            "segments": model_payload(model)["segments"]
            if model is not None and model.fits(s["track_length_m"])
            else [],
        }

    demo: dict[str, DemoReplay] = {}

    @app.post("/api/live/demo")
    def live_demo(
        action: str = "start", speed: float = Query(1.0, ge=0.5, le=20.0)
    ) -> dict[str, Any]:
        """Replay the synthetic demo session into the live engine (marked synthetic)."""
        engine = need_live()
        current = demo.get("replay")
        if action == "stop":
            if current is not None:
                current.stop()
            return {"running": False}
        if action != "start":
            raise HTTPException(422, "action must be start or stop")
        if current is not None and current.running:
            return {"running": True}
        demo["replay"] = DemoReplay(engine, speed=speed)
        demo["replay"].start()
        return {"running": True}

    if (STATIC_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    else:

        @app.get("/")
        def no_ui() -> dict[str, str]:
            return {"message": "Pitwall API is running. The web UI has not been built (see web/)."}

    return app
