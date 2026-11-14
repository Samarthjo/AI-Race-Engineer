"""Raw packet log: data layer L0.

Every UDP datagram is stored byte-for-byte with two receive timestamps, so all later
layers (decoding, laps, analysis) can be rebuilt without re-driving a lap.

Segment file layout (``.pwraw``)::

    8 bytes    magic  b"PWRAW01\\n"
    uint32     metadata length N (little-endian)
    N bytes    metadata JSON (UTF-8)
    records:   int64 wall_ns | uint64 mono_ns | uint16 payload_len | payload

``wall_ns`` is ``time.time_ns()`` (for humans), ``mono_ns`` is ``time.monotonic_ns()``
(for intervals; unaffected by clock changes). Closed segments may be compressed
losslessly to ``.pwraw.zst``; compression is verified by hash before the original
is removed. A segment cut short by a crash stays readable up to its last full record.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import zstandard

MAGIC = b"PWRAW01\n"
_FILE_HEADER = struct.Struct("<8sI")
_RECORD = struct.Struct("<qQH")
RECORD_HEADER_SIZE = _RECORD.size  # 18
MAX_PAYLOAD = 0xFFFF
RAW_SUFFIX = ".pwraw"
ZST_SUFFIX = ".pwraw.zst"
SEGMENT_PREFIX = "seg_"
_READ_CHUNK = 1 << 20
_MAX_METADATA = 1 << 20


class RawLogError(Exception):
    """Raised for unreadable or invalid raw log files."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    wall_ns: int
    mono_ns: int
    payload: bytes


def segment_name(index: int) -> str:
    return f"{SEGMENT_PREFIX}{index:05d}{RAW_SUFFIX}"


# --------------------------------------------------------------------------- writing


class SegmentWriter:
    """Writes one segment file. Never overwrites an existing file."""

    def __init__(self, path: Path, metadata: dict[str, Any], flush_interval_s: float = 0.5):
        meta = json.dumps(metadata, sort_keys=True).encode("utf-8")
        if len(meta) > _MAX_METADATA:
            raise RawLogError("metadata too large")
        self.path = path
        self._fh: BinaryIO = open(path, "xb", buffering=_READ_CHUNK)  # noqa: SIM115
        self._fh.write(_FILE_HEADER.pack(MAGIC, len(meta)))
        self._fh.write(meta)
        self._flush_interval_ns = int(flush_interval_s * 1e9)
        self._last_flush_ns = time.monotonic_ns()
        self.opened_mono_ns = self._last_flush_ns
        self.records = 0
        self.bytes = _FILE_HEADER.size + len(meta)
        self.closed = False

    def write(self, wall_ns: int, mono_ns: int, payload: bytes) -> None:
        if len(payload) > MAX_PAYLOAD:
            raise RawLogError(f"payload of {len(payload)} bytes exceeds {MAX_PAYLOAD}")
        self._fh.write(_RECORD.pack(wall_ns, mono_ns, len(payload)))
        self._fh.write(payload)
        self.records += 1
        self.bytes += RECORD_HEADER_SIZE + len(payload)
        self.maybe_flush(mono_ns)

    def maybe_flush(self, now_mono_ns: int) -> None:
        if now_mono_ns - self._last_flush_ns >= self._flush_interval_ns:
            self._fh.flush()
            self._last_flush_ns = now_mono_ns

    def close(self) -> None:
        if self.closed:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self.closed = True


