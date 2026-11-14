"""One capture run: directory, manifest, writer, compressor, receiver, health report.

Layout::

    <data_dir>/raw/<capture_id>/
        capture.json        manifest (settings, label/note, final health report)
        seg_00000.pwraw.zst raw segments (L0)
        seg_00001.pwraw     (segment still open or not yet compressed)
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pitwall import __version__
from pitwall.capture.health import HealthTracker, IntervalStats
from pitwall.capture.rawlog import (
    BackgroundCompressor,
    CaptureReadSummary,
    RotatingRawLogWriter,
    iter_capture,
)
from pitwall.capture.receiver import CaptureCounters, UdpCapture, parse_endpoint
from pitwall.config import CaptureConfig

MANIFEST_NAME = "capture.json"


def new_capture_id(raw_dir: Path) -> str:
    base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    candidate, n = base, 1
    while (raw_dir / candidate).exists():
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    tmp = directory / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, directory / MANIFEST_NAME)


def read_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / MANIFEST_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class CaptureRun:
    """Runs a capture until ``stop_event`` is set, then finalises the manifest."""

    raw_dir: Path
    settings: CaptureConfig
    label: str | None = None
    note: str | None = None
    on_status: Callable[[IntervalStats, CaptureCounters], None] | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    capture_id: str = ""
    directory: Path = Path()
    health: HealthTracker | None = None
    capture: UdpCapture | None = None

    def prepare(self) -> None:
        """Create the directory, bind the socket and write the initial manifest."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.capture_id = new_capture_id(self.raw_dir)
        self.directory = self.raw_dir / self.capture_id
        self.directory.mkdir()
        s = self.settings
        self._compressor = BackgroundCompressor() if s.compress else None
        self._writer = RotatingRawLogWriter(
            self.directory,
            capture_metadata={"capture_id": self.capture_id, "pitwall_version": __version__},
            rotate_seconds=s.rotate_seconds,
            rotate_bytes=s.rotate_mb * 1024 * 1024,
            on_segment_closed=self._compressor.submit if self._compressor else None,
        )
        self.health = HealthTracker(expected_hz=s.expected_hz)
        self.capture = UdpCapture(
            s.bind_host,
            s.port,
            self._writer,
            self.health,
            forward_to=[parse_endpoint(f) for f in s.forward],
            allow_sources=set(s.allow_sources) or None,
            on_status=self.on_status,
            status_interval_s=s.status_interval_s,
        )
        self._started_utc = _utc_now()
        self._manifest: dict[str, Any] = {
            "capture_id": self.capture_id,
            "status": "running",
            "label": self.label,
            "note": self.note,
            "started_utc": self._started_utc,
            "pitwall_version": __version__,
            "host": platform.node(),
            "bind": f"{self.capture.bound_address[0]}:{self.capture.bound_address[1]}",
            "recv_buffer_bytes": self.capture.recv_buffer_bytes,
            "settings": {
                "forward": s.forward,
                "allow_sources": s.allow_sources,
                "rotate_seconds": s.rotate_seconds,
                "rotate_mb": s.rotate_mb,
                "compress": s.compress,
                "expected_hz": s.expected_hz,
            },
        }
        write_manifest(self.directory, self._manifest)

    def run(self) -> dict[str, Any]:
        """Blocking. Returns the final manifest."""
        if self.capture is None:
            self.prepare()
        assert self.capture is not None and self.health is not None
        try:
            self.capture.run(self.stop_event)
        finally:
            self._writer.close()
            if self._compressor is not None:
                self._compressor.close()
            manifest = dict(self._manifest)
            manifest.update(
                status="complete",
                stopped_utc=_utc_now(),
                counters=self.capture.counters.__dict__,
                segments=[p.name for p in self._writer.segments],
                compression_errors=[
                    f"{r.source.name}: {r.error}"
                    for r in (self._compressor.results if self._compressor else [])
                    if r.error
                ],
                health=self.health.report(),
            )
            write_manifest(self.directory, manifest)
        return manifest


def analyse_capture(
    path: Path, expected_hz: int | None = 60
) -> tuple[HealthTracker, CaptureReadSummary]:
    """Recompute health from a stored capture (directory or single segment)."""
    health = HealthTracker(expected_hz=expected_hz)
    summary = CaptureReadSummary()
    for rec in iter_capture(path, summary):
        health.observe(rec.wall_ns, rec.mono_ns, rec.payload)
    return health, summary


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
