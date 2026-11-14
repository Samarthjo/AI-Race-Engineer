"""Common header carried by every F1 25 UDP packet.

Layout (little-endian, packed), as used by the F1 23 / F1 24 / F1 25 UDP formats:

    uint16  packetFormat             e.g. 2025
    uint8   gameYear                 last two digits, e.g. 25
    uint8   gameMajorVersion
    uint8   gameMinorVersion
    uint8   packetVersion            version of this packet type
    uint8   packetId                 see PacketId
    uint64  sessionUID               unique per session
    float   sessionTime              seconds since session start
    uint32  frameIdentifier          frame the data was retrieved on (rewinds on flashback)
    uint32  overallFrameIdentifier   frame counter that does not rewind on flashback
    uint8   playerCarIndex
    uint8   secondaryPlayerCarIndex  255 if no second player

Total: 29 bytes. Must be re-verified against the official EA spec in Stage 1
(see docs/spec/README.md), including for the 2026 Season Pack UDP mode.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

_HEADER_STRUCT = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = _HEADER_STRUCT.size  # 29


class PacketTooShortError(ValueError):
    """Raised when a datagram is too short to contain a packet header."""


class PacketId(IntEnum):
    """Packet type identifiers in the F1 25 UDP format."""

    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    FINAL_CLASSIFICATION = 8
    LOBBY_INFO = 9
    CAR_DAMAGE = 10
    SESSION_HISTORY = 11
    TYRE_SETS = 12
    MOTION_EX = 13
    TIME_TRIAL = 14
    LAP_POSITIONS = 15


def packet_name(packet_id: int) -> str:
    """Human-readable name for a packet id; unknown ids are reported, never guessed."""
    try:
        return PacketId(packet_id).name
    except ValueError:
        return f"UNKNOWN_{packet_id}"


@dataclass(frozen=True, slots=True)
class PacketHeader:
    packet_format: int
    game_year: int
    game_major_version: int
    game_minor_version: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int
    secondary_player_car_index: int

    @classmethod
    def unpack(cls, data: bytes | bytearray | memoryview) -> PacketHeader:
        if len(data) < HEADER_SIZE:
            raise PacketTooShortError(f"datagram has {len(data)} bytes, header needs {HEADER_SIZE}")
        return cls(*_HEADER_STRUCT.unpack_from(data, 0))

    def pack(self) -> bytes:
        return _HEADER_STRUCT.pack(
            self.packet_format,
            self.game_year,
            self.game_major_version,
            self.game_minor_version,
            self.packet_version,
            self.packet_id,
            self.session_uid,
            self.session_time,
            self.frame_identifier,
            self.overall_frame_identifier,
            self.player_car_index,
            self.secondary_player_car_index,
        )

    @property
    def name(self) -> str:
        return packet_name(self.packet_id)

    @property
    def game_version(self) -> str:
        return f"{self.game_major_version}.{self.game_minor_version:02d}"
