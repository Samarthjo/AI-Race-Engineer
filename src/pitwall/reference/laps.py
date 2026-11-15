"""Reference laps in Pitwall's common schema, with capability flags.

Lap tables (``laptimes.json``) and telemetry (``<lap>_tel.json``) come from FastF1's
processing of the official F1 feed. What this data can support is recorded as capability
flags, and the analysis checks them before computing any metric:

==============  ==========================  =========================================
channel         real F1 data                consequence
==============  ==========================  =========================================
speed, rpm      about 4 Hz, whole numbers   minimum speeds within a few km/h
throttle        about 4 Hz, 0-100 %         pickup point within about one sample
brake           on/off only                 brake *point* only: no pressure, release
                                            shape or trail braking
gear, DRS       about 4 Hz                  shift points within about one sample
steering        none                        no steering comparisons
wheel slip      none                        no lock-up or wheelspin comparisons
position        about 4 Hz, 1/10 m units    used to place the lap on your track
==============  ==========================  =========================================

The telemetry file merges two streams, car data and position data, on one time axis and
fills each stream's gaps by interpolation. Only *measured* samples are kept: raw car data
has whole-number speed, rpm and throttle, and raw position data has whole-number x, y and z.
Rows that are neither (the interpolated lap start and end) are dropped.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

LAP_TIME_TOLERANCE_S = 0.005  # telemetry must end at the official lap time
MIN_SAMPLES = 20
POSITION_SCALE = 0.1  # archive positions are in 1/10 m
PATH_RATIO_RANGE = (0.9, 1.1)  # position path length vs distance integrated from speed


class ReferenceDataError(ValueError):
    """The archive's data does not look like what Pitwall expects; nothing is guessed."""


def _missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() in ("", "None", "none", "nan", "NaN", "NaT")
    return isinstance(v, float) and math.isnan(v)


