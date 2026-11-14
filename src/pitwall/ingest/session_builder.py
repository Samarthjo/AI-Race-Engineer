"""Build one session: canonical player-car frames, laps, and session metadata (L1 + L2).

Lap rules (all from game data, nothing inferred from looks):

* A lap is a run of canonical Lap Data rows with the same ``currentLapNum`` inside one
  timeline segment. It is **complete** when the next row (same segment) has lap number + 1.
* Lap time: the Session History packet (authoritative), else ``lastLapTimeInMS`` reported on
  the first row of the next lap. If both exist and differ, it is reported in ``notes``.
* Validity: Session History lap-valid flag, else "``currentLapInvalid`` never set".
* Kind (out / in / flying / on_track / garage): from ``driverStatus`` and ``pitStatus``.
* **usable** = complete, valid, flying (or racing) lap, no flashback, packet loss <= 2 %,
  no data gap > 30 m. Blocking reasons are listed in ``issues``; non-blocking remarks
  (pauses, assists on, time-source disagreement) in ``notes``.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from pitwall.f1udp import enums, f125
from pitwall.f1udp.decoder import to_python
from pitwall.f1udp.header import PacketId, packet_name
from pitwall.ingest.collector import SessionCollection, StreamData
from pitwall.ingest.timeline import Timeline, canonicalize, count_missing
from pitwall.ingest.timeline import cadence as stream_cadence

MAX_LOSS_PCT = 2.0
MAX_DISTANCE_GAP_M = 30.0
PAUSE_WALL_GAP_NS = 1_000_000_000

# Joined into the per-frame table by exact frame (the base is Lap Data).
JOIN_EXACT = (PacketId.CAR_TELEMETRY, PacketId.MOTION, PacketId.CAR_STATUS, PacketId.MOTION_EX)
# Lower-rate stream joined "as of" the frame (latest value at or before it).
JOIN_ASOF = (PacketId.CAR_DAMAGE,)

SETTINGS_FIELDS = (
    "rule_set",
    "equal_car_performance",
    "recovery_mode",
    "flashback_limit",
    "surface_type",
    "low_fuel_mode",
    "race_starts",
    "tyre_temperature",
    "pit_lane_tyre_sim",
    "car_damage",
    "car_damage_rate",
    "collisions",
    "corner_cutting_stringency",
    "parc_ferme_rules",
    "safety_car",
    "red_flags",
    "session_length",
)
SESSION_ASSIST_FIELDS = (
    "steering_assist",
    "braking_assist",
    "gearbox_assist",
    "pit_assist",
    "pit_release_assist",
    "ers_assist",
    "drs_assist",
    "dynamic_racing_line",
    "dynamic_racing_line_type",
)


@dataclass
class CanonicalStream:
    data: StreamData
    timeline: Timeline
    cadence: int

    @property
    def rows(self) -> np.ndarray:
        return self.data.rows[self.timeline.keep]

    @property
    def header(self) -> np.ndarray:
        return self.data.header[self.timeline.keep]

    @property
    def wall_ns(self) -> np.ndarray:
        return self.data.wall_ns[self.timeline.keep]

    @property
    def overall(self) -> np.ndarray:
        return self.header["overall_frame_identifier"].astype(np.int64)


@dataclass
class Lap:
    lap_index: int
    lap_number: int
    segment: int
    kind: str
    complete: bool
    valid: bool | None
    usable: bool
    lap_time_ms: int | None
    lap_time_source: str | None
    s1_ms: int | None
    s2_ms: int | None
    s3_ms: int | None
    valid_history: bool | None
    invalid_flag_seen: bool
    flashbacks: int
    pauses: int
    frames: int
    missing_frames: int
    loss_pct: float
    max_distance_gap_m: float
    start_session_time: float
    end_session_time: float
    start_frame: int
    end_frame: int
    start_distance_m: float
    end_distance_m: float
    tyre_actual: int | None
    tyre_visual: int | None
    tyre_age_laps: int | None
    fuel_start: float | None
    fuel_end: float | None
    tyre_wear_start: list[float] | None
    tyre_wear_end: list[float] | None
    assists: dict[str, Any]
    setup: dict[str, Any] | None
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class BuiltSession:
    session_uid: int
    frames: pa.Table
    laps: list[Lap]
    summary: dict[str, Any]
    details: dict[str, Any]


# ------------------------------------------------------------------------- helpers


def flatten(rows: np.ndarray) -> dict[str, np.ndarray]:
    """Structured player records -> flat columns; per-wheel arrays become name_rl/_rr/_fl/_fr."""
    out: dict[str, np.ndarray] = {}
    for name in rows.dtype.names or ():
        col = rows[name]
        if col.dtype.names is not None:
            continue  # nested records (not present in the high-rate streams)
        if col.ndim == 2 and col.shape[1] == 4:
            for i, wheel in enumerate(f125.WHEELS):
                out[f"{name}_{wheel}"] = np.ascontiguousarray(col[:, i])
        elif col.ndim == 1:
            out[name] = np.ascontiguousarray(col)
    return out


def _ms(minutes: Any, ms: Any) -> int:
    return int(minutes) * 60_000 + int(ms)


def _round(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(float(x), nd)


def _iso(wall_ns: int) -> str:
    secs, ns = divmod(int(wall_ns), 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{ns // 1_000_000:03d}Z"


def _setup_dict(setup: np.void) -> dict[str, Any]:
    d = to_python(setup)
    tp = d.pop("tyre_pressure")
    for i, w in enumerate(f125.WHEELS):
        d[f"tyre_pressure_{w}"] = tp[i]
    return d


def history_laps(hist: np.void | None) -> dict[int, dict[str, Any]]:
    """Lap number -> {time, sectors, flags} from a Session History packet."""
    if hist is None:
        return {}
    out: dict[int, dict[str, Any]] = {}
    n = int(hist["num_laps"])
    for i in range(min(n, f125.MAX_LAPS_IN_HISTORY)):
        h = hist["lap_history_data"][i]
        t = int(h["lap_time_in_ms"])
        if t <= 0:
            continue  # current (unfinished) lap
        out[i + 1] = {
            "lap_time_ms": t,
            # 0 means "not measured" (e.g. out-laps): stored as None, never as a 0 s sector
            "s1_ms": _ms(h["sector1_time_minutes_part"], h["sector1_time_ms_part"]) or None,
            "s2_ms": _ms(h["sector2_time_minutes_part"], h["sector2_time_ms_part"]) or None,
            "s3_ms": _ms(h["sector3_time_minutes_part"], h["sector3_time_ms_part"]) or None,
            "flags": int(h["lap_valid_bit_flags"]),
        }
    return out


def lap_kind(driver_status: np.ndarray, pit_status: np.ndarray) -> str:
    if (driver_status == 2).any() or pit_status[-1] != 0:
        return "in"
    if (driver_status == 3).any() or pit_status[0] != 0:
        return "out"
    status, _ = Counter(driver_status.tolist()).most_common(1)[0]
    return {0: "garage", 1: "flying", 4: "on_track"}.get(status, "unknown")


def _window(overall: np.ndarray, lo: int, hi: int) -> slice:
    """Rows of an increasing ``overall`` array within [lo, hi]."""
    return slice(
        int(np.searchsorted(overall, lo, "left")), int(np.searchsorted(overall, hi, "right"))
    )


def _increasing(a: np.ndarray) -> bool:
    return bool(len(a) < 2 or np.all(np.diff(a) > 0))


def exact_index(base: np.ndarray, other: np.ndarray) -> np.ndarray:
    """For each base value, the index of the equal value in ``other`` (or -1)."""
    if len(other) == 0:
        return np.full(len(base), -1, dtype=np.int64)
    if _increasing(other):
        pos = np.searchsorted(other, base)
        pos_c = np.clip(pos, 0, len(other) - 1)
        return np.where(other[pos_c] == base, pos_c, -1).astype(np.int64)
    lookup = {int(v): i for i, v in enumerate(other.tolist())}
    return np.asarray([lookup.get(int(v), -1) for v in base.tolist()], dtype=np.int64)


def asof_index(base: np.ndarray, other: np.ndarray) -> np.ndarray:
    """For each base value, the index of the last ``other`` value <= it (or -1)."""
    if len(other) == 0:
        return np.full(len(base), -1, dtype=np.int64)
    order = np.argsort(other, kind="stable")
    pos = np.searchsorted(other[order], base, "right") - 1
    return np.where(pos >= 0, order[np.clip(pos, 0, None)], -1).astype(np.int64)


# ---------------------------------------------------------------------------- build


def canonical_streams(c: SessionCollection) -> dict[int, CanonicalStream]:
    targets = [
        int(e.details["flashback_frame_identifier"])
        for e in c.events
        if e.code == "FLBK" and "flashback_frame_identifier" in e.details
    ]
    out: dict[int, CanonicalStream] = {}
    for pid, data in c.finish_streams().items():
        if len(data) == 0:
            continue
        step = stream_cadence(data.header["overall_frame_identifier"])
        tl = canonicalize(data.header["frame_identifier"], targets, tolerance=max(10, 3 * step))
        out[pid] = CanonicalStream(data, tl, step)
    return out


def build_session(c: SessionCollection) -> BuiltSession | None:
    """Laps + frame table + metadata for one session, or None if it has no Lap Data."""
    streams = canonical_streams(c)
    base = streams.get(PacketId.LAP_DATA)
    if base is None or len(base.timeline.keep) == 0:
        return None

    base_time = base.header["session_time"].astype(np.float64)
    base_wall = base.wall_ns
    wall_gap = np.diff(base_wall, prepend=base_wall[:1])
    time_gap = np.diff(base_time, prepend=base_time[:1])
    # Paused: wall clock moved > 1 s while game time barely moved.
    paused_before = (wall_gap > PAUSE_WALL_GAP_NS) & (time_gap < 0.5 * wall_gap / 1e9)
    # A flashback also leaves a wall-clock gap (the erased driving); that is not a pause.
    paused_before &= ~base.timeline.after_rewind

    session_recs = c.session_records()
    laps, spans = _build_laps(c, streams, base, base_time, paused_before, session_recs)

    lap_index = np.full(len(base.timeline.keep), -1, dtype=np.int32)
    for lap, (s, e) in zip(laps, spans, strict=True):
        lap_index[s:e] = lap.lap_index

    frames = _frames_table(base, lap_index, paused_before, streams)
    summary, details = _session_meta(c, session_recs, laps, streams)
    return BuiltSession(c.session_uid, frames, laps, summary, details)


def _build_laps(
    c: SessionCollection,
    streams: dict[int, CanonicalStream],
    base: CanonicalStream,
    stime: np.ndarray,
    paused_before: np.ndarray,
    session_recs: np.ndarray,
) -> tuple[list[Lap], list[tuple[int, int]]]:
    rows = base.rows
    overall = base.overall
    frame_ids = base.header["frame_identifier"].astype(np.int64)
    segment = base.timeline.segment
    after_rewind = base.timeline.after_rewind
    lapnum = rows["current_lap_num"].astype(np.int64)
    n = len(rows)

    breaks = np.flatnonzero((np.diff(segment) != 0) | (np.diff(lapnum) != 0)) + 1
    starts = [0, *breaks.tolist()]
    ends = [*breaks.tolist(), n]
    complete_flags = [
        bool(e < n and segment[e] == segment[s] and lapnum[e] == lapnum[s] + 1)
        for s, e in zip(starts, ends, strict=True)
    ]
    # The last complete occurrence of a lap number owns its Session History entry.
    last_complete_for: dict[int, int] = {}
    for i, (s, ok) in enumerate(zip(starts, complete_flags, strict=True)):
        if ok:
            last_complete_for[int(lapnum[s])] = i

    history = history_laps(c.history.get(c.player_car_index))
    telemetry = streams.get(PacketId.CAR_TELEMETRY)
    status = streams.get(PacketId.CAR_STATUS)
    damage = streams.get(PacketId.CAR_DAMAGE)
    tel_overall = telemetry.overall if telemetry is not None else np.empty(0, np.int64)
    tel_ok = telemetry is not None and _increasing(tel_overall)
    st_overall = status.overall if status is not None else np.empty(0, np.int64)
    dm_overall = damage.overall if damage is not None else np.empty(0, np.int64)
    sess_overall = (
        session_recs["header"]["overall_frame_identifier"].astype(np.int64)
        if len(session_recs)
        else np.empty(0, np.int64)
    )

    laps: list[Lap] = []
    spans: list[tuple[int, int]] = []
    for i, (s, e) in enumerate(zip(starts, ends, strict=True)):
        r = rows[s:e]
        num = int(lapnum[s])
        complete = complete_flags[i]
        issues: list[str] = []
        notes: list[str] = []

        # ---- lap time and sectors
        lapdata_time = int(rows["last_lap_time_in_ms"][e]) if complete else None
        hist = history.get(num) if last_complete_for.get(num) == i else None
        lap_time: int | None = None
        source: str | None = None
        s1 = s2 = s3 = None
        if hist is not None:
            lap_time, source = hist["lap_time_ms"], "session_history"
            s1, s2, s3 = hist["s1_ms"], hist["s2_ms"], hist["s3_ms"]
            if lapdata_time and lapdata_time != lap_time:
                notes.append(
                    f"lap time sources disagree: history {lap_time} ms, lap data {lapdata_time} ms"
                )
        elif lapdata_time:
            lap_time, source = lapdata_time, "lap_data"
            last = r[-1]
            s1 = _ms(last["sector1_time_minutes_part"], last["sector1_time_ms_part"]) or None
            s2 = _ms(last["sector2_time_minutes_part"], last["sector2_time_ms_part"]) or None
            if s1 and s2 and lap_time > s1 + s2:
                s3 = lap_time - s1 - s2

        invalid_seen = bool((r["current_lap_invalid"] != 0).any())
        valid_history = bool(hist["flags"] & 0x01) if hist is not None else None
        valid: bool | None
        if not complete:
            valid = None
        elif valid_history is not None:
            valid = valid_history
            if valid_history and invalid_seen:
                notes.append("invalid flag seen during the lap, but history marks it valid")
        else:
            valid = not invalid_seen

        kind = lap_kind(r["driver_status"], r["pit_status"])

        # ---- data quality
        ov = overall[s:e]
        skip = after_rewind[s:e]
        paused = paused_before[s:e].copy()
        paused[0] = False
        flashbacks = int(skip.sum())
        pauses = int(paused.sum())
        missing = count_missing(ov, base.cadence, skip, paused)
        loss = 100.0 * missing / (len(ov) + missing)
        if telemetry is not None and tel_ok:
            w = _window(tel_overall, int(ov[0]), int(ov[-1]))
            t_missing = count_missing(
                tel_overall[w], telemetry.cadence, telemetry.timeline.after_rewind[w]
            )
            t_n = w.stop - w.start
            t_loss = 100.0 * t_missing / (t_n + t_missing) if t_n else 100.0
            if t_loss > loss:
                loss, missing = t_loss, t_missing
        dist = r["lap_distance"].astype(np.float64)
        ddist = np.diff(dist)
        max_gap = float(ddist[ddist > 0].max()) if (ddist > 0).any() else 0.0

        # ---- context: tyres, fuel, wear, assists, setup
        tyre_actual = tyre_visual = tyre_age = None
        fuel_start = fuel_end = None
        tc_max = abs_max = None
        if status is not None:
            w = _window(st_overall, int(ov[0]), int(ov[-1]))
            sr = status.rows[w]
            if len(sr):
                tyre_actual = int(sr["actual_tyre_compound"][0])
                tyre_visual = int(sr["visual_tyre_compound"][0])
                tyre_age = int(sr["tyres_age_laps"][0])
                fuel_start = float(sr["fuel_in_tank"][0])
                fuel_end = float(sr["fuel_in_tank"][-1])
                tc_max = int(sr["traction_control"].max())
                abs_max = int(sr["anti_lock_brakes"].max())
        wear_start = wear_end = None
        if damage is not None and len(dm_overall):
            i0 = int(asof_index(np.asarray([ov[0]]), dm_overall)[0])
            i1 = int(asof_index(np.asarray([ov[-1]]), dm_overall)[0])
            if i0 >= 0:
                wear_start = [round(float(x), 3) for x in damage.rows["tyres_wear"][i0]]
            if i1 >= 0:
                wear_end = [round(float(x), 3) for x in damage.rows["tyres_wear"][i1]]

        assists: dict[str, Any] = {"traction_control": tc_max, "anti_lock_brakes": abs_max}
        if len(sess_overall):
            j = max(0, int(asof_index(np.asarray([ov[-1]]), sess_overall)[0]))
            srec = session_recs[j]
            for key in SESSION_ASSIST_FIELDS:
                assists[key] = int(srec[key])
        control_on = [
            label
            for label, on in (
                ("traction control", (tc_max or 0) > 0),
                ("ABS", (abs_max or 0) > 0),
                ("braking assist", assists.get("braking_assist", 0) > 0),
                ("steering assist", assists.get("steering_assist", 0) > 0),
                ("automatic gearbox", assists.get("gearbox_assist", 0) == 3),
            )
            if on
        ]
        assists["control_assists_on"] = control_on
        if control_on:
            notes.append("assists on: " + ", ".join(control_on))

        setup = None
        if c.setups:
            chosen = c.setups[0]
            for ch in c.setups:
                if ch.overall_frame_identifier <= int(ov[0]):
                    chosen = ch
            setup = _setup_dict(chosen.setup)

        # ---- usability
        if not complete:
            issues.append("incomplete lap")
        if kind not in ("flying", "on_track"):
            issues.append(f"{kind} lap")
        if valid is False:
            issues.append("invalid lap (track limits / corner cutting)")
        if flashbacks:
            issues.append(f"flashback used ({flashbacks}x)")
        if loss > MAX_LOSS_PCT:
            issues.append(f"packet loss {loss:.1f}%")
        if max_gap > MAX_DISTANCE_GAP_M:
            issues.append(f"data gap of {max_gap:.0f} m")
        if complete and lap_time is None:
            issues.append("no lap time reported")
        if pauses:
            notes.append(f"game paused {pauses}x")

        laps.append(
            Lap(
                lap_index=i,
                lap_number=num,
                segment=int(segment[s]),
                kind=kind,
                complete=complete,
                valid=valid,
                usable=not issues,
                lap_time_ms=lap_time,
                lap_time_source=source,
                s1_ms=s1,
                s2_ms=s2,
                s3_ms=s3,
                valid_history=valid_history,
                invalid_flag_seen=invalid_seen,
                flashbacks=flashbacks,
                pauses=pauses,
                frames=e - s,
                missing_frames=int(missing),
                loss_pct=round(loss, 3),
                max_distance_gap_m=round(max_gap, 2),
                start_session_time=round(float(stime[s]), 4),
                end_session_time=round(float(stime[e - 1]), 4),
                start_frame=int(frame_ids[s]),
                end_frame=int(frame_ids[e - 1]),
                start_distance_m=round(float(dist[0]), 2),
                end_distance_m=round(float(dist[-1]), 2),
                tyre_actual=tyre_actual,
                tyre_visual=tyre_visual,
                tyre_age_laps=tyre_age,
                fuel_start=_round(fuel_start),
                fuel_end=_round(fuel_end),
                tyre_wear_start=wear_start,
                tyre_wear_end=wear_end,
                assists=assists,
                setup=setup,
                issues=issues,
                notes=notes,
            )
        )
        spans.append((s, e))
    return laps, spans


def _frames_table(
    base: CanonicalStream,
    lap_index: np.ndarray,
    paused_before: np.ndarray,
    streams: dict[int, CanonicalStream],
) -> pa.Table:
    """One row per canonical Lap Data frame, player car, all high-rate channels joined."""
    hdr = base.header
    cols: dict[str, pa.Array] = {
        "segment": pa.array(base.timeline.segment),
        "lap_index": pa.array(lap_index),
        "frame_identifier": pa.array(hdr["frame_identifier"]),
        "overall_frame_identifier": pa.array(hdr["overall_frame_identifier"]),
        "session_time": pa.array(hdr["session_time"]),
        "wall_ns": pa.array(base.wall_ns),
        "after_flashback": pa.array(base.timeline.after_rewind),
        "paused_before": pa.array(paused_before),
    }

    def add(name: str, arr: pa.Array) -> None:
        if name in cols:
            raise ValueError(f"duplicate frame column {name!r}")
        cols[name] = arr

    for name, values in flatten(base.rows).items():
        add(name, pa.array(values))
    base_overall = base.overall
    n = len(base_overall)
    for pid in (*JOIN_EXACT, *JOIN_ASOF):
        st = streams.get(pid)
        if st is None:
            continue
        idx = (
            exact_index(base_overall, st.overall)
            if pid in JOIN_EXACT
            else asof_index(base_overall, st.overall)
        )
        missing = idx < 0
        take = np.where(missing, 0, idx)
        for name, values in flatten(st.rows).items():
            vals = values[take] if len(values) else np.zeros(n, dtype=values.dtype)
            add(name, pa.array(vals, mask=missing))
    return pa.table(cols)


def _session_meta(
    c: SessionCollection,
    session_recs: np.ndarray,
    laps: list[Lap],
    streams: dict[int, CanonicalStream],
) -> tuple[dict[str, Any], dict[str, Any]]:
    first = session_recs[0] if len(session_recs) else None
    last = session_recs[-1] if len(session_recs) else None
    track_id = session_type = game_mode = formula = None
    if len(session_recs):
        track_id = Counter(session_recs["track_id"].tolist()).most_common(1)[0][0]
        session_type = Counter(session_recs["session_type"].tolist()).most_common(1)[0][0]
        game_mode = Counter(session_recs["game_mode"].tolist()).most_common(1)[0][0]
        formula = Counter(session_recs["formula"].tolist()).most_common(1)[0][0]

    team_id = None
    participant: dict[str, Any] | None = None
    if c.participants is not None and c.player_car_index < f125.MAX_CARS:
        p = c.participants["car"][c.player_car_index]
        team_id = int(p["team_id"])
        participant = {k: v for k, v in to_python(p).items() if k != "name"}

    usable = [lap for lap in laps if lap.usable and lap.lap_time_ms]
    valid = [lap for lap in laps if lap.complete and lap.valid and lap.lap_time_ms]
    summary: dict[str, Any] = {
        "session_uid": f"{c.session_uid:016x}",
        "packet_format": c.packet_format,
        "game_version": c.game_version,
        "track_id": track_id,
        "track_name": enums.name(enums.TRACKS, track_id, "track") if track_id is not None else None,
        "track_length_m": int(last["track_length"]) if last is not None else None,
        "session_type": session_type,
        "session_type_name": (
            enums.name(enums.SESSION_TYPES, session_type, "session type")
            if session_type is not None
            else None
        ),
        "game_mode": game_mode,
        "game_mode_name": (
            enums.name(enums.GAME_MODES, game_mode, "game mode") if game_mode is not None else None
        ),
        "formula": formula,
        "weather": int(first["weather"]) if first is not None else None,
        "track_temp_c": int(first["track_temperature"]) if first is not None else None,
        "air_temp_c": int(first["air_temperature"]) if first is not None else None,
        "sector2_start_m": _round(last["sector2_lap_distance_start"], 2)
        if last is not None
        else None,
        "sector3_start_m": _round(last["sector3_lap_distance_start"], 2)
        if last is not None
        else None,
        "player_car_index": c.player_car_index,
        "team_id": team_id,
        "first_utc": _iso(c.first_wall_ns),
        "last_utc": _iso(c.last_wall_ns),
        "packets": c.packets,
        "lap_count": len(laps),
        "complete_laps": sum(lap.complete for lap in laps),
        "valid_laps": len(valid),
        "usable_laps": len(usable),
        "best_usable_lap_ms": min(
            (lap.lap_time_ms for lap in usable if lap.lap_time_ms is not None), default=None
        ),
        "best_valid_lap_ms": min(
            (lap.lap_time_ms for lap in valid if lap.lap_time_ms is not None), default=None
        ),
        "flashbacks": sum(
            s.timeline.flashbacks_applied
            for s in streams.values()
            if s.data.packet_id == PacketId.LAP_DATA
        ),
        "timeline_resets": sum(
            s.timeline.resets for s in streams.values() if s.data.packet_id == PacketId.LAP_DATA
        ),
        "assists": {k: int(last[k]) for k in SESSION_ASSIST_FIELDS} if last is not None else {},
        "settings": {k: int(last[k]) for k in SETTINGS_FIELDS} if last is not None else {},
    }
    stream_info = {
        packet_name(pid): {
            "received": len(s.data),
            "kept": len(s.timeline.keep),
            "cadence_frames": s.cadence,
            "flashbacks_applied": s.timeline.flashbacks_applied,
            "resets": s.timeline.resets,
            "duplicates": s.timeline.duplicates,
            "dropped_by_flashback": s.timeline.dropped,
        }
        for pid, s in sorted(streams.items())
    }
    player_hist = c.history.get(c.player_car_index)
    details = {
        "summary": summary,
        "packet_counts": {packet_name(k): v for k, v in sorted(c.counts.items())},
        "streams": stream_info,
        "session_first": to_python(first) if first is not None else None,
        "session_last": to_python(last) if last is not None else None,
        "events": [
            {
                "code": e.code,
                "frame_identifier": e.frame_identifier,
                "overall_frame_identifier": e.overall_frame_identifier,
                "session_time": round(e.session_time, 4),
                "details": e.details,
            }
            for e in c.events
            if e.code != "BUTN"  # button presses: too chatty, still in the raw log
        ],
        "button_events": sum(1 for e in c.events if e.code == "BUTN"),
        "session_history_player": to_python(player_hist) if player_hist is not None else None,
        "time_trial": to_python(c.time_trial) if c.time_trial is not None else None,
        "participant_player": participant,
        "setups": [
            {
                "overall_frame_identifier": s.overall_frame_identifier,
                "session_time": round(s.session_time, 4),
                "setup": _setup_dict(s.setup),
            }
            for s in c.setups
        ],
        "tyre_sets_player": (
            to_python(c.tyre_sets[c.player_car_index])
            if c.player_car_index in c.tyre_sets
            else None
        ),
        "final_classification": (
            to_python(c.final_classification) if c.final_classification is not None else None
        ),
        "laps": [asdict(lap) for lap in laps],
    }
    return summary, details
