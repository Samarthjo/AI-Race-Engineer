"""UDP capture loop: receive, store raw, forward, track health. Nothing else.

Kept deliberately simple so it can run for hours without losing packets. Decoding and
analysis happen in other processes that receive the forwarded copy (or read the raw log).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pitwall.capture.health import HealthTracker, IntervalStats
from pitwall.capture.rawlog import RotatingRawLogWriter

RECV_BUFFER_BYTES = 4 * 1024 * 1024
_POLL_TIMEOUT_S = 0.2


def parse_endpoint(text: str) -> tuple[str, int]:
    """Parse ``host:port`` (IPv4 or hostname)."""
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {text!r}")
    port_num = int(port)
    if not 0 < port_num < 65536:
        raise ValueError(f"port out of range in {text!r}")
    return host, port_num


@dataclass
class CaptureCounters:
    received: int = 0
    stored: int = 0
    forwarded: int = 0
    forward_errors: int = 0
    rejected_source: int = 0


class UdpCapture:
    """Blocking capture loop; call :meth:`run` and set ``stop_event`` to end it."""

    def __init__(
        self,
        bind_host: str,
        bind_port: int,
        writer: RotatingRawLogWriter | None,
        health: HealthTracker,
        forward_to: list[tuple[str, int]] | None = None,
        allow_sources: set[str] | None = None,
        on_status: Callable[[IntervalStats, CaptureCounters], None] | None = None,
        status_interval_s: float = 5.0,
    ):
        self._writer = writer
        self.health = health
        self._forward_to = list(forward_to or [])
        self._allow = allow_sources
        self._on_status = on_status
        self._status_ns = int(status_interval_s * 1e9)
        self.counters = CaptureCounters()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        self._sock.bind((bind_host, bind_port))
        self._sock.settimeout(_POLL_TIMEOUT_S)
        self.bound_address: tuple[str, int] = self._sock.getsockname()
        # The OS may cap the requested buffer; record what we actually got.
        self.recv_buffer_bytes: int = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)

        for host, port in self._forward_to:
            if port == self.bound_address[1] and host in ("127.0.0.1", "localhost", bind_host):
                raise ValueError(f"forward target {host}:{port} would loop back into capture")
        self._fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if self._forward_to else None

    def run(self, stop_event: threading.Event) -> None:
        last_status = time.monotonic_ns()
        try:
            while not stop_event.is_set():
                try:
                    payload, addr = self._sock.recvfrom(65535)
                except TimeoutError:
                    payload = None
                    addr = None
                now_mono = time.monotonic_ns()
                if payload is not None and addr is not None:
                    self._handle(payload, addr[0], now_mono)
                if self._writer is not None:
                    self._writer.tick(now_mono)
                if self._on_status is not None and now_mono - last_status >= self._status_ns:
                    self._on_status(self.health.interval(now_mono), self.counters)
                    last_status = now_mono
        finally:
            self.close()

    def _handle(self, payload: bytes, source: str, mono_ns: int) -> None:
        wall_ns = time.time_ns()
        self.counters.received += 1
        if self._allow is not None and source not in self._allow:
            self.counters.rejected_source += 1
            return
        if self._writer is not None:
            self._writer.write(wall_ns, mono_ns, payload)
            self.counters.stored += 1
        if self._fwd is not None:
            for target in self._forward_to:
                try:
                    self._fwd.sendto(payload, target)
                    self.counters.forwarded += 1
                except OSError:
                    self.counters.forward_errors += 1
        self.health.observe(wall_ns, mono_ns, payload, source)

    def close(self) -> None:
        self._sock.close()
        if self._fwd is not None:
            self._fwd.close()
