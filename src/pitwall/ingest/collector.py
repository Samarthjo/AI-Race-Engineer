"""Split a raw capture into game sessions and decode what later layers need.

* High-rate player-car streams (lap data, telemetry, motion, status, motion-ex, damage) are
  decoded in batches and reduced to the player's car (``playerCarIndex`` from each header).
* Low-rate packets keep their latest value (participants, session history, time trial…) or a
  compact history (session packets, setup changes, events).
* Other cars' data is not extracted here; it stays available in the raw log (L0).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pitwall.capture.rawlog import RawRecord
from pitwall.f1udp import f125
from pitwall.f1udp.decoder import (
    DecodeError,
    SizeMismatchError,
    UnknownPacketError,
    UnsupportedFormatError,
    check_packet,
    decode_batch,
    decode_event_details,
    event_code,
)
from pitwall.f1udp.header import PacketHeader, PacketId

PLAYER_STREAMS: tuple[int, ...] = (
    PacketId.LAP_DATA,
    PacketId.CAR_TELEMETRY,
    PacketId.MOTION,
    PacketId.CAR_STATUS,
    PacketId.MOTION_EX,
    PacketId.CAR_DAMAGE,
)
_CHUNK = 2048


@dataclass
class StreamData:
    """Player-car rows of one packet type, in receive order."""

    packet_id: int
    header: np.ndarray  # f125.HEADER records
    rows: np.ndarray  # player-car records (e.g. f125.CAR_TELEMETRY)
    wall_ns: np.ndarray  # int64 receive time

    def __len__(self) -> int:
        return len(self.rows)


class PlayerStream:
    """Accumulates one high-rate packet type and extracts the player's car in batches."""

    def __init__(self, packet_id: int, dtype: np.dtype):
        self.packet_id = packet_id
        self.dtype = dtype
        self._pending: list[bytes] = []
        self._pending_wall: list[int] = []
        self._headers: list[np.ndarray] = []
        self._rows: list[np.ndarray] = []
        self._walls: list[np.ndarray] = []
        self.skipped_no_player = 0

    def add(self, payload: bytes, wall_ns: int) -> None:
        self._pending.append(payload)
        self._pending_wall.append(wall_ns)
        if len(self._pending) >= _CHUNK:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        arr = decode_batch(self._pending, self.dtype)
        wall = np.asarray(self._pending_wall, dtype=np.int64)
        self._pending = []
        self._pending_wall = []
        hdr = arr["header"]
        names = arr.dtype.names or ()
        if "car" in names:
            idx = hdr["player_car_index"].astype(np.intp)
            ok = idx < f125.MAX_CARS  # 255 = no player car (e.g. spectating)
            self.skipped_no_player += int((~ok).sum())
            arr, hdr, idx, wall = arr[ok], hdr[ok], idx[ok], wall[ok]
            rows = arr["car"][np.arange(len(arr)), idx]
        else:
            rows = arr["player"]
        self._headers.append(hdr.copy())
        self._rows.append(rows.copy())
        self._walls.append(wall)

    def finish(self) -> StreamData:
        self._flush()
        car_dtype = (
            self.dtype["car"].base if "car" in (self.dtype.names or ()) else self.dtype["player"]
        )
        if not self._rows:
            return StreamData(
                self.packet_id,
                np.empty(0, f125.HEADER),
                np.empty(0, car_dtype),
                np.empty(0, np.int64),
            )
        return StreamData(
            self.packet_id,
            np.concatenate(self._headers),
            np.concatenate(self._rows),
            np.concatenate(self._walls),
        )


@dataclass
class EventRecord:
    code: str
    frame_identifier: int
    overall_frame_identifier: int
    session_time: float
    wall_ns: int
    details: dict[str, Any]


@dataclass
class SetupChange:
    overall_frame_identifier: int
    session_time: float
    setup: np.void  # f125.CAR_SETUP


