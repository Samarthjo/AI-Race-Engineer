"""Decode raw F1 UDP datagrams into numpy records (single or in batches).

Strict by design: a datagram is only decoded if its ``packetFormat`` is supported and its
length matches the spec size for its packet id exactly. Anything else raises a
:class:`DecodeError` subclass, which callers count and report, never guess around.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any

import numpy as np

from pitwall.f1udp import f125
from pitwall.f1udp.header import PacketHeader, PacketTooShortError, packet_name

# packetFormat -> {packet_id: dtype}
SUPPORTED_FORMATS: dict[int, dict[int, np.dtype]] = {f125.PACKET_FORMAT: f125.PACKET_DTYPES}


class DecodeError(ValueError):
    """A datagram could not be decoded."""


class UnsupportedFormatError(DecodeError):
    pass


class UnknownPacketError(DecodeError):
    pass


class SizeMismatchError(DecodeError):
    pass


def packet_dtype(packet_format: int, packet_id: int) -> np.dtype:
    dtypes = SUPPORTED_FORMATS.get(packet_format)
    if dtypes is None:
        raise UnsupportedFormatError(
            f"packetFormat {packet_format} not supported (supported: {sorted(SUPPORTED_FORMATS)})"
        )
    dt = dtypes.get(packet_id)
    if dt is None:
        raise UnknownPacketError(f"unknown packet id {packet_id} for packetFormat {packet_format}")
    return dt


def check_packet(payload: bytes) -> tuple[PacketHeader, np.dtype]:
    """Validate header, format and size. Returns the header and the dtype to decode with."""
    try:
        hdr = PacketHeader.unpack(payload)
    except PacketTooShortError as exc:
        raise DecodeError(str(exc)) from exc
    dt = packet_dtype(hdr.packet_format, hdr.packet_id)
    if len(payload) != dt.itemsize:
        raise SizeMismatchError(
            f"{packet_name(hdr.packet_id)}: {len(payload)} bytes, expected {dt.itemsize}"
        )
    return hdr, dt


def decode_packet(payload: bytes) -> np.void:
    """Decode one datagram into a structured record (a copy, safe to keep)."""
    _, dt = check_packet(payload)
    return np.frombuffer(payload, dtype=dt, count=1).copy()[0]


def decode_batch(payloads: Sequence[bytes], dtype: np.dtype) -> np.ndarray:
    """Decode many same-type, already size-checked datagrams at once."""
    if not payloads:
        return np.empty(0, dtype=dtype)
    return np.frombuffer(b"".join(payloads), dtype=dtype).copy()


# ---------------------------------------------------------------------------- events

_NO_DATA: tuple[str, ...] = ()
# event code -> (struct format for the leading bytes of the 12-byte union, field names)
EVENT_LAYOUTS: dict[str, tuple[struct.Struct | None, tuple[str, ...]]] = {
    "SSTA": (None, _NO_DATA),  # session started
    "SEND": (None, _NO_DATA),  # session ended
    "FTLP": (struct.Struct("<Bf"), ("vehicle_idx", "lap_time")),
    "RTMT": (struct.Struct("<BB"), ("vehicle_idx", "reason")),
    "DRSE": (None, _NO_DATA),
    "DRSD": (struct.Struct("<B"), ("reason",)),
    "TMPT": (struct.Struct("<B"), ("vehicle_idx",)),
    "CHQF": (None, _NO_DATA),
    "RCWN": (struct.Struct("<B"), ("vehicle_idx",)),
    "PENA": (
        struct.Struct("<BBBBBBB"),
        (
            "penalty_type",
            "infringement_type",
            "vehicle_idx",
            "other_vehicle_idx",
            "time",
            "lap_num",
            "places_gained",
        ),
    ),
    "SPTP": (
        struct.Struct("<BfBBBf"),
        (
            "vehicle_idx",
            "speed",
            "is_overall_fastest_in_session",
            "is_driver_fastest_in_session",
            "fastest_vehicle_idx_in_session",
            "fastest_speed_in_session",
        ),
    ),
    "STLG": (struct.Struct("<B"), ("num_lights",)),
    "LGOT": (None, _NO_DATA),
    "DTSV": (struct.Struct("<B"), ("vehicle_idx",)),
    "SGSV": (struct.Struct("<Bf"), ("vehicle_idx", "stop_time")),
    "FLBK": (struct.Struct("<If"), ("flashback_frame_identifier", "flashback_session_time")),
    "BUTN": (struct.Struct("<I"), ("button_status",)),
    "RDFL": (None, _NO_DATA),
    "OVTK": (struct.Struct("<BB"), ("overtaking_vehicle_idx", "being_overtaken_vehicle_idx")),
    "SCAR": (struct.Struct("<BB"), ("safety_car_type", "event_type")),
    "COLL": (struct.Struct("<BB"), ("vehicle1_idx", "vehicle2_idx")),
}


def decode_event_details(code: str, details: bytes) -> dict[str, Any]:
    """Decode the 12-byte event union for ``code``. Unknown codes keep the raw bytes."""
    layout = EVENT_LAYOUTS.get(code)
    if layout is None:
        return {"raw_hex": details.hex()}
    st, names = layout
    if st is None:
        return {}
    values = st.unpack_from(details, 0)
    return {
        n: (round(v, 6) if isinstance(v, float) else v) for n, v in zip(names, values, strict=True)
    }


def event_code(record: np.void) -> str:
    return bytes(record["event_string_code"]).decode("ascii", errors="replace")


# ------------------------------------------------------------------- JSON conversion


def to_python(value: Any) -> Any:
    """Convert numpy records/arrays/scalars to plain JSON-friendly Python values."""
    if isinstance(value, np.void):
        if value.dtype.names is None:  # opaque bytes (e.g. event union)
            return bytes(value).hex()
        return {name: to_python(value[name]) for name in value.dtype.names}
    if isinstance(value, np.ndarray):
        if value.dtype.names is not None:
            return [to_python(v) for v in value]
        if value.dtype.kind == "S":
            return [to_python(v) for v in value.tolist()]
        return value.tolist()
    if isinstance(value, bytes | np.bytes_):
        return bytes(value).split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    if isinstance(value, np.generic):
        item = value.item()
        if isinstance(item, float):
            return round(item, 6)
        return item
    return value
