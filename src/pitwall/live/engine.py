"""The live engine: forwarded packets in, the Live page's state out, at the game's own rate.

* **Display state** updates with every packet: inputs, speed, gear, rpm, tyres, fuel, ERS,
  lap, sector, position, and the live delta to the chosen reference.
* **Frames** of the player car are joined like the ingest's frame table: Lap Data is the base;
  Car Telemetry, Motion and Motion Ex of the *same frame* are joined to it (a frame is closed
  once three newer frames have arrived, so packets of one frame may arrive in any order).
* **Laps**: a lap ends when the lap number increases; its time is the game's ``lastLapTime``.
  A flashback (``FLBK`` event) erases the frames after the frame it rewinds to, as in the
  ingest. Flags: started at the line, valid, kind (out/in/flying), flashbacks, packet loss;
  only clean laps are usable.
* **Health**: packets per second, Lap Data frames missing, age of the last packet.

Everything here is provisional; ``pitwall ingest`` rebuilds the laps from the raw recording.
"""

from __future__ import annotations

import collections
import socket
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from pitwall.analysis.trace import MAX_START_DISTANCE_M, LapTrace, NotComparableError, build_trace
from pitwall.f1udp import enums, f125
from pitwall.f1udp.decoder import DecodeError, check_packet, decode_event_details
from pitwall.f1udp.header import PacketId
from pitwall.ingest.session_builder import MAX_LOSS_PCT, lap_kind
from pitwall.live.reference import AUTO, LiveReference

DEFAULT_LIVE_PORT = 20778
RECEIVING_TIMEOUT_S = 2.0
REORDER_FRAMES = 3
MAX_EVENTS = 200
MAX_LAPS = 100
RATE_WINDOW_S = 2.0
HEALTH_WINDOW = 600  # Lap Data frames used for the live packet-loss figure

LAP_FIELDS = (
    "last_lap_time_in_ms",
    "lap_distance",
    "current_lap_time_in_ms",
    "current_lap_num",
    "current_lap_invalid",
    "driver_status",
    "pit_status",
    "sector",
    "sector1_time_ms_part",
    "sector1_time_minutes_part",
    "sector2_time_ms_part",
    "sector2_time_minutes_part",
)
TEL_FIELDS = ("speed", "throttle", "brake", "steer", "gear", "engine_rpm", "drs")
MOTION_FIELDS = ("world_position_x", "world_position_z")


def _ms(minutes: Any, ms: Any) -> int:
    return int(minutes) * 60_000 + int(ms)


def _wheels(values: Any, digits: int = 1) -> dict[str, float]:
    return {w: round(float(values[i]), digits) for i, w in enumerate(f125.WHEELS)}


@dataclass
class LiveLap:
    number: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    flashbacks: int = 0
    missing: int = 0
    expected: int = 0
    complete: bool = False
    lap_time_ms: int | None = None
    sectors_ms: tuple[int | None, int | None, int | None] = (None, None, None)
    valid: bool = True
    kind: str = "unknown"
    started_at_line: bool = False
    issues: list[str] = field(default_factory=list)
    usable: bool = False
    delta_s: float | None = None  # at the line, against the reference used during the lap
    reference: str | None = None
    sector_deltas: list[float | None] = field(default_factory=lambda: [None, None, None])
    report: dict[str, Any] | None = None
    trace: LapTrace | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "lap_time_ms": self.lap_time_ms,
            "sectors_ms": list(self.sectors_ms),
            "valid": self.valid,
            "kind": self.kind,
            "usable": self.usable,
            "issues": self.issues,
            "flashbacks": self.flashbacks,
            "loss_pct": round(100 * self.missing / self.expected, 2) if self.expected else 0.0,
            "delta_s": None if self.delta_s is None else round(self.delta_s, 3),
            "sector_deltas": [None if d is None else round(d, 3) for d in self.sector_deltas],
            "reference": self.reference,
            "report": self.report,
        }

    def table(self) -> pa.Table:
        cols: dict[str, list[Any]] = collections.defaultdict(list)
        names = sorted({k for r in self.rows for k in r})
        for r in self.rows:
            for k in names:
                cols[k].append(r.get(k))
        return pa.table({k: pa.array(v) for k, v in cols.items()})