class RotatingRawLogWriter:
    """Writes a capture as numbered segments in one directory, rotating by age or size."""

    def __init__(
        self,
        directory: Path,
        capture_metadata: dict[str, Any],
        rotate_seconds: float = 300.0,
        rotate_bytes: int = 256 * 1024 * 1024,
        flush_interval_s: float = 0.5,
        on_segment_closed: Callable[[Path], None] | None = None,
    ):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._metadata = dict(capture_metadata)
        self._rotate_ns = int(rotate_seconds * 1e9)
        self._rotate_bytes = rotate_bytes
        self._flush_interval_s = flush_interval_s
        self._on_closed = on_segment_closed
        self._index = -1
        self._segment: SegmentWriter | None = None
        self.segments: list[Path] = []
        self.total_records = 0
        self.total_payload_bytes = 0

    @property
    def current_segment(self) -> SegmentWriter | None:
        return self._segment

    def _open_next(self) -> SegmentWriter:
        self._index += 1
        meta = dict(self._metadata)
        meta.update(
            format="pwraw",
            format_version=1,
            segment_index=self._index,
            segment_created_utc=_utc_now_iso(),
        )
        path = self.directory / segment_name(self._index)
        seg = SegmentWriter(path, meta, self._flush_interval_s)
        self.segments.append(path)
        return seg

    def _close_current(self) -> None:
        if self._segment is None:
            return
        self._segment.close()
        closed_path = self._segment.path
        self._segment = None
        if self._on_closed is not None:
            self._on_closed(closed_path)

    def write(self, wall_ns: int, mono_ns: int, payload: bytes) -> None:
        seg = self._segment
        if seg is not None and (
            mono_ns - seg.opened_mono_ns >= self._rotate_ns or seg.bytes >= self._rotate_bytes
        ):
            self._close_current()
            seg = None
        if seg is None:
            seg = self._segment = self._open_next()
        seg.write(wall_ns, mono_ns, payload)
        self.total_records += 1
        self.total_payload_bytes += len(payload)

    def tick(self, now_mono_ns: int) -> None:
        """Call periodically (also when idle) so buffered data reaches the OS."""
        if self._segment is not None:
            self._segment.maybe_flush(now_mono_ns)

    def close(self) -> None:
        self._close_current()


# ------------------------------------------------------------------------ compression


def compress_segment(path: Path, level: int = 6) -> Path:
    """Losslessly compress ``seg.pwraw`` to ``seg.pwraw.zst``, verify, then remove the original.

    The ``.zst`` file only appears (atomically) after its decompressed hash matches.
    """
    if not path.name.endswith(RAW_SUFFIX):
        raise RawLogError(f"not a raw segment: {path}")
    final = path.with_name(path.name + ".zst")
    tmp = path.with_name(path.name + ".zst.tmp")
    src_hash = hashlib.sha256()
    cctx = zstandard.ZstdCompressor(level=level)
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        with cctx.stream_writer(dst, closefd=False) as writer:
            while chunk := src.read(_READ_CHUNK):
                src_hash.update(chunk)
                writer.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    check = hashlib.sha256()
    with open(tmp, "rb") as fh:
        reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        while chunk := reader.read(_READ_CHUNK):
            check.update(chunk)
    if check.digest() != src_hash.digest():
        tmp.unlink(missing_ok=True)
        raise RawLogError(f"compression verification failed for {path}")
    os.replace(tmp, final)
    path.unlink()
    return final


@dataclass
class CompressionResult:
    source: Path
    output: Path | None
    error: str | None


class BackgroundCompressor:
    """Compresses closed segments on a worker thread so capture never blocks."""

    def __init__(self, level: int = 6):
        self._level = level
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self.results: list[CompressionResult] = []
        self._thread = threading.Thread(target=self._run, name="pwraw-compress", daemon=True)
        self._thread.start()

    def submit(self, path: Path) -> None:
        self._queue.put(path)

    def _run(self) -> None:
        while True:
            path = self._queue.get()
            if path is None:
                return
            try:
                out = compress_segment(path, self._level)
                self.results.append(CompressionResult(path, out, None))
            except Exception as exc:  # keep the raw file; report, never crash capture
                self.results.append(CompressionResult(path, None, repr(exc)))

    def close(self, timeout: float | None = None) -> None:
        self._queue.put(None)
        self._thread.join(timeout)


# ---------------------------------------------------------------------------- reading