@dataclass
class SessionCollection:
    session_uid: int
    packet_format: int
    game_version: str
    first_wall_ns: int
    last_wall_ns: int = 0
    packets: int = 0
    player_car_index: int = 255
    counts: Counter[int] = field(default_factory=Counter)
    streams: dict[int, PlayerStream] = field(default_factory=dict)
    session_payloads: list[bytes] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)
    history: dict[int, np.void] = field(default_factory=dict)  # latest per car_idx
    setups: list[SetupChange] = field(default_factory=list)
    tyre_sets: dict[int, np.void] = field(default_factory=dict)
    participants: np.void | None = None
    time_trial: np.void | None = None
    final_classification: np.void | None = None
    lap_positions: np.void | None = None

    def observe(self, hdr: PacketHeader, dtype: np.dtype, rec: RawRecord) -> None:
        self.packets += 1
        self.last_wall_ns = rec.wall_ns
        self.counts[hdr.packet_id] += 1
        if hdr.player_car_index < f125.MAX_CARS:
            self.player_car_index = hdr.player_car_index
        pid = hdr.packet_id
        if pid in PLAYER_STREAMS:
            stream = self.streams.get(pid)
            if stream is None:
                stream = self.streams[pid] = PlayerStream(pid, dtype)
            stream.add(rec.payload, rec.wall_ns)
        elif pid == PacketId.SESSION:
            self.session_payloads.append(rec.payload)
        else:
            self._observe_low_rate(hdr, dtype, rec)

    def _observe_low_rate(self, hdr: PacketHeader, dtype: np.dtype, rec: RawRecord) -> None:
        pkt = np.frombuffer(rec.payload, dtype=dtype, count=1).copy()[0]
        pid = hdr.packet_id
        if pid == PacketId.EVENT:
            code = event_code(pkt)
            self.events.append(
                EventRecord(
                    code=code,
                    frame_identifier=hdr.frame_identifier,
                    overall_frame_identifier=hdr.overall_frame_identifier,
                    session_time=float(hdr.session_time),
                    wall_ns=rec.wall_ns,
                    details=decode_event_details(code, bytes(pkt["event_details"])),
                )
            )
        elif pid == PacketId.SESSION_HISTORY:
            self.history[int(pkt["car_idx"])] = pkt
        elif pid == PacketId.CAR_SETUPS:
            if hdr.player_car_index < f125.MAX_CARS:
                setup = pkt["car"][hdr.player_car_index]
                if not self.setups or bytes(self.setups[-1].setup.tobytes()) != setup.tobytes():
                    self.setups.append(
                        SetupChange(hdr.overall_frame_identifier, float(hdr.session_time), setup)
                    )
        elif pid == PacketId.TYRE_SETS:
            self.tyre_sets[int(pkt["car_idx"])] = pkt
        elif pid == PacketId.PARTICIPANTS:
            self.participants = pkt
        elif pid == PacketId.TIME_TRIAL:
            self.time_trial = pkt
        elif pid == PacketId.FINAL_CLASSIFICATION:
            self.final_classification = pkt
        elif pid == PacketId.LAP_POSITIONS:
            self.lap_positions = pkt
        # LOBBY_INFO: multiplayer lobby only; kept in the raw log, not needed here.

    def finish_streams(self) -> dict[int, StreamData]:
        return {pid: s.finish() for pid, s in self.streams.items()}

    def session_records(self) -> np.ndarray:
        return decode_batch(self.session_payloads, f125.PACKET_SESSION)


@dataclass
class CollectResult:
    datagrams: int = 0
    decoded: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    error_examples: list[str] = field(default_factory=list)
    sessions: dict[int, SessionCollection] = field(default_factory=dict)

    def _error(self, kind: str, exc: Exception) -> None:
        self.errors[kind] += 1
        if len(self.error_examples) < 10:
            self.error_examples.append(f"{kind}: {exc}")


def collect(records: Iterable[RawRecord]) -> CollectResult:
    result = CollectResult()
    for rec in records:
        result.datagrams += 1
        try:
            hdr, dtype = check_packet(rec.payload)
        except UnsupportedFormatError as exc:
            result._error("unsupported_format", exc)
            continue
        except UnknownPacketError as exc:
            result._error("unknown_packet_id", exc)
            continue
        except SizeMismatchError as exc:
            result._error("size_mismatch", exc)
            continue
        except DecodeError as exc:
            result._error("too_short", exc)
            continue
        session = result.sessions.get(hdr.session_uid)
        if session is None:
            session = result.sessions[hdr.session_uid] = SessionCollection(
                session_uid=hdr.session_uid,
                packet_format=hdr.packet_format,
                game_version=hdr.game_version,
                first_wall_ns=rec.wall_ns,
            )
        session.observe(hdr, dtype, rec)
        result.decoded += 1
    return result
