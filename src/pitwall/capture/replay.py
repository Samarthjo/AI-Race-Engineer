"""Replay a raw capture as UDP datagrams, preserving original timing (or as fast as possible).

Lets the live pipeline, UI and tests run without the PS5.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from pitwall.capture.rawlog import RawRecord

# Silences longer than this in the original capture (pauses, menus) are shortened on replay.
MAX_REPLAY_GAP_S = 2.0


@dataclass
class ReplayResult:
    sent: int
    seconds: float


def replay(
    records: Iterable[RawRecord],
    target: tuple[str, int],
    speed: float = 1.0,
    max_gap_s: float = MAX_REPLAY_GAP_S,
    stop_event: threading.Event | None = None,
) -> ReplayResult:
    """Send each record's payload to ``target``.

    ``speed`` 1.0 = real time, 2.0 = twice as fast, 0 = no pacing (as fast as possible).
    """
    if speed < 0:
        raise ValueError("speed must be >= 0")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    start = time.perf_counter()
    schedule = 0.0  # seconds after start at which the current record is due
    prev_mono: int | None = None
    try:
        for rec in records:
            if stop_event is not None and stop_event.is_set():
                break
            if speed > 0 and prev_mono is not None:
                gap = max(0.0, (rec.mono_ns - prev_mono) / 1e9)
                schedule += min(gap, max_gap_s) / speed
                delay = schedule - (time.perf_counter() - start)
                if delay > 0:
                    time.sleep(delay)
            prev_mono = rec.mono_ns
            sock.sendto(rec.payload, target)
            sent += 1
    finally:
        sock.close()
    return ReplayResult(sent=sent, seconds=time.perf_counter() - start)
