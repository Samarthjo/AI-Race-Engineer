"""Capture health: packet rates, formats, sessions, packet loss, flashbacks and gaps.

Only the common packet header is parsed, so this works before full decoding exists
(Stage 0) and on captures from any game version.

Packet loss is estimated per (session, packet type) from ``overallFrameIdentifier``,
which advances every game frame and does not rewind on flashbacks. The typical
(median) frame step between packets is taken as the send cadence; larger steps count
as missing packets. Silences longer than ``PAUSE_GAP_NS`` (pause menu, loading) are
treated as pauses, not loss.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pitwall.f1udp.header import PacketHeader, PacketId, PacketTooShortError, packet_name

PAUSE_GAP_NS = 1_000_000_000
EXPECTED_PACKET_FORMAT = 2025  # "F1 25" UDP format (agreed: base 2025 cars)
LOSS_WARN_PCT = 0.5
LOSS_FAIL_PCT = 2.0
RATE_TOLERANCE = 0.10
MIN_STEPS_FOR_LOSS = 30

# Packet types sent at the configured send rate; used for the headline loss figure.
HIGH_RATE_IDS: frozenset[int] = frozenset(
    {
        PacketId.MOTION,
        PacketId.LAP_DATA,
        PacketId.CAR_TELEMETRY,
        PacketId.CAR_STATUS,
        PacketId.MOTION_EX,
    }
)


@dataclass
class StreamTracker:
    """Frame-sequence tracking for one packet type within one session."""

    last_overall: int | None = None
    last_frame: int | None = None
    last_mono: int | None = None
    steps: Counter[int] = field(default_factory=Counter)
    received: int = 0
    rewinds: int = 0  # frameIdentifier went back while overall advanced: flashback
    resets: int = 0  # overallFrameIdentifier went back: game restarted the counter
    duplicates: int = 0
    pauses: int = 0

    def observe(self, overall: int, frame: int, mono_ns: int) -> None:
        self.received += 1
        if self.last_overall is not None and self.last_frame is not None:
            step = overall - self.last_overall
            if step < 0:
                self.resets += 1
            elif step == 0:
                self.duplicates += 1
            elif self.last_mono is not None and mono_ns - self.last_mono > PAUSE_GAP_NS:
                self.pauses += 1
            else:
                self.steps[step] += 1
            if step > 0 and frame < self.last_frame:
                self.rewinds += 1
        self.last_overall = overall
        self.last_frame = frame
        self.last_mono = mono_ns

    def cadence(self) -> int | None:
        """Median frame step between consecutive packets, or None if too few samples."""
        total = sum(self.steps.values())
        if total < MIN_STEPS_FOR_LOSS:
            return None
        half = total / 2
        running = 0
        for step in sorted(self.steps):
            running += self.steps[step]
            if running >= half:
                return step
        return None  # unreachable

    def loss(self) -> tuple[int, int] | None:
        """(estimated missing packets, expected packets) or None if insufficient data."""
        cadence = self.cadence()
        if cadence is None:
            return None
        missing = 0
        counted = 0
        for step, count in self.steps.items():
            counted += count
            if step > 1.5 * cadence:
                missing += count * (round(step / cadence) - 1)
        return missing, counted + missing


@dataclass
class SessionHealth:
    session_uid: int
    first_wall_ns: int
    first_mono_ns: int
    last_wall_ns: int = 0
    last_mono_ns: int = 0
    packets: int = 0
    active_ns: int = 0
    first_session_time: float | None = None
    last_session_time: float | None = None
    counts: Counter[int] = field(default_factory=Counter)
    streams: dict[int, StreamTracker] = field(default_factory=dict)

    def observe(self, hdr: PacketHeader, wall_ns: int, mono_ns: int) -> None:
        if self.packets and 0 <= mono_ns - self.last_mono_ns <= PAUSE_GAP_NS:
            self.active_ns += mono_ns - self.last_mono_ns
        self.packets += 1
        self.last_wall_ns = wall_ns
        self.last_mono_ns = mono_ns
        if self.first_session_time is None:
            self.first_session_time = hdr.session_time
        self.last_session_time = hdr.session_time
        self.counts[hdr.packet_id] += 1
        stream = self.streams.get(hdr.packet_id)
        if stream is None:
            stream = self.streams[hdr.packet_id] = StreamTracker()
        stream.observe(hdr.overall_frame_identifier, hdr.frame_identifier, mono_ns)

    def high_rate_loss(self) -> tuple[int, int]:
        missing = expected = 0
        for pid in HIGH_RATE_IDS:
            stream = self.streams.get(pid)
            loss = stream.loss() if stream else None
            if loss:
                missing += loss[0]
                expected += loss[1]
        return missing, expected

    def flashback_rewinds(self) -> int:
        stream = self.streams.get(PacketId.CAR_TELEMETRY) or self.streams.get(PacketId.LAP_DATA)
        return stream.rewinds if stream else 0


@dataclass
class IntervalStats:
    seconds: float
    packets: int
    packets_per_s: float
    telemetry_hz: float
    session_uid: int | None
    packet_format: int | None
    game_version: str | None
    sources: list[str]
    loss_pct: float | None
    malformed: int


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail" | "info"
    detail: str


class HealthTracker:
    """Accumulates capture health from raw datagrams (live or from a raw log)."""

    def __init__(self, expected_hz: int | None = 60):
        self.expected_hz = expected_hz
        self.packets = 0
        self.bytes = 0
        self.malformed = 0
        self.active_ns = 0
        self.first_wall_ns: int | None = None
        self.last_wall_ns: int | None = None
        self._last_mono: int | None = None
        self.counts: Counter[int] = Counter()
        self.bytes_by_id: Counter[int] = Counter()
        self.sizes: dict[int, Counter[int]] = {}
        self.formats: Counter[tuple[int, int, int, int]] = Counter()
        self.sources: Counter[str] = Counter()
        self.sessions: dict[int, SessionHealth] = {}
        self.gap_count = 0
        self.gaps: list[tuple[int, int]] = []  # (wall_ns at gap start, duration_ns), largest kept
        self._current_session: int | None = None
        self._last_header: PacketHeader | None = None
        # interval counters for live status
        self._iv_start_mono: int | None = None
        self._iv_packets = 0
        self._iv_telemetry = 0
        self._iv_malformed = 0
        self._iv_sources: set[str] = set()
        self._iv_loss_base: tuple[int, int] = (0, 0)

    # ------------------------------------------------------------------ ingest

    def observe(
        self, wall_ns: int, mono_ns: int, payload: bytes, source: str | None = None
    ) -> None:
        self.packets += 1
        self.bytes += len(payload)
        self._iv_packets += 1
        if self._iv_start_mono is None:
            self._iv_start_mono = mono_ns
        if source is not None:
            self.sources[source] += 1
            self._iv_sources.add(source)
        if self.first_wall_ns is None:
            self.first_wall_ns = wall_ns
        self.last_wall_ns = wall_ns
        if self._last_mono is not None:
            delta = mono_ns - self._last_mono
            if 0 <= delta <= PAUSE_GAP_NS:
                self.active_ns += delta
            elif delta > PAUSE_GAP_NS:
                self._record_gap(wall_ns - delta, delta)
        self._last_mono = mono_ns

        try:
            hdr = PacketHeader.unpack(payload)
        except PacketTooShortError:
            self.malformed += 1
            self._iv_malformed += 1
            return
        self._last_header = hdr
        pid = hdr.packet_id
        self.counts[pid] += 1
        self.bytes_by_id[pid] += len(payload)
        sizes = self.sizes.get(pid)
        if sizes is None:
            sizes = self.sizes[pid] = Counter()
        if len(sizes) < 16 or len(payload) in sizes:
            sizes[len(payload)] += 1
        self.formats[
            (hdr.packet_format, hdr.game_year, hdr.game_major_version, hdr.game_minor_version)
        ] += 1
        if pid == PacketId.CAR_TELEMETRY:
            self._iv_telemetry += 1

        session = self.sessions.get(hdr.session_uid)
        if session is None:
            session = self.sessions[hdr.session_uid] = SessionHealth(
                hdr.session_uid, wall_ns, mono_ns
            )
        session.observe(hdr, wall_ns, mono_ns)
        self._current_session = hdr.session_uid

    def _record_gap(self, start_wall_ns: int, duration_ns: int) -> None:
        self.gap_count += 1
        self.gaps.append((start_wall_ns, duration_ns))
        if len(self.gaps) > 40:
            self.gaps.sort(key=lambda g: g[1], reverse=True)
            del self.gaps[20:]

    # ---------------------------------------------------------------- summaries

    def total_loss(self) -> tuple[int, int]:
        missing = expected = 0
        for s in self.sessions.values():
            m, e = s.high_rate_loss()
            missing += m
            expected += e
        return missing, expected

    def interval(self, now_mono_ns: int | None = None) -> IntervalStats:
        """Stats since the previous call (for a live status line); resets the interval."""
        now = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
        start = self._iv_start_mono if self._iv_start_mono is not None else now
        secs = max((now - start) / 1e9, 1e-9)
        missing, expected = self.total_loss()
        base_m, base_e = self._iv_loss_base
        d_m, d_e = missing - base_m, expected - base_e
        hdr = self._last_header
        stats = IntervalStats(
            seconds=secs,
            packets=self._iv_packets,
            packets_per_s=self._iv_packets / secs,
            telemetry_hz=self._iv_telemetry / secs,
            session_uid=self._current_session,
            packet_format=hdr.packet_format if hdr else None,
            game_version=hdr.game_version if hdr else None,
            sources=sorted(self._iv_sources),
            loss_pct=(100.0 * d_m / d_e) if d_e > 0 else None,
            malformed=self._iv_malformed,
        )
        self._iv_start_mono = now
        self._iv_packets = 0
        self._iv_telemetry = 0
        self._iv_malformed = 0
        self._iv_sources = set()
        self._iv_loss_base = (missing, expected)
        return stats

    def checks(self) -> list[Check]:
        out: list[Check] = []
        if self.packets == 0:
            out.append(
                Check(
                    "packets_received",
                    "fail",
                    "No packets received. Check PS5 UDP settings (IP, port) and the Mac firewall.",
                )
            )
            return out
        out.append(Check("packets_received", "ok", f"{self.packets} packets"))

        fmts = sorted({f[0] for f in self.formats})
        if fmts == [EXPECTED_PACKET_FORMAT]:
            out.append(Check("udp_format", "ok", f"packetFormat {EXPECTED_PACKET_FORMAT} (F1 25)"))
        elif fmts:
            out.append(
                Check(
                    "udp_format",
                    "warn",
                    f"packetFormat values {fmts}; expected only {EXPECTED_PACKET_FORMAT}. "
                    "Set the in-game UDP Format to F1 25 (not the 2026 Season Pack format).",
                )
            )

        unknown = sorted(pid for pid in self.counts if pid not in PacketId._value2member_map_)
        if unknown:
            out.append(
                Check("packet_ids", "warn", f"Unknown packet ids {unknown} (different UDP format?)")
            )
        else:
            out.append(Check("packet_ids", "ok", f"{len(self.counts)} known packet types seen"))

        if self.expected_hz and self.active_ns > 0:
            hz = self.counts.get(PacketId.CAR_TELEMETRY, 0) / (self.active_ns / 1e9)
            ok = abs(hz - self.expected_hz) <= RATE_TOLERANCE * self.expected_hz
            out.append(
                Check(
                    "telemetry_rate",
                    "ok" if ok else "warn",
                    f"CAR_TELEMETRY {hz:.1f} Hz while active (expected {self.expected_hz} Hz)",
                )
            )

        missing, expected = self.total_loss()
        if expected:
            pct = 100.0 * missing / expected
            status = "ok" if pct < LOSS_WARN_PCT else ("warn" if pct < LOSS_FAIL_PCT else "fail")
            out.append(
                Check(
                    "packet_loss",
                    status,
                    f"~{pct:.2f}% of high-rate packets missing ({missing}/{expected}); "
                    f"target < {LOSS_WARN_PCT}%",
                )
            )
        else:
            out.append(Check("packet_loss", "info", "Not enough data to estimate loss"))

        out.append(
            Check(
                "malformed",
                "ok" if self.malformed == 0 else "warn",
                f"{self.malformed} datagrams shorter than a packet header",
            )
        )
        if len(self.sources) > 1:
            out.append(
                Check("sources", "warn", f"Packets from several hosts: {sorted(self.sources)}")
            )
        elif self.sources:
            out.append(Check("sources", "ok", f"Single source {next(iter(self.sources))}"))
        return out

    def report(self) -> dict[str, Any]:
        """JSON-serialisable health report."""
        active_s = self.active_ns / 1e9
        missing, expected = self.total_loss()
        types = []
        for pid in sorted(self.counts):
            types.append(
                {
                    "id": pid,
                    "name": packet_name(pid),
                    "count": self.counts[pid],
                    "bytes": self.bytes_by_id[pid],
                    "rate_hz": round(self.counts[pid] / active_s, 2) if active_s else None,
                    "sizes": {str(k): v for k, v in sorted(self.sizes.get(pid, {}).items())},
                }
            )
        sessions = []
        for s in self.sessions.values():
            m, e = s.high_rate_loss()
            per_stream = {}
            for pid in sorted(s.streams):
                st = s.streams[pid]
                loss = st.loss()
                per_stream[packet_name(pid)] = {
                    "received": st.received,
                    "cadence_frames": st.cadence(),
                    "missing_est": loss[0] if loss else None,
                    "resets": st.resets,
                    "pauses": st.pauses,
                }
            sessions.append(
                {
                    "session_uid": f"{s.session_uid:016x}",
                    "packets": s.packets,
                    "first_utc": _iso(s.first_wall_ns),
                    "last_utc": _iso(s.last_wall_ns),
                    "active_s": round(s.active_ns / 1e9, 2),
                    "session_time_span_s": [s.first_session_time, s.last_session_time],
                    "flashback_rewinds": s.flashback_rewinds(),
                    "loss_pct": round(100.0 * m / e, 3) if e else None,
                    "streams": per_stream,
                }
            )
        largest = sorted(self.gaps, key=lambda g: g[1], reverse=True)[:10]
        return {
            "packets": self.packets,
            "bytes": self.bytes,
            "malformed": self.malformed,
            "first_utc": _iso(self.first_wall_ns),
            "last_utc": _iso(self.last_wall_ns),
            "active_s": round(active_s, 2),
            "sources": dict(self.sources),
            "formats": [
                {
                    "packet_format": f[0],
                    "game_year": f[1],
                    "game_version": f"{f[2]}.{f[3]:02d}",
                    "count": n,
                }
                for f, n in self.formats.most_common()
            ],
            "packet_types": types,
            "loss": {
                "missing_est": missing,
                "expected": expected,
                "pct": round(100.0 * missing / expected, 3) if expected else None,
            },
            "sessions": sessions,
            "gaps": {
                "count": self.gap_count,
                "threshold_s": PAUSE_GAP_NS / 1e9,
                "largest": [
                    {"at_utc": _iso(w), "duration_s": round(d / 1e9, 2)} for w, d in largest
                ],
            },
            "checks": [c.__dict__ for c in self.checks()],
        }


def _iso(wall_ns: int | None) -> str | None:
    if wall_ns is None:
        return None
    secs, ns = divmod(wall_ns, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{ns // 1_000_000:03d}Z"
