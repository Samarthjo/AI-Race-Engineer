"""Basic live status from the packets `pitwall capture run` forwards (default UDP 20778).

Stage 2 scope: is data arriving, which session/track, current lap, lap distance, speed and
gear. The full live telemetry screen arrives in Stage 6. This listener only reads packets;
recording stays the job of the capture process.
"""

from __future__ import annotations

import collections
import socket
import threading
import time
from typing import Any

import numpy as np

from pitwall.f1udp import enums, f125
from pitwall.f1udp.header import HEADER_SIZE, PacketHeader, PacketId

DEFAULT_LIVE_PORT = 20778
RECEIVING_TIMEOUT_S = 2.0


class LiveStatus:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_LIVE_PORT):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._times: collections.deque[float] = collections.deque()
        self._state: dict[str, Any] = {}
        self._last_packet: float | None = None
        self.error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.host, self.port))
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = f"cannot listen on UDP {self.host}:{self.port}: {exc}"
            return
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="pitwall-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._sock is not None:
            self._sock.close()

    # ------------------------------------------------------------------ receiving

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                payload = self._sock.recv(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            self.observe(payload, time.monotonic())

    def observe(self, payload: bytes, now: float) -> None:
        if len(payload) < HEADER_SIZE:
            return
        hdr = PacketHeader.unpack(payload)
        update: dict[str, Any] = {
            "packet_format": hdr.packet_format,
            "game_version": hdr.game_version,
            "session_uid": f"{hdr.session_uid:016x}",
        }
        if hdr.packet_format == f125.PACKET_FORMAT and hdr.player_car_index < f125.MAX_CARS:
            pid = hdr.packet_id
            dtype = f125.PACKET_DTYPES.get(pid)
            if dtype is not None and len(payload) == dtype.itemsize:
                if pid == PacketId.LAP_DATA:
                    car = np.frombuffer(payload, dtype, count=1)[0]["car"][hdr.player_car_index]
                    update.update(
                        lap_number=int(car["current_lap_num"]),
                        lap_distance_m=round(float(car["lap_distance"]), 1),
                        current_lap_time_ms=int(car["current_lap_time_in_ms"]),
                        last_lap_time_ms=int(car["last_lap_time_in_ms"]) or None,
                        sector=int(car["sector"]) + 1,
                        lap_invalid=bool(car["current_lap_invalid"]),
                    )
                elif pid == PacketId.CAR_TELEMETRY:
                    car = np.frombuffer(payload, dtype, count=1)[0]["car"][hdr.player_car_index]
                    update.update(
                        speed_kmh=int(car["speed"]),
                        gear=int(car["gear"]),
                        throttle=round(float(car["throttle"]), 3),
                        brake=round(float(car["brake"]), 3),
                    )
                elif pid == PacketId.SESSION:
                    rec = np.frombuffer(payload, dtype, count=1)[0]
                    track_id = int(rec["track_id"])
                    session_type = int(rec["session_type"])
                    update.update(
                        track_id=track_id,
                        track_name=enums.name(enums.TRACKS, track_id, "track"),
                        session_type_name=enums.name(enums.SESSION_TYPES, session_type),
                    )
        with self._lock:
            self._last_packet = now
            self._times.append(now)
            while self._times and now - self._times[0] > 2.0:
                self._times.popleft()
            self._state.update(update)

    # ------------------------------------------------------------------- snapshot

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self._lock:
            age = None if self._last_packet is None else now - self._last_packet
            recent = [t for t in self._times if now - t <= 2.0]
            rate = len(recent) / 2.0
            state = dict(self._state)
        return {
            "listening": self._sock is not None and self.error is None,
            "port": self.port,
            "error": self.error,
            "receiving": age is not None and age <= RECEIVING_TIMEOUT_S,
            "last_packet_age_s": None if age is None else round(age, 1),
            "packets_per_s": round(rate, 1),
            **state,
        }