class LiveEngine:
    """Thread-safe: ``observe`` runs on the UDP thread, analyses on a worker thread, and
    snapshots are read by the web server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_LIVE_PORT,
        *,
        data_dir: Path | None = None,
        analyse: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = Path(data_dir) if data_dir else None
        self.analyse = analyse and self.data_dir is not None
        self.error: str | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pitwall-live-work")
        self._seq = 0
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_EVENTS)
        self._event_id = 0
        self._times: collections.deque[float] = collections.deque()
        self._last_packet: float | None = None
        self._decode_errors = 0
        self.reference_choice = AUTO
        self.on_lap: list[Callable[[LiveLap], None]] = []
        self._reset_session(None)
        self._synthetic = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.host, self.port))
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = f"cannot listen on UDP {self.host}:{self.port}: {exc}"
            return
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="pitwall-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._sock is not None:
            self._sock.close()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                payload = self._sock.recv(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            self.observe(payload, time.monotonic())

    def set_synthetic(self, synthetic: bool) -> None:
        """Mark what arrives from now on as synthetic (the demo replay). Switching it on
        starts afresh; switching it off keeps the demo on screen until new data arrives
        (a new session always starts afresh)."""
        with self._lock:
            self._synthetic = synthetic
            if synthetic:
                self._reset_session(None)

    # ------------------------------------------------------------------ session

    def _reset_session(self, info: dict[str, Any] | None) -> None:
        self._uid: str | None = None
        self.session: dict[str, Any] | None = info
        self.laps: list[LiveLap] = []
        self.current: LiveLap | None = None
        self._pending: dict[int, dict[str, Any]] = {}
        self._newest_overall = -1
        self._last_lap_overall: int | None = None
        self._steps: collections.deque[int] = collections.deque(maxlen=60)
        self._loss: collections.deque[tuple[int, int]] = collections.deque(maxlen=HEALTH_WINDOW)
        self._last_frame: int | None = None
        self.state: dict[str, Any] = {}
        self.reference: LiveReference | None = None
        self.reference_note: str | None = None
        self._delta: dict[int, list[tuple[float, float]]] = {}  # lap -> (distance, delta)

    def _event(self, kind: str, **data: Any) -> None:
        self._event_id += 1
        self._events.append({"id": self._event_id, "type": kind, **data})

    def events_after(self, event_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["id"] > event_id]

    @property
    def last_event_id(self) -> int:
        with self._lock:
            return self._event_id

    # ------------------------------------------------------------------ packets

    def observe(self, payload: bytes, now: float) -> None:
        try:
            hdr, dtype = check_packet(payload)
        except DecodeError:
            with self._lock:
                self._decode_errors += 1
            return
        player = hdr.player_car_index
        if player >= f125.MAX_CARS:
            return
        rec = np.frombuffer(payload, dtype, count=1)[0]
        pid = hdr.packet_id
        with self._lock:
            self._seq += 1
            self._last_packet = now
            self._times.append(now)
            while self._times and now - self._times[0] > RATE_WINDOW_S:
                self._times.popleft()
            uid = f"{hdr.session_uid:016x}"
            if uid != self._uid:  # a new session: start afresh
                self._reset_session(None)
                self._uid = uid
            if pid == PacketId.SESSION:
                self._on_session(rec, uid, hdr)
            elif pid == PacketId.EVENT:
                self._on_event(rec)
            elif pid == PacketId.LAP_DATA:
                car = rec["car"][player]
                self._on_lap_data(car, hdr)
                self._add_frame(hdr, {k: car[k].item() for k in LAP_FIELDS}, base=True)
            elif pid == PacketId.CAR_TELEMETRY:
                car = rec["car"][player]
                self._on_telemetry(car)
                self._add_frame(hdr, {k: car[k].item() for k in TEL_FIELDS})
            elif pid == PacketId.MOTION:
                car = rec["car"][player]
                self.state["x"] = round(float(car["world_position_x"]), 2)
                self.state["z"] = round(float(car["world_position_z"]), 2)
                self._add_frame(hdr, {k: car[k].item() for k in MOTION_FIELDS})
            elif pid == PacketId.MOTION_EX:
                slip = rec["player"]["wheel_slip_ratio"]
                self._add_frame(
                    hdr,
                    {f"wheel_slip_ratio_{w}": float(slip[i]) for i, w in enumerate(f125.WHEELS)},
                )
            elif pid == PacketId.CAR_STATUS:
                self._on_status(rec["car"][player])
            elif pid == PacketId.CAR_DAMAGE:
                self.state["tyre_wear_pct"] = _wheels(rec["car"][player]["tyres_wear"])

    def _on_session(self, rec: np.void, uid: str, hdr: Any) -> None:
        track_id = int(rec["track_id"])
        track_name = enums.name(enums.TRACKS, track_id, "track")
        if self._synthetic:
            track_name = "Synthetic test track"  # as the ingest names synthetic sessions
        info = {
            "session_uid": uid,
            "session_key": f"live:{uid}",
            "packet_format": hdr.packet_format,
            "track_id": track_id,
            "track_name": track_name,
            "track_length_m": int(rec["track_length"]),
            "session_type_name": enums.name(enums.SESSION_TYPES, int(rec["session_type"])),
            "formula": int(rec["formula"]),
            "sector2_start_m": round(float(rec["sector2_lap_distance_start"]), 2),
            "sector3_start_m": round(float(rec["sector3_lap_distance_start"]), 2),
            "weather": enums.name(enums.WEATHER, int(rec["weather"])),
            "track_temp_c": int(rec["track_temperature"]),
            "air_temp_c": int(rec["air_temperature"]),
            "synthetic": self._synthetic,
        }
        if self.session is None:
            self.session = info
            self._event("session", session=self._session_summary())
            self._resolve_reference()
        else:
            changed = self.session["track_length_m"] != info["track_length_m"]
            self.session.update(info)
            if changed:
                self._resolve_reference()

    def _session_summary(self) -> dict[str, Any] | None:
        s = self.session
        if s is None:
            return None
        return {k: v for k, v in s.items() if k != "packet_format"}

    def _on_event(self, rec: np.void) -> None:
        code = rec["event_string_code"].decode(errors="replace")
        if code == "FLBK":
            target = decode_event_details(code, rec["event_details"].tobytes())
            self._flashback(int(target["flashback_frame_identifier"]))
        elif code == "SSTA" and self.session is not None:
            self._event("session_started")

    def _flashback(self, frame: int) -> None:
        """Erase every frame after ``frame``, as the ingest does (the future it rewinds)."""
        self._pending = {k: v for k, v in self._pending.items() if v["frame_identifier"] <= frame}
        lap = self.current
        if lap is not None and lap.rows and lap.rows[0]["frame_identifier"] > frame and self.laps:
            # rewound into the previous lap: it is not complete any more
            prev = self.laps.pop()
            prev.complete, prev.lap_time_ms, prev.report = False, None, None
            self.current = lap = prev
            self._event("lap_undone", lap=prev.number)
        if lap is not None:
            lap.rows = [r for r in lap.rows if r["frame_identifier"] <= frame]
            lap.flashbacks += 1
        self._last_frame = frame
        self._event("flashback", frame=frame, lap=None if lap is None else lap.number)

    def _on_lap_data(self, car: np.void, hdr: Any) -> None:
        st = self.state
        d = float(car["lap_distance"])
        t_ms = int(car["current_lap_time_in_ms"])
        lap_num = int(car["current_lap_num"])
        st.update(
            lap_number=lap_num,
            lap_distance_m=round(d, 1),
            current_lap_time_ms=t_ms,
            last_lap_time_ms=int(car["last_lap_time_in_ms"]) or None,
            sector=int(car["sector"]) + 1,
            lap_invalid=bool(car["current_lap_invalid"]),
            s1_ms=_ms(car["sector1_time_minutes_part"], car["sector1_time_ms_part"]) or None,
            s2_ms=_ms(car["sector2_time_minutes_part"], car["sector2_time_ms_part"]) or None,
            pit_status=int(car["pit_status"]),
        )
        self._update_delta(lap_num, d, t_ms / 1000)

    def _on_telemetry(self, car: np.void) -> None:
        self.state.update(
            speed_kmh=int(car["speed"]),
            gear=int(car["gear"]),
            rpm=int(car["engine_rpm"]),
            throttle=round(float(car["throttle"]), 3),
            brake=round(float(car["brake"]), 3),
            steer=round(float(car["steer"]), 3),
            drs=bool(car["drs"]),
            rev_lights_pct=int(car["rev_lights_percent"]),
            tyre_surface_c=_wheels(car["tyres_surface_temperature"], 0),
            tyre_inner_c=_wheels(car["tyres_inner_temperature"], 0),
            tyre_pressure_psi=_wheels(car["tyres_pressure"], 1),
            brake_temp_c=_wheels(car["brakes_temperature"], 0),
        )

    def _on_status(self, car: np.void) -> None:
        self.state.update(
            fuel_kg=round(float(car["fuel_in_tank"]), 2),
            fuel_laps=round(float(car["fuel_remaining_laps"]), 2),
            ers_store_mj=round(float(car["ers_store_energy"]) / 1e6, 3),
            ers_mode=int(car["ers_deploy_mode"]),
            max_rpm=int(car["max_rpm"]),
            drs_allowed=bool(car["drs_allowed"]),
            tyre_compound=enums.name(enums.VISUAL_COMPOUNDS, int(car["visual_tyre_compound"])),
            tyre_age_laps=int(car["tyres_age_laps"]),
            abs=bool(car["anti_lock_brakes"]),
            traction_control=int(car["traction_control"]),
        )

    # ------------------------------------------------------------------ frames and laps

    def _add_frame(self, hdr: Any, fields: dict[str, Any], base: bool = False) -> None:
        ov = int(hdr.overall_frame_identifier)
        row = self._pending.get(ov)
        if row is None:
            row = self._pending[ov] = {
                "overall_frame_identifier": ov,
                "frame_identifier": int(hdr.frame_identifier),
            }
        row.update(fields)
        if base:
            row["_base"] = True
        self._newest_overall = max(self._newest_overall, ov)
        ready = sorted(k for k in self._pending if k <= self._newest_overall - REORDER_FRAMES)
        for k in ready:
            done = self._pending.pop(k)
            if done.pop("_base", False):
                self._on_frame(done)

    def flush(self) -> None:
        """Close every pending frame (end of a replay or a stream)."""
        with self._lock:
            for k in sorted(self._pending):
                done = self._pending.pop(k)
                if done.pop("_base", False):
                    self._on_frame(done)

    def _on_frame(self, row: dict[str, Any]) -> None:
        frame = row["frame_identifier"]
        if self._last_frame is not None and frame < self._last_frame:
            # frames went back without a flashback event: a reset, the lap is lost
            self.current = None
            self._event("reset")
        self._last_frame = frame
        ov = row["overall_frame_identifier"]
        if self._last_lap_overall is not None:
            step = ov - self._last_lap_overall
            if 0 < step < 600:
                self._steps.append(step)
                cadence = int(statistics.median(self._steps)) if self._steps else 1
                missing = max(0, round(step / max(1, cadence)) - 1)
                self._loss.append((missing, missing + 1))
                if self.current is not None:
                    self.current.missing += missing
                    self.current.expected += missing + 1
        self._last_lap_overall = ov
        n = int(row["current_lap_num"])
        lap = self.current
        if lap is not None and n == lap.number + 1 and lap.rows:
            self._complete(lap, row)
            lap = None
        if lap is None or n != lap.number:
            lap = self.current = LiveLap(number=n)
        lap.rows.append(row)

    def _complete(self, lap: LiveLap, next_row: dict[str, Any]) -> None:
        """``next_row`` is the first frame of the next lap: it carries the lap time."""
        lap.complete = True
        lap.lap_time_ms = int(next_row.get("last_lap_time_in_ms") or 0) or None
        last = lap.rows[-1]
        s1 = _ms(last["sector1_time_minutes_part"], last["sector1_time_ms_part"]) or None
        s2 = _ms(last["sector2_time_minutes_part"], last["sector2_time_ms_part"]) or None
        s3 = lap.lap_time_ms - s1 - s2 if lap.lap_time_ms and s1 and s2 else None
        lap.sectors_ms = (s1, s2, s3)
        lap.valid = not any(r["current_lap_invalid"] for r in lap.rows)
        lap.kind = lap_kind(
            np.array([r["driver_status"] for r in lap.rows]),
            np.array([r["pit_status"] for r in lap.rows]),
        )
        lap.started_at_line = lap.rows[0]["lap_distance"] <= MAX_START_DISTANCE_M
        loss = 100 * lap.missing / lap.expected if lap.expected else 0.0
        issues = []
        if not lap.lap_time_ms:
            issues.append("no lap time")
        if not lap.valid:
            issues.append("invalid lap (track limits / corner cutting)")
        if lap.kind not in ("flying", "on_track"):
            issues.append(f"{lap.kind} lap")
        if not lap.started_at_line:
            issues.append("did not start at the line")
        if lap.flashbacks:
            issues.append(f"flashback used ({lap.flashbacks}x)")
        if loss > MAX_LOSS_PCT:
            issues.append(f"packet loss {loss:.1f} %")
        lap.issues = issues
        lap.usable = not issues
        ref = self.reference
        if ref is not None and lap.lap_time_ms and lap.number in self._delta:
            # at the line: the official lap time, so it equals the post-lap delta
            lap.delta_s = lap.lap_time_ms / 1000 - ref.lap_time_s
            lap.reference = ref.label
            s1d, s2d = lap.sector_deltas[0], lap.sector_deltas[1]
            if s1d is not None and s2d is not None:
                lap.sector_deltas[2] = lap.delta_s - s1d - s2d
        self.laps.append(lap)
        del self.laps[:-MAX_LAPS]
        self._event("lap_completed", lap=lap.summary())
        for cb in self.on_lap:
            cb(lap)
        if lap.lap_time_ms:
            self._executor.submit(self._after_lap, lap)

    # ------------------------------------------------------------------ live delta

    def _update_delta(self, lap_num: int, d: float, t: float) -> None:
        """Delta to the reference at this distance; sector deltas when a line is crossed.
        A lap is only compared if it was followed from the start line."""
        ref = self.reference
        st = self.state
        samples = self._delta.get(lap_num)
        if samples is None:
            if d > MAX_START_DISTANCE_M:
                st["delta_s"] = st["predicted_lap_ms"] = None
                return
            samples = self._delta[lap_num] = []
            for old in [k for k in self._delta if k < lap_num - 2]:
                del self._delta[old]
        lap = self.current if self.current is not None and self.current.number == lap_num else None
        bounds = (
            (self.session["sector2_start_m"], self.session["sector3_start_m"])
            if self.session
            else ()
        )
        if samples and d < samples[-1][0] - 0.5:  # a flashback went back along the lap
            samples[:] = [x for x in samples if x[0] <= d]
            if lap is not None:
                for k, b in enumerate(bounds):
                    if b > d:
                        lap.sector_deltas[k] = None
        if ref is None or not 0 <= d <= ref.track_length_m:
            st["delta_s"] = st["predicted_lap_ms"] = None
            return
        delta = t - ref.time_at(d)
        st["delta_s"] = round(delta, 3)
        st["predicted_lap_ms"] = round((ref.lap_time_s + delta) * 1000)
        prev = samples[-1] if samples else None
        samples.append((d, delta))
        if lap is not None and prev is not None:
            for k, b in enumerate(bounds):
                if prev[0] < b <= d and lap.sector_deltas[k] is None:
                    f = (b - prev[0]) / (d - prev[0]) if d > prev[0] else 1.0
                    at_b = prev[1] + f * (delta - prev[1])
                    before = sum(x for x in lap.sector_deltas[:k] if x is not None)
                    lap.sector_deltas[k] = at_b - before
        st["sector_deltas"] = [
            None if x is None else round(x, 3)
            for x in (lap.sector_deltas if lap is not None else [None, None, None])
        ]

    # ------------------------------------------------------------------ worker

    def _after_lap(self, lap: LiveLap) -> None:
        """Worker thread: build the lap's trace, refresh the session best, analyse."""
        from pitwall.live.analysis import analyse_lap

        with self._lock:
            session = dict(self.session) if self.session else None
            if not lap.usable:
                lap.report = {
                    "status": "skipped",
                    "reason": "not analysed: " + ", ".join(lap.issues),
                }
                self._seq += 1
                self._event("report", lap=lap.number, report=lap.report)
                return
            table = lap.table()
        if session is None:
            return
        try:
            trace = build_trace(
                table.append_column("lap_index", pa.array([lap.number] * table.num_rows)),
                session_key=session["session_key"],
                lap_index=lap.number,
                lap_number=lap.number,
                lap_time_ms=int(lap.lap_time_ms or 0),
                track_length_m=session["track_length_m"],
                sector_starts_m=(session["sector2_start_m"], session["sector3_start_m"]),
                official_sectors_ms=lap.sectors_ms,
            )
        except (NotComparableError, ValueError, KeyError) as exc:
            with self._lock:
                lap.report = {"status": "skipped", "reason": f"no trace: {exc}"}
                self._event("report", lap=lap.number, report=lap.report)
            return
        with self._lock:
            lap.trace = trace
        if lap.usable and self.reference_choice in (AUTO, "session_best"):
            self._resolve_reference(worker=True)
        if not self.analyse:
            return
        report = analyse_lap(self, lap, trace, session)
        with self._lock:
            lap.report = report
            self._seq += 1
            self._event("report", lap=lap.number, report=report)

    def _resolve_reference(self, worker: bool = False) -> None:
        if self.data_dir is None and self.reference_choice == AUTO:
            # no database: the session's best lap is the only possible reference
            self._set_reference(self._session_best())
            return
        if not worker:
            self._executor.submit(self._resolve_reference, True)
            return
        from pitwall.live import reference as refs
        from pitwall.storage import db

        with self._lock:
            session = dict(self.session) if self.session else None
            choice = self.reference_choice
        if session is None:
            return
        result: LiveReference | None = None
        note = None
        conn = db.connect(self.data_dir / "pitwall.db") if self.data_dir else None
        try:
            if choice in (AUTO, "personal_best") and conn is not None:
                result = refs.personal_best(conn, self.data_dir, session)  # type: ignore[arg-type]
            if result is None and choice in (AUTO, "session_best"):
                result = self._session_best()
            if choice.startswith("pro:") and conn is not None:
                path = self._best_path(conn, session)
                if path is None:
                    note = "a real F1 lap needs one of your laps on this track to be placed"
                else:
                    try:
                        result = refs.pro(conn, self.data_dir, session, choice[4:], path)  # type: ignore[arg-type]
                    except (LookupError, NotComparableError) as exc:
                        note = str(exc)
            if result is None and note is None:
                note = {
                    "personal_best": "no ingested lap on this track yet",
                    "session_best": "no clean lap in this session yet",
                }.get(choice, "no reference yet: complete a clean lap, or ingest earlier laps")
        finally:
            if conn is not None:
                conn.close()
        with self._lock:
            if self.session is not None and self.session["session_uid"] == session["session_uid"]:
                self._set_reference(result, note)

    def _best_path(self, conn: Any, session: dict[str, Any]) -> LapTrace | None:
        from pitwall.live import reference as refs

        pb = refs.personal_best(conn, self.data_dir, session)  # type: ignore[arg-type]
        if pb is not None and pb.trace is not None:
            return pb.trace
        best = self._session_best()
        return best.trace if best is not None else None

    def _session_best(self) -> LiveReference | None:
        from pitwall.live.reference import from_trace

        with self._lock:
            done = [x for x in self.laps if x.usable and x.trace is not None and x.lap_time_ms]
        if not done:
            return None
        best = min(done, key=lambda x: x.lap_time_ms or 0)
        assert best.trace is not None
        return from_trace(
            best.trace, "session_best", "session_best", f"session best (lap {best.number})"
        )

    def track_path(self) -> LapTrace | None:
        """A lap of this track with positions: the reference's, else the session's best or
        last analysed lap (for the live map)."""
        with self._lock:
            if self.reference is not None and self.reference.trace is not None:
                return self.reference.trace
            done = [x for x in self.laps if x.trace is not None]
        best = self._session_best()
        if best is not None and best.trace is not None:
            return best.trace
        return done[-1].trace if done else None

    def _set_reference(self, ref: LiveReference | None, note: str | None = None) -> None:
        with self._lock:
            old = None if self.reference is None else self.reference.summary()
            self.reference = ref
            self.reference_note = note if ref is None else None
            new = None if ref is None else ref.summary()
            if new != old:
                self._seq += 1
                self._event("reference", reference=new, note=self.reference_note)

    def choose_reference(self, key: str) -> None:
        with self._lock:
            self.reference_choice = key
        self._executor.submit(self._resolve_reference, True)

    # ------------------------------------------------------------------ snapshot

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self._lock:
            age = None if self._last_packet is None else now - self._last_packet
            recent = [t for t in self._times if now - t <= RATE_WINDOW_S]
            missing = sum(m for m, _ in self._loss)
            expected = sum(e for _, e in self._loss)
            st = dict(self.state)
            cur = self.current
            return {
                "seq": self._seq,
                "listening": self._sock is not None and self.error is None,
                "port": self.port,
                "error": self.error,
                "receiving": age is not None and age <= RECEIVING_TIMEOUT_S,
                "last_packet_age_s": None if age is None else round(age, 3),
                "packets_per_s": round(len(recent) / RATE_WINDOW_S, 1),
                "loss_pct": round(100 * missing / expected, 2) if expected else 0.0,
                "decode_errors": self._decode_errors,
                "session": self._session_summary(),
                "track_name": self.session["track_name"] if self.session else None,
                "session_type_name": (self.session["session_type_name"] if self.session else None),
                "reference": None if self.reference is None else self.reference.summary(),
                "reference_choice": self.reference_choice,
                "reference_note": self.reference_note,
                "current_lap": None
                if cur is None
                else {
                    "number": cur.number,
                    "flashbacks": cur.flashbacks,
                    "started_at_line": bool(cur.rows)
                    and cur.rows[0]["lap_distance"] <= MAX_START_DISTANCE_M,
                },
                "laps": [lap.summary() for lap in self.laps[-20:]],
                **st,
            }

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq
