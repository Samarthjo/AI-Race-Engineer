"""Synthetic test track and driver model. For tests and the demo, never real telemetry.

Track: a convex polygon whose corners are rounded with circular arcs, so it is closed by
construction. The centreline is sampled every ``ds`` metres and scaled to a whole number
of metres (the game reports track length as an integer).

Driving: a quasi-steady-state lap simulation.
* Corner speed limit from lateral grip: ``v = sqrt(a_lat / curvature)``.
* Forward pass: accelerate with a speed-dependent limit (drag).
* Backward pass: brake no harder than the braking limit.

A :class:`DriverProfile` changes one corner at a time (earlier/gentler braking, lower corner
speed, later throttle), so tests know exactly where and why a lap loses time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

G = 9.81

# Vertices (counter-clockwise, metres) and the corner radius used at each vertex.
DEFAULT_LAYOUT: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0, 25.0),  # T1: slow hairpin-like corner after the main straight
    (1400.0, -80.0, 60.0),  # T2: medium-speed
    (1900.0, 300.0, 180.0),  # T3: fast
    (1700.0, 900.0, 35.0),  # T4: slow
    (900.0, 1150.0, 300.0),  # T5: flat out
    (200.0, 900.0, 45.0),  # T6: slow-medium onto the main straight
)


@dataclass(frozen=True)
class CornerSpan:
    index: int
    start_m: float  # arc start (turn-in)
    end_m: float  # arc end (exit)
    radius_m: float

    @property
    def apex_m(self) -> float:
        return (self.start_m + self.end_m) / 2


@dataclass
class Track:
    s: np.ndarray  # distance along the centreline, 0 .. length (inclusive), step ds
    x: np.ndarray  # world x (m)
    z: np.ndarray  # world z (m)
    curvature: np.ndarray  # 1/m, positive = left turn
    length: float  # whole metres
    corners: list[CornerSpan]
    ds: float


def build_track(
    layout: tuple[tuple[float, float, float], ...] = DEFAULT_LAYOUT,
    ds: float = 0.5,
    start_fraction: float = 0.5,
) -> Track:
    """Rounded convex polygon; the start/finish line sits ``start_fraction`` along the
    straight that leads into the first corner."""
    pts = np.asarray([(x, y) for x, y, _ in layout], dtype=np.float64)
    radii = [r for _, _, r in layout]
    n = len(pts)
    pieces: list[tuple[str, tuple]] = []  # ("line", (a, b)) or ("arc", (c, r, a0, dtheta))
    tangent_in: list[np.ndarray] = []
    tangent_out: list[np.ndarray] = []
    arcs: list[tuple[np.ndarray, float, float, float]] = []
    for i in range(n):
        p_prev, p, p_next = pts[i - 1], pts[i], pts[(i + 1) % n]
        u_in = (p - p_prev) / np.linalg.norm(p - p_prev)
        u_out = (p_next - p) / np.linalg.norm(p_next - p)
        cross = u_in[0] * u_out[1] - u_in[1] * u_out[0]
        if cross <= 0:
            raise ValueError(f"layout must be convex and counter-clockwise (vertex {i})")
        theta = math.atan2(cross, float(np.dot(u_in, u_out)))  # turn angle, > 0
        r = radii[i]
        t = r * math.tan(theta / 2)
        a = p - u_in * t
        b = p + u_out * t
        normal = np.array([-u_in[1], u_in[0]])  # left normal
        centre = a + normal * r
        a0 = math.atan2(a[1] - centre[1], a[0] - centre[0])
        tangent_in.append(a)
        tangent_out.append(b)
        arcs.append((centre, r, a0, theta))
    for i in range(n):
        start = tangent_out[i - 1]
        end = tangent_in[i]
        if np.dot(end - start, pts[i] - pts[i - 1]) <= 0:
            raise ValueError(f"corner radii too large: no straight before vertex {i}")
        pieces.append(("line", (start, end)))
        pieces.append(("arc", arcs[i]))

    xs: list[float] = []
    ys: list[float] = []
    ks: list[float] = []
    arc_ranges: list[tuple[float, float, float]] = []
    s_total = 0.0
    for kind, data in pieces:
        if kind == "line":
            a, b = data
            length = float(np.linalg.norm(b - a))
            m = max(1, round(length / ds))
            for j in range(m):
                f = j / m
                xs.append(a[0] + (b[0] - a[0]) * f)
                ys.append(a[1] + (b[1] - a[1]) * f)
                ks.append(0.0)
        else:
            centre, r, a0, theta = data
            length = r * theta
            m = max(1, round(length / ds))
            arc_ranges.append((s_total, s_total + length, r))
            for j in range(m):
                ang = a0 + theta * j / m
                xs.append(centre[0] + r * math.cos(ang))
                ys.append(centre[1] + r * math.sin(ang))
                ks.append(1.0 / r)
        s_total += length

    xy = np.column_stack([xs, ys])
    seg = np.linalg.norm(np.diff(np.vstack([xy, xy[:1]]), axis=0), axis=1)
    s_raw = np.concatenate([[0.0], np.cumsum(seg)])  # includes closing point
    total = s_raw[-1]

    # Rotate so s = 0 is on the straight before the first corner.
    first_straight_len = float(np.linalg.norm(tangent_in[0] - tangent_out[-1]))
    s0 = start_fraction * first_straight_len
    closed_xy = np.vstack([xy, xy[:1]])
    closed_k = np.concatenate([ks, ks[:1]])

    # Resample on a uniform grid of the rotated distance, scaled to whole metres.
    length = float(round(total))
    scale = length / total
    grid = np.arange(0.0, length + ds / 2, ds)
    src = (grid / scale + s0) % total
    x = np.interp(src, s_raw, closed_xy[:, 0]) * scale
    z = np.interp(src, s_raw, closed_xy[:, 1]) * scale
    k_idx = np.clip(np.searchsorted(s_raw, src, side="right") - 1, 0, len(closed_k) - 1)
    curvature = closed_k[k_idx] / scale

    corners = []
    for i, (a_start, a_end, r) in enumerate(arc_ranges):
        start = ((a_start - s0) % total) * scale
        end = start + (a_end - a_start) * scale
        corners.append(CornerSpan(i, start, end, r * scale))
    return Track(grid, x, z, curvature, length, corners, ds)


# ------------------------------------------------------------------------- driving


@dataclass(frozen=True)
class CarModel:
    v_max: float = 92.0  # m/s (331 km/h)
    a_lat: float = 3.0 * G  # lateral grip
    a_accel: float = 12.0  # m/s^2 at low speed
    v_top: float = 95.0  # speed where drag cancels drive
    a_brake: float = 40.0  # m/s^2 (about 4 g)


@dataclass(frozen=True)
class DriverProfile:
    """Per-corner deviations from the model's limit lap (keys are corner indices)."""

    brake_scale: dict[int, float] = field(default_factory=dict)  # <1: brakes earlier, softer
    corner_speed_scale: dict[int, float] = field(default_factory=dict)  # <1: slower in corner
    throttle_delay_m: dict[int, float] = field(default_factory=dict)  # metres of extra coasting
    grip_scale: float = 1.0


@dataclass
class LapProfile:
    """One simulated lap on the track grid."""

    track: Track
    speed: np.ndarray  # m/s
    time: np.ndarray  # s since the line, same grid as track.s
    throttle: np.ndarray
    brake: np.ndarray
    gear: np.ndarray
    rpm: np.ndarray
    steer: np.ndarray
    lap_time: float

    def at(self, d: np.ndarray | float) -> dict[str, np.ndarray]:
        s = self.track.s
        d = np.asarray(d, dtype=np.float64)
        idx = np.clip(np.searchsorted(s, d, side="right") - 1, 0, len(s) - 1)
        return {
            "speed": np.interp(d, s, self.speed),
            "throttle": np.interp(d, s, self.throttle),
            "brake": np.interp(d, s, self.brake),
            "steer": np.interp(d, s, self.steer),
            "gear": self.gear[idx],
            "rpm": np.interp(d, s, self.rpm),
            "x": np.interp(d, s, self.track.x),
            "z": np.interp(d, s, self.track.z),
        }

    def distance_at_time(self, t: np.ndarray | float) -> np.ndarray:
        return np.asarray(np.interp(t, self.time, self.track.s))

    def time_at(self, d: float) -> float:
        return float(np.interp(d, self.track.s, self.time))


GEAR_UP_SPEEDS = (0.0, 22.0, 33.0, 44.0, 55.0, 66.0, 77.0, 86.0)  # m/s where gear n+1 starts


