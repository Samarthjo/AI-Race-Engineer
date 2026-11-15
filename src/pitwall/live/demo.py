"""A synthetic session replayed into the live engine, to try the Live page without the PS5.

The demo session of ``pitwall demo`` (laps with known mistakes on the synthetic track) is
generated in memory and fed to the engine at real time (or faster). The engine marks it as
synthetic, so it is only ever compared with synthetic laps, and the page says so.
"""

from __future__ import annotations

import threading
import time

from pitwall.live.engine import LiveEngine
from pitwall.synth.demo import demo_plans
from pitwall.synth.session import SessionSynth
from pitwall.synth.track import build_track


class DemoReplay:
    def __init__(self, engine: LiveEngine, speed: float = 1.0, loops: int = 1) -> None:
        self.engine = engine
        self.speed = max(0.1, speed)
        self.loops = loops
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pitwall-live-demo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self.engine.set_synthetic(True)
        try:
            for loop in range(self.loops):
                session = SessionSynth(
                    track=build_track(),
                    track_id=-1,
                    session_uid=0xDE30_0000_0000_0000 | (int(time.time()) + loop),
                ).run(demo_plans(), final_partial=0.3)
                t0 = time.monotonic()
                first = session.records[0].mono_ns
                for rec in session.records:
                    due = t0 + (rec.mono_ns - first) / 1e9 / self.speed
                    wait = due - time.monotonic()
                    if wait > 0 and self._stop.wait(wait):
                        return
                    self.engine.observe(rec.payload, time.monotonic())
                self.engine.flush()
                if self._stop.is_set():
                    return
        finally:
            self.engine.set_synthetic(False)