def _float(v: Any) -> float | None:
    if _missing(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ms(v: Any) -> int | None:
    f = _float(v)
    return None if f is None else round(f * 1000)


def _int(v: Any) -> int | None:
    f = _float(v)
    return None if f is None else round(f)


def _bool(v: Any) -> bool | None:
    if _missing(v):
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _str(v: Any) -> str | None:
    return None if _missing(v) else str(v)


def slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def ref_key(provider: str, season: int, event: str, session: str, driver: str, lap: int) -> str:
    return "-".join([provider, str(season), slug(event), slug(session), slug(driver), f"l{lap}"])


# ------------------------------------------------------------------------ lap tables


@dataclass(frozen=True)
class LapRow:
    driver: str
    driver_number: str | None
    team: str | None
    lap_number: int
    lap_time_ms: int | None
    sectors_ms: tuple[int | None, int | None, int | None]
    compound: str | None
    tyre_life: int | None
    session_part: str | None  # Q1 / Q2 / Q3 in qualifying
    track_status: str | None
    pit_out: bool
    pit_in: bool
    deleted: bool
    deleted_reason: str | None
    accurate: bool | None  # None: not reported
    generated: bool  # added by FastF1 to fill a gap, not timed
    personal_best: bool

    @property
    def issues(self) -> list[str]:
        """Why this lap cannot be a reference (empty: it can)."""
        out = []
        if self.lap_time_ms is None:
            out.append("no lap time")
        if self.deleted:
            out.append("deleted" + (f" ({self.deleted_reason})" if self.deleted_reason else ""))
        if self.accurate is None:
            out.append("timing accuracy not reported")
        elif not self.accurate:
            out.append("timing marked inaccurate")
        if self.generated:
            out.append("not timed (filled in by FastF1)")
        if self.pit_out:
            out.append("out lap")
        if self.pit_in:
            out.append("in lap")
        return out

    @property
    def usable(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sectors_ms"] = list(self.sectors_ms)
        d["issues"] = self.issues
        d["usable"] = self.usable
        return d


def parse_lap_rows(data: dict[str, Any], driver: str | None = None) -> list[LapRow]:
    """Rows of a ``laptimes.json`` / ``session_laptimes.json`` (columns of equal length)."""
    laps = data.get("lap")
    if not isinstance(laps, list) or "time" not in data:
        raise ReferenceDataError("lap table has no 'lap'/'time' columns")
    n = len(laps)

    def col(name: str) -> list[Any]:
        v = data.get(name)
        if v is None:
            return [None] * n
        if not isinstance(v, list) or len(v) != n:
            raise ReferenceDataError(f"lap table column {name!r} has the wrong length")
        return v

    cols = {
        k: col(k)
        for k in (
            "time",
            "s1",
            "s2",
            "s3",
            "compound",
            "life",
            "qs",
            "status",
            "pout",
            "pin",
            "del",
            "delR",
            "iacc",
            "ff1G",
            "pb",
            "drv",
            "dNum",
            "team",
        )
    }
    rows = []
    for i in range(n):
        lap_number = _int(laps[i])
        drv = _str(cols["drv"][i]) or driver
        if lap_number is None or drv is None:
            continue
        rows.append(
            LapRow(
                driver=drv,
                driver_number=_str(cols["dNum"][i]),
                team=_str(cols["team"][i]),
                lap_number=lap_number,
                lap_time_ms=_ms(cols["time"][i]),
                sectors_ms=(_ms(cols["s1"][i]), _ms(cols["s2"][i]), _ms(cols["s3"][i])),
                compound=_str(cols["compound"][i]),
                tyre_life=_int(cols["life"][i]),
                session_part=_str(cols["qs"][i]),
                track_status=_str(cols["status"][i]),
                pit_out=_float(cols["pout"][i]) is not None,
                pit_in=_float(cols["pin"][i]) is not None,
                deleted=bool(_bool(cols["del"][i])),
                deleted_reason=_str(cols["delR"][i]),
                accurate=_bool(cols["iacc"][i]),
                generated=bool(_bool(cols["ff1G"][i])),
                personal_best=bool(_bool(cols["pb"][i])),
            )
        )
    return rows


def fastest_usable(rows: list[LapRow], part: str | None = None) -> LapRow | None:
    """Fastest lap that can be a reference (optionally only in Q1 / Q2 / Q3)."""
    cands = [r for r in rows if r.usable and (part is None or r.session_part == part)]
    return min(cands, key=lambda r: (r.lap_time_ms or 0, r.lap_number), default=None)


def classification(rows: list[LapRow], part: str | None = None) -> list[LapRow]:
    """Each driver's fastest usable lap, fastest first."""
    best: dict[str, LapRow] = {}
    for r in rows:
        if not r.usable or (part is not None and r.session_part != part):
            continue
        cur = best.get(r.driver)
        if cur is None or (r.lap_time_ms or 0) < (cur.lap_time_ms or 0):
            best[r.driver] = r
    return sorted(best.values(), key=lambda r: (r.lap_time_ms or 0, r.driver))


# ------------------------------------------------------------------------ telemetry


@dataclass
class Samples:
    """Measured samples of one reference lap. Times in s since the lap start."""

    car_t: np.ndarray
    speed: np.ndarray  # km/h
    throttle: np.ndarray  # 0..1
    brake: np.ndarray  # 0 / 1
    gear: np.ndarray
    rpm: np.ndarray
    drs: np.ndarray  # 0 / 1
    car_distance: np.ndarray  # m, integrated from speed by FastF1 (diagnostic only)
    pos_t: np.ndarray
    x: np.ndarray  # m, the circuit's own frame
    y: np.ndarray
    z: np.ndarray


@dataclass(frozen=True)
class Capabilities:
    """What a reference source can support (checked before any metric is computed)."""

    brake: str = "binary"  # "binary" (on/off) or "pressure" (0-1)
    steering: bool = False
    wheel_slip: bool = False
    car_interval_s: float = 0.0  # median time between car-data samples
    car_max_gap_s: float = 0.0
    pos_interval_s: float = 0.0
    pos_max_gap_s: float = 0.0
    car_samples: int = 0
    pos_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Capabilities:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def not_comparable(self) -> list[str]:
        """Comparisons this source cannot support, in words."""
        out = []
        if self.brake == "binary":
            out.append("brake pressure, release and trail braking (the reference brake is on/off)")
        if not self.steering:
            out.append("steering (no steering data in the reference)")
        if not self.wheel_slip:
            out.append("lock-ups and wheelspin (no wheel-slip data in the reference)")
        out.append("coasting and throttle hesitation (too few samples to judge)")
        return out


@dataclass
class TelemetryChecks:
    rows: int
    end_s: float
    lap_time_s: float
    path_length_m: float
    integrated_length_m: float
    throttle_over_100: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _channel(tel: dict[str, Any], key: str, n: int | None) -> np.ndarray:
    v = tel.get(key)
    if not isinstance(v, list):
        raise ReferenceDataError(f"telemetry has no {key!r} channel")
    if n is not None and len(v) != n:
        raise ReferenceDataError(f"telemetry channel {key!r} has the wrong length")
    out = np.full(len(v), np.nan)
    for i, x in enumerate(v):
        f = _float(x)
        if f is not None:
            out[i] = f
    return out


def _whole(a: np.ndarray) -> np.ndarray:
    ok = np.isfinite(a)
    out = np.zeros(len(a), dtype=bool)
    out[ok] = np.abs(a[ok] - np.round(a[ok])) < 1e-6
    return out


def _first_per_time(t: np.ndarray, mask: np.ndarray) -> np.ndarray:
    idx = np.flatnonzero(mask)
    keep = np.concatenate([[True], np.diff(t[idx]) > 0]) if len(idx) else np.zeros(0, bool)
    out = np.zeros(len(t), dtype=bool)
    out[idx[keep]] = True
    return out


def _repeats_previous(mask: np.ndarray, cols: list[np.ndarray]) -> np.ndarray:
    """Rows of ``mask`` whose values equal those of the previous row of ``mask``."""
    out = np.zeros(len(mask), dtype=bool)
    idx = np.flatnonzero(mask)
    if len(idx) < 2:
        return out
    same = np.ones(len(idx) - 1, dtype=bool)
    for c in cols:
        same &= c[idx[1:]] == c[idx[:-1]]
    out[idx[1:][same]] = True
    return out


def _intervals(t: np.ndarray) -> tuple[float, float]:
    if len(t) < 2:
        return 0.0, 0.0
    dt = np.diff(t)
    return float(np.median(dt)), float(np.max(dt))


def parse_telemetry(
    tel: dict[str, Any], lap_time_s: float
) -> tuple[Samples, Capabilities, TelemetryChecks]:
    """Measured samples of one lap, with checks; raises :class:`ReferenceDataError`."""
    t = _channel(tel, "time", None)
    n = len(t)
    if n < 2 * MIN_SAMPLES:
        raise ReferenceDataError(f"telemetry has only {n} rows")
    ch = {
        k: _channel(tel, k, n) for k in ("speed", "rpm", "throttle", "brake", "gear", "x", "y", "z")
    }
    drs = _channel(tel, "drs", n) if "drs" in tel else np.zeros(n)
    dist = _channel(tel, "distance", n) if "distance" in tel else np.full(n, np.nan)
    if not np.all(np.isfinite(t)) or np.any(np.diff(t) < 0):
        raise ReferenceDataError("telemetry time is missing or goes backwards")

    car = _whole(ch["speed"]) & _whole(ch["rpm"]) & _whole(ch["throttle"])
    car &= np.isfinite(ch["brake"]) & np.isfinite(ch["gear"])
    pos = _whole(ch["x"]) & _whole(ch["y"]) & _whole(ch["z"])
    # keep one sample per instant and stream
    car = _first_per_time(t, car)
    pos = _first_per_time(t, pos)
    # a position row whose filled-in car values repeat the previous car sample exactly
    # (constant speed, rpm and throttle) is not a new car sample
    repeat = _repeats_previous(car, [ch[k] for k in ("speed", "rpm", "throttle", "gear", "brake")])
    car &= ~(pos & repeat)
    if car.sum() < MIN_SAMPLES or pos.sum() < MIN_SAMPLES:
        raise ReferenceDataError(
            f"telemetry format not recognised: found {int(car.sum())} car and "
            f"{int(pos.sum())} position samples (expected whole-number raw values)"
        )

    brake = ch["brake"][car]
    if not np.all(np.isin(brake, (0.0, 1.0))):
        raise ReferenceDataError("the brake channel is not on/off as expected")
    throttle_raw = ch["throttle"][car]
    over = int(np.count_nonzero(throttle_raw > 100))
    drs_car = drs[car]
    if np.nanmax(drs_car, initial=0) > 1:  # raw FastF1 DRS codes: 10, 12, 14 = open
        drs_car = (drs_car >= 10).astype(float)

    samples = Samples(
        car_t=t[car],
        speed=ch["speed"][car],
        throttle=np.clip(throttle_raw, 0, 100) / 100.0,
        brake=brake,
        gear=ch["gear"][car],
        rpm=ch["rpm"][car],
        drs=np.nan_to_num(drs_car),
        car_distance=dist[car],
        pos_t=t[pos],
        x=ch["x"][pos] * POSITION_SCALE,
        y=ch["y"][pos] * POSITION_SCALE,
        z=ch["z"][pos] * POSITION_SCALE,
    )

    end_s = float(t[-1])
    if abs(end_s - lap_time_s) > LAP_TIME_TOLERANCE_S:
        raise ReferenceDataError(
            f"telemetry ends at {end_s:.3f} s but the official lap time is {lap_time_s:.3f} s"
        )
    path = float(np.sum(np.hypot(np.diff(samples.x), np.diff(samples.y))))
    v = samples.speed / 3.6
    integrated = float(np.sum((v[1:] + v[:-1]) / 2 * np.diff(samples.car_t)))
    ratio = path / integrated if integrated > 0 else 0.0
    if not PATH_RATIO_RANGE[0] <= ratio <= PATH_RATIO_RANGE[1]:
        raise ReferenceDataError(
            f"position units not recognised: path {path:.0f} m vs {integrated:.0f} m "
            "travelled according to the speed"
        )
    car_iv, car_gap = _intervals(samples.car_t)
    pos_iv, pos_gap = _intervals(samples.pos_t)
    caps = Capabilities(
        brake="binary",
        steering=False,
        wheel_slip=False,
        car_interval_s=round(car_iv, 4),
        car_max_gap_s=round(car_gap, 4),
        pos_interval_s=round(pos_iv, 4),
        pos_max_gap_s=round(pos_gap, 4),
        car_samples=len(samples.car_t),
        pos_samples=len(samples.pos_t),
    )
    checks = TelemetryChecks(
        rows=n,
        end_s=round(end_s, 4),
        lap_time_s=round(lap_time_s, 4),
        path_length_m=round(path, 1),
        integrated_length_m=round(integrated, 1),
        throttle_over_100=over,
    )
    if over:
        checks.notes.append(f"{over} throttle samples above 100 % were clipped")
    return samples, caps, checks


# ------------------------------------------------------------------------ a stored lap


@dataclass
class RefLap:
    """One real (or synthetic) reference lap: catalogue data plus measured samples."""

    key: str
    provider: str
    synthetic: bool
    season: int
    event: str
    session: str
    session_part: str | None
    driver: str
    driver_number: str | None
    driver_name: str | None
    team: str | None
    lap_number: int
    lap_time_ms: int
    sectors_ms: tuple[int | None, int | None, int | None]
    compound: str | None
    tyre_life: int | None
    track_status: str | None
    capabilities: Capabilities
    checks: dict[str, Any]
    sources: list[dict[str, Any]]
    fetched_utc: str
    samples: Samples

    @property
    def lap_time_s(self) -> float:
        return self.lap_time_ms / 1000.0

    @property
    def label(self) -> str:
        part = f" {self.session_part}" if self.session_part else ""
        tag = " (synthetic)" if self.synthetic else ""
        return (
            f"{self.driver} · {self.season} {self.event} {self.session}{part} · "
            f"lap {self.lap_number}{tag}"
        )