class _ChunkedSource:
    def __init__(self, read: Callable[[int], bytes]):
        self._read = read
        self._buf = bytearray()
        self._pos = 0
        self.eof = False

    def available(self) -> int:
        return len(self._buf) - self._pos

    def fill(self, need: int) -> bool:
        """Ensure ``need`` bytes are buffered. Returns False at EOF if not enough."""
        while self.available() < need and not self.eof:
            if self._pos:
                del self._buf[: self._pos]
                self._pos = 0
            chunk = self._read(_READ_CHUNK)
            if not chunk:
                self.eof = True
                break
            self._buf.extend(chunk)
        return self.available() >= need

    def take(self, n: int) -> bytes:
        out = bytes(self._buf[self._pos : self._pos + n])
        self._pos += n
        return out


@dataclass
class RawLogReader:
    """Iterates records of one segment (``.pwraw`` or ``.pwraw.zst``).

    After iteration, ``truncated`` is True if the file ended inside a record (crash),
    and ``error`` holds any decompression error. Records before that point are valid.
    """

    path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    error: str | None = None
    records_read: int = 0

    def __iter__(self) -> Iterator[RawRecord]:
        with open(self.path, "rb") as fh:
            if self.path.name.endswith(".zst"):
                reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
                read: Callable[[int], bytes] = reader.read
            else:
                read = fh.read
            src = _ChunkedSource(read)
            try:
                yield from self._parse(src)
            except zstandard.ZstdError as exc:
                self.error = f"decompression error: {exc}"
                self.truncated = True

    def _parse(self, src: _ChunkedSource) -> Iterator[RawRecord]:
        if not src.fill(_FILE_HEADER.size):
            raise RawLogError(f"{self.path}: file too short for header")
        magic, meta_len = _FILE_HEADER.unpack(src.take(_FILE_HEADER.size))
        if magic != MAGIC:
            raise RawLogError(f"{self.path}: bad magic {magic!r}")
        if meta_len > _MAX_METADATA or not src.fill(meta_len):
            raise RawLogError(f"{self.path}: metadata missing or corrupt")
        self.metadata = json.loads(src.take(meta_len).decode("utf-8"))
        while True:
            if not src.fill(RECORD_HEADER_SIZE):
                self.truncated = src.available() > 0
                return
            wall_ns, mono_ns, length = _RECORD.unpack(src.take(RECORD_HEADER_SIZE))
            if not src.fill(length):
                self.truncated = True
                return
            self.records_read += 1
            yield RawRecord(wall_ns, mono_ns, src.take(length))


def read_metadata(path: Path) -> dict[str, Any]:
    reader = RawLogReader(path)
    for _ in reader:
        break
    return reader.metadata


def find_segments(path: Path) -> list[Path]:
    """Segment files for a capture directory (or a single segment file), in order.

    If a segment exists both raw and compressed (crash between verify and delete),
    the verified ``.zst`` copy is used.
    """
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise RawLogError(f"no such capture: {path}")
    by_stem: dict[str, Path] = {}
    for p in sorted(path.iterdir()):
        if not p.name.startswith(SEGMENT_PREFIX):
            continue
        if p.name.endswith(ZST_SUFFIX):
            by_stem[p.name[: -len(ZST_SUFFIX)]] = p
        elif p.name.endswith(RAW_SUFFIX):
            by_stem.setdefault(p.name[: -len(RAW_SUFFIX)], p)
    return [by_stem[k] for k in sorted(by_stem)]


@dataclass
class CaptureReadSummary:
    segments: int = 0
    records: int = 0
    truncated_segments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def iter_capture(path: Path, summary: CaptureReadSummary | None = None) -> Iterator[RawRecord]:
    """All records of a capture (directory or single segment), in receive order."""
    for seg in find_segments(path):
        reader = RawLogReader(seg)
        yield from reader
        if summary is not None:
            summary.segments += 1
            summary.records += reader.records_read
            if reader.truncated:
                summary.truncated_segments.append(seg.name)
            if reader.error:
                summary.errors.append(f"{seg.name}: {reader.error}")


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
