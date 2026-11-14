"""Synthetic F1 25-style datagrams for tests.

Only the 29-byte header is realistic; bodies are zero padding. These are test fixtures,
never real telemetry. Real golden captures from the PS5 replace them in Stage 1.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from pitwall.f1udp.header import PacketHeader, PacketId

SYNTH_SESSION_UID = 0x5E57_0000_0000_0001
BODY_SIZE = 100


def make_packet(
    packet_id: int,
    frame: int,
    overall: int | None = None,
    session_uid: int = SYNTH_SESSION_UID,
    packet_format: int = 2025,
    session_time: float | None = None,
    body_size: int = BODY_SIZE,
) -> bytes:
    hdr = PacketHeader(
        packet_format=packet_format,
        game_year=25,
        game_major_version=1,
        game_minor_version=12,
        packet_version=1,
        packet_id=packet_id,
        session_uid=session_uid,
        session_time=frame / 60.0 if session_time is None else session_time,
        frame_identifier=frame,
        overall_frame_identifier=frame if overall is None else overall,
        player_car_index=0,
        secondary_player_car_index=255,
    )
    return hdr.pack() + bytes(body_size)


@dataclass
class Datagram:
    mono_ns: int
    payload: bytes


HIGH_RATE = (
    PacketId.MOTION,
    PacketId.LAP_DATA,
    PacketId.CAR_TELEMETRY,
    PacketId.CAR_STATUS,
    PacketId.MOTION_EX,
)


def stream(
    frames: range,
    hz: int = 60,
    ids: tuple[int, ...] = HIGH_RATE,
    drop: AbstractSet[tuple[int, int]] | None = None,
    start_ns: int = 1_000_000_000,
    session_uid: int = SYNTH_SESSION_UID,
) -> list[Datagram]:
    """One packet per id per frame at ``hz``; ``drop`` holds (frame, packet_id) to omit."""
    drop = drop or frozenset()
    out: list[Datagram] = []
    period = 1_000_000_000 // hz
    for i, frame in enumerate(frames):
        t = start_ns + i * period
        for pid in ids:
            if (frame, pid) in drop:
                continue
            out.append(Datagram(t, make_packet(pid, frame, session_uid=session_uid)))
    return out
