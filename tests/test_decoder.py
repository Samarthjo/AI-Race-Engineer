import contextlib
import struct

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.f1udp import f125
from pitwall.f1udp.decoder import (
    DecodeError,
    SizeMismatchError,
    UnknownPacketError,
    UnsupportedFormatError,
    decode_event_details,
    decode_packet,
    to_python,
)
from pitwall.f1udp.header import PacketHeader, PacketId

# Sizes published for packetFormat 2025 (and agreed by both cross-check implementations).
SPEC_SIZES = {
    "MOTION": 1349,
    "SESSION": 753,
    "LAP_DATA": 1285,
    "EVENT": 45,
    "PARTICIPANTS": 1284,
    "CAR_SETUPS": 1133,
    "CAR_TELEMETRY": 1352,
    "CAR_STATUS": 1239,
    "FINAL_CLASSIFICATION": 1042,
    "LOBBY_INFO": 954,
    "CAR_DAMAGE": 1041,
    "SESSION_HISTORY": 1460,
    "TYRE_SETS": 231,
    "MOTION_EX": 273,
    "TIME_TRIAL": 101,
    "LAP_POSITIONS": 1131,
}

PER_CAR_SIZES = {
    "CAR_MOTION": 60,
    "CAR_LAP_DATA": 57,
    "PARTICIPANT": 57,
    "CAR_SETUP": 50,
    "CAR_TELEMETRY": 60,
    "CAR_STATUS": 55,
    "FINAL_CLASSIFICATION": 46,
    "LOBBY_PLAYER": 42,
    "CAR_DAMAGE": 46,
    "LAP_HISTORY": 14,
    "TYRE_SET": 10,
    "TIME_TRIAL_SET": 24,
}


def test_all_16_packet_dtypes_match_spec_sizes():
    assert len(f125.PACKET_DTYPES) == 16
    for name, size in SPEC_SIZES.items():
        assert f125.PACKET_DTYPES[PacketId[name]].itemsize == size, name


def test_per_car_struct_sizes():
    for name, size in PER_CAR_SIZES.items():
        assert getattr(f125, name).itemsize == size, name


def _packet(pid: int, fmt: int = 2025) -> np.ndarray:
    arr = np.zeros(1, dtype=f125.PACKET_DTYPES[pid])
    h = arr["header"]
    h["packet_format"] = fmt
    h["game_year"] = 25
    h["packet_id"] = pid
    h["session_uid"] = 42
    h["frame_identifier"] = 7
    h["overall_frame_identifier"] = 9
    h["player_car_index"] = 2
    return arr


def test_decode_telemetry_player_values():
    arr = _packet(PacketId.CAR_TELEMETRY)
    car = arr["car"]
    car["speed"][0, 2] = 312
    car["throttle"][0, 2] = 0.75
    car["brake"][0, 2] = 0.25
    car["gear"][0, 2] = -1
    car["tyres_pressure"][0, 2] = [21.0, 21.5, 23.0, 23.5]
    arr["suggested_gear"] = 5
    rec = decode_packet(arr.tobytes())
    assert rec["header"]["player_car_index"] == 2
    p = rec["car"][2]
    assert int(p["speed"]) == 312
    assert float(p["throttle"]) == pytest.approx(0.75)
    assert float(p["brake"]) == pytest.approx(0.25)
    assert int(p["gear"]) == -1
    assert list(p["tyres_pressure"]) == pytest.approx([21.0, 21.5, 23.0, 23.5])
    assert int(rec["suggested_gear"]) == 5


def test_field_offsets_follow_spec_order():
    # Spot-check byte offsets inside the per-car lap data record (spec order, packed).
    dt = f125.CAR_LAP_DATA
    assert dt.fields is not None
    assert dt.fields["last_lap_time_in_ms"][1] == 0
    assert dt.fields["current_lap_time_in_ms"][1] == 4
    assert dt.fields["lap_distance"][1] == 20
    assert dt.fields["current_lap_num"][1] == 33
    assert dt.fields["speed_trap_fastest_lap"][1] == 56


def test_strict_validation():
    good = _packet(PacketId.CAR_TELEMETRY).tobytes()
    with pytest.raises(SizeMismatchError):
        decode_packet(good[:-1])
    with pytest.raises(UnsupportedFormatError):
        decode_packet(_packet(PacketId.CAR_TELEMETRY, fmt=2024).tobytes())
    unknown = bytearray(good)
    unknown[6] = 99  # packetId
    with pytest.raises(UnknownPacketError):
        decode_packet(bytes(unknown))
    with pytest.raises(DecodeError):
        decode_packet(b"\x00" * 10)


@settings(max_examples=200)
@given(st.binary(min_size=0, max_size=1500))
def test_random_bytes_never_crash(data: bytes):
    with contextlib.suppress(DecodeError):
        decode_packet(data)


def test_event_details():
    assert decode_event_details("FLBK", struct.pack("<If", 1234, 56.5).ljust(12, b"\0")) == {
        "flashback_frame_identifier": 1234,
        "flashback_session_time": 56.5,
    }
    sptp = decode_event_details("SPTP", struct.pack("<BfBBBf", 3, 341.5, 1, 1, 3, 341.5))
    assert sptp["vehicle_idx"] == 3 and sptp["speed"] == pytest.approx(341.5)
    assert decode_event_details("BUTN", struct.pack("<I", 0x40).ljust(12, b"\0")) == {
        "button_status": 0x40
    }
    assert decode_event_details("SSTA", bytes(12)) == {}
    assert "raw_hex" in decode_event_details("ZZZZ", bytes(12))


def test_event_packet_roundtrip():
    arr = _packet(PacketId.EVENT)
    arr["event_string_code"] = b"FLBK"
    arr["event_details"] = np.void(struct.pack("<If", 77, 1.5).ljust(12, b"\0"))
    rec = decode_packet(arr.tobytes())
    assert bytes(rec["event_string_code"]) == b"FLBK"


def test_to_python_strings_and_arrays():
    arr = _packet(PacketId.PARTICIPANTS)
    arr["car"]["name"][0, 2] = b"Player"
    data = to_python(decode_packet(arr.tobytes()))
    assert data["car"][2]["name"] == "Player"
    assert data["header"]["session_uid"] == 42
    assert len(data["car"][2]["livery_colours"]) == 4


def test_header_matches_numpy_layout():
    raw = _packet(PacketId.LAP_DATA).tobytes()
    hdr = PacketHeader.unpack(raw)
    assert (hdr.packet_format, hdr.frame_identifier, hdr.overall_frame_identifier) == (2025, 7, 9)
