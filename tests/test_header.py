import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.f1udp.header import HEADER_SIZE, PacketHeader, PacketTooShortError, packet_name


def test_header_size_is_29_bytes():
    assert HEADER_SIZE == 29


@given(
    fmt=st.integers(0, 0xFFFF),
    pid=st.integers(0, 255),
    uid=st.integers(0, 2**64 - 1),
    frame=st.integers(0, 2**32 - 1),
    overall=st.integers(0, 2**32 - 1),
)
def test_header_roundtrip(fmt, pid, uid, frame, overall):
    hdr = PacketHeader(fmt, 25, 1, 7, 1, pid, uid, 12.5, frame, overall, 0, 255)
    data = hdr.pack()
    assert len(data) == HEADER_SIZE
    assert PacketHeader.unpack(data + b"\x00" * 10) == hdr


def test_short_datagram_rejected():
    with pytest.raises(PacketTooShortError):
        PacketHeader.unpack(b"\x00" * 28)


def test_known_layout_little_endian():
    # packetFormat 2025 = 0x07E9 little-endian, gameYear 25, packetId 6 (CAR_TELEMETRY)
    raw = bytes.fromhex("e907") + bytes([25, 1, 12, 1, 6]) + bytes(22)
    hdr = PacketHeader.unpack(raw)
    assert hdr.packet_format == 2025
    assert hdr.game_year == 25
    assert hdr.packet_id == 6
    assert hdr.name == "CAR_TELEMETRY"
    assert hdr.game_version == "1.12"


def test_unknown_packet_names_are_not_guessed():
    assert packet_name(13) == "MOTION_EX"
    assert packet_name(99) == "UNKNOWN_99"
