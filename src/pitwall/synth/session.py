"""Synthetic F1 25 session built with the real packet layouts (never real telemetry).

Every lap is described by a :class:`LapPlan`. Two driving modes:

* **constant speed** on a circle (``duration_s``): simple fixtures for lap bookkeeping;
* **driver model** on the synthetic test track (``driver``, needs ``SessionSynth(track=...)``):
  braking zones, corner speeds, throttle, gears and steering from
  :func:`pitwall.synth.track.simulate_lap`.

Scenario switches add a flashback, an invalidation, a pause or packet loss. The generator
knows the true answer for every lap (time, sectors, validity, and for driver-model laps the
exact time at every distance), so the pipeline can be checked exactly.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import numpy as np

from pitwall.capture.rawlog import RawRecord
from pitwall.f1udp import f125
from pitwall.f1udp.header import PacketId
from pitwall.synth.track import DriverProfile, LapProfile, Track, simulate_lap

HZ = 60
PLAYER = 3  # deliberately not 0, to prove the player index is honoured


@dataclass
class LapPlan:
    duration_s: float | None = None  # constant-speed lap time (out-lap: pit exit to line)
    kind: str = "flying"  # "out" (starts mid-track) | "flying"
    driver: DriverProfile | None = None  # driver-model lap on the synthetic track
    invalid_from: float | None = None  # fraction of the lap where the lap becomes invalid
    flashback_at: float | None = None  # fraction the flashback rewinds to
    flashback_erased_s: float = 1.0  # driving that the flashback erases
    pause_at: float | None = None
    pause_s: float = 30.0
    drop_at: float | None = None  # fraction where all packets are lost...
    drop_frames: int = 0  # ...for this many frames


@dataclass
class TrueLap:
    lap_number: int
    kind: str
    lap_time_ms: int
    s1_ms: int
    s2_ms: int
    s3_ms: int
    valid: bool
    frames: int  # canonical lap-data rows (after flashback truncation, minus dropped)
    dropped: int
    profile: LapProfile | None = None  # driver-model laps: exact time at every distance


@dataclass
class SyntheticSession:
    records: list[RawRecord]
    laps: list[TrueLap]
    session_uid: int
    track_length: float


def _template(pid: int) -> np.ndarray:
    return np.zeros(1, dtype=f125.PACKET_DTYPES[pid])


@dataclass
class _State:
    session_uid: int
    frame: int = 0
    overall: int = 0
    session_time: float = 0.0
    mono_ns: int = 1_000_000_000
    wall_ns: int = 1_763_000_000_000_000_000
    records: list[RawRecord] = field(default_factory=list)


class SessionSynth:
    def __init__(
        self,
        track_length: float = 1000.0,
        session_uid: int = 0x1234_5678_9ABC_DEF0,
        track_id: int = 11,  # Monza
        session_type: int = 18,  # Time Trial
        game_mode: int = 5,  # Time Trial
        traction_control: int = 0,
        anti_lock_brakes: int = 0,
        track: Track | None = None,
        start_wall_ns: int | None = None,
    ):
        self.tc = traction_control
        self.abs = anti_lock_brakes
        self.track = track
        self.L = track.length if track is not None else track_length
        self._profiles: dict[str, LapProfile] = {}  # keyed by repr(DriverProfile)
        self.track_id = track_id
        self.session_type = session_type
        self.game_mode = game_mode
        self.st = _State(session_uid)
        if start_wall_ns is not None:
            self.st.wall_ns = start_wall_ns
        self.history: list[tuple[int, int, int, int, int]] = []  # time, s1, s2, s3, flags
        self.fuel = 10.0

    # ---------------------------------------------------------------- packet helpers

    def _header(self, arr: np.ndarray, pid: int) -> None:
        h = arr["header"]
        h["packet_format"] = 2025
        h["game_year"] = 25
        h["game_major_version"] = 1
        h["game_minor_version"] = 12
        h["packet_version"] = 1
        h["packet_id"] = pid
        h["session_uid"] = self.st.session_uid
        h["session_time"] = self.st.session_time
        h["frame_identifier"] = self.st.frame
        h["overall_frame_identifier"] = self.st.overall
        h["player_car_index"] = PLAYER
        h["secondary_player_car_index"] = 255

    def _emit(self, arr: np.ndarray) -> None:
        payload = arr.tobytes()
        assert len(payload) == arr.dtype.itemsize
        self.st.records.append(RawRecord(self.st.wall_ns, self.st.mono_ns, payload))

    def _event(self, code: str, details: bytes = b"") -> None:
        arr = _template(PacketId.EVENT)
        self._header(arr, PacketId.EVENT)
        arr["event_string_code"] = code.encode()
        arr["event_details"] = np.void(details.ljust(12, b"\x00"))
        self._emit(arr)

    def _session_packet(self) -> None:
        arr = _template(PacketId.SESSION)
        self._header(arr, PacketId.SESSION)
        arr["weather"] = 0
        arr["track_temperature"] = 33
        arr["air_temperature"] = 24
        arr["track_length"] = int(self.L)
        arr["session_type"] = self.session_type
        arr["track_id"] = self.track_id
        arr["formula"] = 0
        arr["game_mode"] = self.game_mode
        arr["gearbox_assist"] = 1  # manual
        arr["sector2_lap_distance_start"] = self.L / 3
        arr["sector3_lap_distance_start"] = 2 * self.L / 3
        self._emit(arr)

    def _history_packet(self) -> None:
        arr = _template(PacketId.SESSION_HISTORY)
        self._header(arr, PacketId.SESSION_HISTORY)
        arr["car_idx"] = PLAYER
        arr["num_laps"] = len(self.history) + 1
        lh = arr["lap_history_data"]
        for i, (t, s1, s2, s3, flags) in enumerate(self.history):
            lh["lap_time_in_ms"][0, i] = t
            lh["sector1_time_ms_part"][0, i] = s1 % 60_000
            lh["sector1_time_minutes_part"][0, i] = s1 // 60_000
            lh["sector2_time_ms_part"][0, i] = s2 % 60_000
            lh["sector2_time_minutes_part"][0, i] = s2 // 60_000
            lh["sector3_time_ms_part"][0, i] = s3 % 60_000
            lh["sector3_time_minutes_part"][0, i] = s3 // 60_000
            lh["lap_valid_bit_flags"][0, i] = flags
        self._emit(arr)

    def _low_rate(self, k_frame: int) -> None:
        if k_frame % 30 == 0:
            self._session_packet()
            setup = _template(PacketId.CAR_SETUPS)
            self._header(setup, PacketId.CAR_SETUPS)
            setup["car"]["front_wing"][0, PLAYER] = 20
            setup["car"]["rear_wing"][0, PLAYER] = 18
            setup["car"]["brake_bias"][0, PLAYER] = 56
            setup["car"]["tyre_pressure"][0, PLAYER] = [21.5, 21.5, 23.0, 23.0]
            self._emit(setup)
        if k_frame % 60 == 0:
            self._history_packet()
            tt = _template(PacketId.TIME_TRIAL)
            self._header(tt, PacketId.TIME_TRIAL)
            tt["personal_best"]["valid"] = 1
            tt["personal_best"]["gearbox_assist"] = 1
            self._emit(tt)
        if k_frame % 300 == 0:
            part = _template(PacketId.PARTICIPANTS)
            self._header(part, PacketId.PARTICIPANTS)
            part["num_active_cars"] = 1
            part["car"]["team_id"][0, PLAYER] = 1
            part["car"]["name"][0, PLAYER] = b"Player"
            self._emit(part)

    def _frame_packets(
        self,
        lap_num: int,
        st: dict[str, float],
        last_ms: int,
        s1: int,
        s2: int,
        invalid: int,
        driver_status: int,
        k_frame: int,
    ) -> None:
        dist = float(st["dist"])
        speed_ms = float(st["speed"])
        sector = 0 if dist < self.L / 3 else (1 if dist < 2 * self.L / 3 else 2)
        ld = _template(PacketId.LAP_DATA)
        self._header(ld, PacketId.LAP_DATA)
        car = ld["car"]
        car["last_lap_time_in_ms"][0, PLAYER] = last_ms
        car["current_lap_time_in_ms"][0, PLAYER] = int(st["cur_ms"])
        car["sector1_time_ms_part"][0, PLAYER] = s1 % 60_000
        car["sector1_time_minutes_part"][0, PLAYER] = s1 // 60_000
        car["sector2_time_ms_part"][0, PLAYER] = s2 % 60_000
        car["sector2_time_minutes_part"][0, PLAYER] = s2 // 60_000
        car["lap_distance"][0, PLAYER] = dist
        car["total_distance"][0, PLAYER] = (lap_num - 1) * self.L + dist
        car["car_position"][0, PLAYER] = 1
        car["current_lap_num"][0, PLAYER] = lap_num
        car["sector"][0, PLAYER] = sector
        car["current_lap_invalid"][0, PLAYER] = invalid
        car["driver_status"][0, PLAYER] = driver_status
        car["result_status"][0, PLAYER] = 2
        self._emit(ld)

        tel = _template(PacketId.CAR_TELEMETRY)
        self._header(tel, PacketId.CAR_TELEMETRY)
        c = tel["car"]
        c["speed"][0, PLAYER] = round(speed_ms * 3.6)
        c["throttle"][0, PLAYER] = st["throttle"]
        c["brake"][0, PLAYER] = st["brake"]
        c["steer"][0, PLAYER] = st["steer"]
        c["gear"][0, PLAYER] = int(st["gear"])
        c["engine_rpm"][0, PLAYER] = round(float(st["rpm"]))
        c["tyres_surface_temperature"][0, PLAYER] = [95, 95, 100, 100]
        c["tyres_pressure"][0, PLAYER] = [21.6, 21.6, 23.1, 23.1]
        self._emit(tel)

        mo = _template(PacketId.MOTION)
        self._header(mo, PacketId.MOTION)
        m = mo["car"]
        m["world_position_x"][0, PLAYER] = st["x"]
        m["world_position_z"][0, PLAYER] = st["z"]
        m["g_force_lateral"][0, PLAYER] = st["g_lat"]
        self._emit(mo)

        stt = _template(PacketId.CAR_STATUS)
        self._header(stt, PacketId.CAR_STATUS)
        s = stt["car"]
        s["fuel_in_tank"][0, PLAYER] = self.fuel
        s["actual_tyre_compound"][0, PLAYER] = 18  # C3
        s["visual_tyre_compound"][0, PLAYER] = 17  # medium
        s["tyres_age_laps"][0, PLAYER] = 2
        s["traction_control"][0, PLAYER] = self.tc
        s["anti_lock_brakes"][0, PLAYER] = self.abs
        self._emit(stt)

        mx = _template(PacketId.MOTION_EX)
        self._header(mx, PacketId.MOTION_EX)
        mx["player"]["wheel_speed"][0] = [speed_ms] * 4
        self._emit(mx)

        if k_frame % 6 == 0:
            dmg = _template(PacketId.CAR_DAMAGE)
            self._header(dmg, PacketId.CAR_DAMAGE)
            wear = 2.0 + self.st.overall / 10_000
            dmg["car"]["tyres_wear"][0, PLAYER] = [wear] * 4
            self._emit(dmg)
        self._low_rate(k_frame)

    def _tick(self, wall_extra_ns: int = 0) -> None:
        self.st.frame += 1
        self.st.overall += 1
        self.st.session_time += 1.0 / HZ
        step = round(1e9 / HZ) + wall_extra_ns
        self.st.mono_ns += step
        self.st.wall_ns += step
        self.fuel -= 0.0005

    # ------------------------------------------------------------------- lap models

    def _lap_model(self, plan: LapPlan) -> _LapModel:
        d0 = 0.5 * self.L if plan.kind == "out" else 0.0
        if plan.driver is not None:
            if self.track is None:
                raise ValueError("driver-model laps need SessionSynth(track=...)")
            key = repr(plan.driver)
            prof = self._profiles.get(key)
            if prof is None:
                prof = self._profiles[key] = simulate_lap(self.track, plan.driver)
            return _ProfileLapModel(prof, d0)
        if plan.duration_s is None:
            raise ValueError("LapPlan needs duration_s or driver")
        return _ConstantLapModel(self.L, plan.duration_s, d0)

    # ------------------------------------------------------------------- scenario

    def run(self, plans: list[LapPlan], final_partial: float | None = 0.4) -> SyntheticSession:
        self._event("SSTA")
        true_laps: list[TrueLap] = []
        last_ms = 0
        lap_num = 1
        k_global = 0
        b2, b3 = self.L / 3, 2 * self.L / 3
        for plan in plans:
            model = self._lap_model(plan)
            n = model.frames
            lap_ms = model.lap_ms
            flying = plan.kind != "out"
            s1_ms = model.official_ms(b2) if flying else 0
            s2_ms = model.official_ms(b3) - s1_ms if flying else 0
            s3_ms = lap_ms - s1_ms - s2_ms if flying else 0
            dropped = 0
            drop_lo = round(plan.drop_at * n) if plan.drop_at is not None else -1
            drop_hi = drop_lo + plan.drop_frames
            k = 0
            flashed = False
            resume_state: tuple[int, float, float] | None = None
            while k < n:
                st = model.state(k)
                dist = float(st["dist"])
                s1 = s1_ms if flying and dist >= b2 else 0
                s2 = s2_ms if flying and dist >= b3 else 0
                invalid = int(plan.invalid_from is not None and k >= plan.invalid_from * n)
                status = 3 if plan.kind == "out" else 1

                if drop_lo <= k < drop_hi:
                    dropped += 1  # all packets of this frame lost on the network
                else:
                    self._frame_packets(lap_num, st, last_ms, s1, s2, invalid, status, k_global)
                k_global += 1

                if plan.flashback_at is not None and not flashed:
                    k_target = round(plan.flashback_at * n)
                    if k == k_target:
                        resume_state = (self.st.frame, self.st.session_time, self.fuel)
                    erased_frames = round(plan.flashback_erased_s * HZ)
                    if resume_state is not None and k == k_target + erased_frames:
                        # Flashback: rewind frame id + session time to the target frame;
                        # the tick below then continues from target + 1 (overall keeps going).
                        target_frame, target_time, fuel = resume_state
                        self.st.frame = target_frame
                        self.st.session_time = target_time
                        self.fuel = fuel
                        self._event("FLBK", struct.pack("<If", target_frame, target_time))
                        flashed = True
                        k = k_target

                pause = plan.pause_at is not None and k == round(plan.pause_at * n)
                self._tick(int(plan.pause_s * 1e9) if pause else 0)
                k += 1

            valid = plan.invalid_from is None and plan.kind != "out"
            flags = (0x01 if valid else 0) | 0x0E
            self.history.append((lap_ms, s1_ms, s2_ms, s3_ms, flags))
            true_laps.append(
                TrueLap(
                    lap_num,
                    plan.kind,
                    lap_ms,
                    s1_ms,
                    s2_ms,
                    s3_ms,
                    valid,
                    n - dropped,
                    dropped,
                    model.profile,
                )
            )
            last_ms = lap_ms
            lap_num += 1

        if final_partial:
            last_plan = plans[-1] if plans else LapPlan(10.0)
            partial_plan = LapPlan(
                duration_s=last_plan.duration_s if last_plan.driver is None else None,
                driver=last_plan.driver,
            )
            if partial_plan.driver is None:
                partial_plan.duration_s = 10.0
            model = self._lap_model(partial_plan)
            for k in range(round(model.frames * final_partial)):
                st = model.state(k)
                self._frame_packets(lap_num, st, last_ms, 0, 0, 0, 1, k_global)
                k_global += 1
                self._tick()
        else:
            self._history_packet()
            self._event("SEND")
        return SyntheticSession(self.st.records, true_laps, self.st.session_uid, self.L)


class _LapModel:
    """Frame-by-frame state of one lap (k = frame index since the lap started)."""

    frames: int
    lap_ms: int
    profile: LapProfile | None = None

    def state(self, k: int) -> dict[str, float]:
        raise NotImplementedError

    def official_ms(self, d: float) -> int:
        """Exact time (ms) at which the car crosses distance ``d`` (sector lines)."""
        raise NotImplementedError


class _ConstantLapModel(_LapModel):
    def __init__(self, length: float, duration: float, d0: float):
        self.L = length
        self.d0 = d0
        self.duration = duration
        self.speed = (length - d0) / duration
        self.frames = math.ceil(duration * HZ - 1e-9)
        self.lap_ms = round(duration * 1000)

    def state(self, k: int) -> dict[str, float]:
        t = k / HZ
        dist = self.d0 + self.speed * t
        theta = 2 * math.pi * dist / self.L
        r = self.L / (2 * math.pi)
        return {
            "dist": dist,
            "cur_ms": round(t * 1000),
            "speed": self.speed,
            "throttle": 1.0,
            "brake": 0.0,
            "steer": 0.1,
            "gear": 7,
            "rpm": 11_000,
            "x": r * math.cos(theta),
            "z": r * math.sin(theta),
            "g_lat": self.speed**2 / r / 9.81,
        }

    def official_ms(self, d: float) -> int:
        return round((d - self.d0) / self.speed * 1000)


class _ProfileLapModel(_LapModel):
    def __init__(self, profile: LapProfile, d0: float):
        self.profile = profile
        self._prof = profile
        self.t0 = profile.time_at(d0) if d0 > 0 else 0.0
        duration = profile.lap_time - self.t0
        self.frames = math.ceil(duration * HZ - 1e-9)
        self.lap_ms = round(duration * 1000)

    def state(self, k: int) -> dict[str, float]:
        t = k / HZ
        dist = float(self._prof.distance_at_time(self.t0 + t))
        ch = self._prof.at(dist)
        track = self._prof.track
        kappa = float(np.interp(dist, track.s, track.curvature))
        v = float(ch["speed"])
        return {
            "dist": dist,
            "cur_ms": round(t * 1000),
            "speed": v,
            "throttle": float(ch["throttle"]),
            "brake": float(ch["brake"]),
            "steer": float(ch["steer"]),
            "gear": int(ch["gear"]),
            "rpm": float(ch["rpm"]),
            "x": float(ch["x"]),
            "z": float(ch["z"]),
            "g_lat": v * v * kappa / 9.81,
        }

    def official_ms(self, d: float) -> int:
        return round((self._prof.time_at(d) - self.t0) * 1000)