def simulate_lap(
    track: Track, driver: DriverProfile | None = None, car: CarModel | None = None
) -> LapProfile:
    driver = driver or DriverProfile()
    car = car or CarModel()
    s, ds, k = track.s, track.ds, track.curvature
    n = len(s)

    with np.errstate(divide="ignore"):
        v_lim = np.where(
            k > 0, np.sqrt(car.a_lat * driver.grip_scale / np.maximum(k, 1e-12)), np.inf
        )
    v_lim = np.minimum(v_lim, car.v_max)
    brake_factor = np.ones(n)
    for c in track.corners:
        on = (s >= c.start_m) & (s <= c.end_m)
        if c.index in driver.corner_speed_scale:
            v_lim[on] *= driver.corner_speed_scale[c.index]
        if c.index in driver.brake_scale:
            zone = (s >= c.start_m - 500.0) & (s <= c.end_m)
            brake_factor[zone] = driver.brake_scale[c.index]
        if c.index in driver.throttle_delay_m:
            hold = (s > c.end_m) & (s <= c.end_m + driver.throttle_delay_m[c.index])
            v_exit = float(v_lim[on].min()) if on.any() else car.v_max
            v_lim[hold] = np.minimum(v_lim[hold], v_exit)

    # Two laps back to back make the start speed periodic.
    lim2 = np.concatenate([v_lim, v_lim])
    bf2 = np.concatenate([brake_factor, brake_factor])
    m = len(lim2)
    v = np.empty(m)
    v[0] = min(lim2[0], car.v_max)
    for i in range(m - 1):
        a = car.a_accel * max(0.0, 1.0 - (v[i] / car.v_top) ** 2)
        v[i + 1] = min(lim2[i + 1], math.sqrt(v[i] ** 2 + 2 * a * ds))
    for i in range(m - 2, -1, -1):
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * car.a_brake * bf2[i] * ds))
    speed = v[n : 2 * n].copy()  # second lap (periodic)
    speed[0] = v[n]  # continuity at the line

    dt = ds / ((speed[:-1] + speed[1:]) / 2)
    time = np.concatenate([[0.0], np.cumsum(dt)])
    lap_time = float(time[-1])

    dv2 = np.diff(speed**2) / (2 * ds)  # acceleration per segment
    accel = np.concatenate([dv2, dv2[-1:]])
    brake = np.clip(-accel / car.a_brake, 0.0, 1.0)
    brake[brake < 0.02] = 0.0
    full = accel > 0.05
    cruise = 0.35 + 0.65 * (speed / car.v_max) ** 2
    throttle = np.where(brake > 0, 0.0, np.where(full, 1.0, cruise))
    gear = np.searchsorted(np.asarray(GEAR_UP_SPEEDS), speed, side="right").astype(np.int8)
    lo = np.asarray(GEAR_UP_SPEEDS)[gear - 1]
    hi = np.asarray((*GEAR_UP_SPEEDS[1:], car.v_max + 1.0))[gear - 1]
    rpm = 4500.0 + (11_800.0 - 4500.0) * np.clip((speed - lo) / (hi - lo), 0, 1)
    steer = np.clip(-k / 0.045, -1.0, 1.0)  # left turns steer left (negative)
    return LapProfile(track, speed, time, throttle, brake, gear, rpm, steer, lap_time)
