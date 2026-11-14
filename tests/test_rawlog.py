import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pitwall.capture.rawlog import (
    RAW_SUFFIX,
    ZST_SUFFIX,
    CaptureReadSummary,
    RawLogError,
    RawLogReader,
    RotatingRawLogWriter,
    SegmentWriter,
    compress_segment,
    find_segments,
    iter_capture,
    segment_name,
)


def _write_segment(path: Path, payloads: list[bytes]) -> None:
    w = SegmentWriter(path, {"capture_id": "t"})
    for i, p in enumerate(payloads):
        w.write(1_700_000_000_000_000_000 + i, 10_000 + i, p)
    w.close()


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(payloads=st.lists(st.binary(min_size=0, max_size=2048), max_size=50))
def test_roundtrip_raw_and_compressed(tmp_path: Path, payloads: list[bytes]):
    d = tmp_path / os.urandom(4).hex()
    d.mkdir()
    seg = d / segment_name(0)
    _write_segment(seg, payloads)
    reader = RawLogReader(seg)
    assert [r.payload for r in reader] == payloads
    assert reader.metadata["capture_id"] == "t"
    assert not reader.truncated

    zst = compress_segment(seg)
    assert zst.name.endswith(ZST_SUFFIX)
    assert not seg.exists()
    reader = RawLogReader(zst)
    recs = list(reader)
    assert [r.payload for r in recs] == payloads
    assert [r.mono_ns for r in recs] == [10_000 + i for i in range(len(payloads))]


def test_never_overwrites_existing_segment(tmp_path: Path):
    seg = tmp_path / segment_name(0)
    _write_segment(seg, [b"a"])
    with pytest.raises(FileExistsError):
        SegmentWriter(seg, {})


def test_truncated_tail_is_tolerated(tmp_path: Path):
    seg = tmp_path / segment_name(0)
    _write_segment(seg, [b"x" * 100, b"y" * 100, b"z" * 100])
    size = seg.stat().st_size
    with open(seg, "r+b") as fh:
        fh.truncate(size - 50)  # cut inside the last record, as a crash would
    reader = RawLogReader(seg)
    assert [r.payload for r in reader] == [b"x" * 100, b"y" * 100]
    assert reader.truncated


def test_bad_magic_rejected(tmp_path: Path):
    bad = tmp_path / segment_name(0)
    bad.write_bytes(b"NOTPWRAW" + bytes(20))
    with pytest.raises(RawLogError):
        list(RawLogReader(bad))


def test_rotation_by_size_and_ordered_reading(tmp_path: Path):
    closed: list[Path] = []
    w = RotatingRawLogWriter(
        tmp_path,
        {"capture_id": "rot"},
        rotate_seconds=3600,
        rotate_bytes=2000,
        on_segment_closed=closed.append,
    )
    payloads = [bytes([i % 256]) * 300 for i in range(20)]
    for i, p in enumerate(payloads):
        w.write(i, i, p)
    w.close()
    assert len(w.segments) > 1
    assert closed == w.segments
    summary = CaptureReadSummary()
    assert [r.payload for r in iter_capture(tmp_path, summary)] == payloads
    assert summary.segments == len(w.segments)
    assert summary.records == len(payloads)


def test_rotation_by_time(tmp_path: Path):
    w = RotatingRawLogWriter(tmp_path, {}, rotate_seconds=1.0)
    w.write(0, 0, b"a")
    first = w.current_segment
    assert first is not None
    w.write(0, first.opened_mono_ns + 2_000_000_000, b"b")
    w.close()
    assert len(w.segments) == 2


def test_find_segments_prefers_verified_zst(tmp_path: Path):
    seg = tmp_path / segment_name(0)
    _write_segment(seg, [b"one"])
    raw_copy = seg.read_bytes()
    compress_segment(seg)
    seg.write_bytes(raw_copy)  # simulate crash after verify but before delete
    found = find_segments(tmp_path)
    assert len(found) == 1 and found[0].name.endswith(ZST_SUFFIX)
    seg2 = tmp_path / segment_name(1)
    _write_segment(seg2, [b"two"])
    names = [p.name for p in find_segments(tmp_path)]
    assert names == [segment_name(0) + ".zst", segment_name(1)]
    assert names[1].endswith(RAW_SUFFIX)
