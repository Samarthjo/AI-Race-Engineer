"""Placing a real-F1 lap on your track: game <-> real registration and the distance map.

Real position data is in the circuit's own frame; the game uses its own world frame (x, z).
For one circuit the two frames differ by a similarity transform (rotation, uniform scale,
translation, possibly a mirror image), as long as the game's track follows the real layout.

1. **Coarse fit.** Both paths are resampled by fraction of their length. Every cyclic shift
   (where the real lap starts relative to the game's line) and both mirror options are tried;
   the best least-squares similarity wins.
2. **Refinement (ICP).** Each real position sample is matched to the nearest point of your
   lap's path, searching only near where it is expected along the lap (so parallel or
   crossing parts of a circuit are never confused), and the transform is re-fitted on the
   matches, ignoring the worst 10 %, until it stops changing.
3. **Distance map.** Each real sample gets the game lap distance of its match (projected
   between grid points); distances are forced to increase along the lap.

The fit is reported with its sideways residuals (different racing lines add a few metres),
its scale (expected about 1) and the start-line offset. A poor fit refuses the comparison
instead of producing a misleading one.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

COARSE_POINTS = 512
WINDOW_FIRST_M = 80.0  # search window around the expected distance (first pass)
WINDOW_M = 30.0
TRIM = 0.90  # fit on the best 90 % of the matches
MAX_ITERATIONS = 30

# quality gates (initial values; to be tuned with real F1 25 laps)
SCALE_CHECK = (0.97, 1.03)
SCALE_POOR = (0.90, 1.10)
MEDIAN_CHECK_M, MEDIAN_POOR_M = 4.0, 10.0
P95_CHECK_M, P95_POOR_M = 12.0, 25.0
OFFSET_CHECK_M, OFFSET_POOR_M = 50.0, 200.0
LENGTH_CHECK = 0.02  # reference path vs your path length, relative


class RegistrationError(ValueError):
    """The real lap cannot be placed on this track."""


@dataclass
class Registration:
    reflected: bool
    rotation_deg: float
    scale: float
    tx: float
    tz: float
    rms_m: float
    median_m: float
    p95_m: float
    max_m: float
    start_offset_m: float  # your lap distance where the real lap starts (+ = after your line)
    length_ratio: float  # real path (in game metres) / your path
    iterations: int
    points: int
    quality: str  # "good" | "check" | "poor"
    notes: list[str] = field(default_factory=list)

    @property
    def _c(self) -> complex:
        a = math.radians(self.rotation_deg)
        return self.scale * complex(math.cos(a), math.sin(a))

    def transform(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Real-frame metres -> game world (x, z)."""
        p = np.asarray(x, dtype=np.float64) + 1j * np.asarray(y, dtype=np.float64)
        if self.reflected:
            p = np.conj(p)
        q = self._c * p + complex(self.tx, self.tz)
        return np.real(q), np.imag(q)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d


@dataclass
class Placement:
    """One real lap mapped onto your track."""

    registration: Registration
    pos_t: np.ndarray
    pos_d: np.ndarray  # unwrapped game lap distance of each position sample (increasing)
    lateral_m: np.ndarray  # sideways distance from your path
    gx: np.ndarray  # positions in the game frame (for the map)
    gz: np.ndarray

    def distance_at(self, t: np.ndarray) -> np.ndarray:
        """Unwrapped game distance at times ``t`` (linear; extrapolated at the ends)."""
        return interp_extrapolate(t, self.pos_t, self.pos_d)


def interp_extrapolate(t: np.ndarray, ts: np.ndarray, ds: np.ndarray, k: int = 4) -> np.ndarray:
    """Linear interpolation of ``ds(ts)``; beyond the ends, the mean slope of the last ``k``
    intervals continues the line (the car does not stop at the first or last sample)."""
    t = np.asarray(t, dtype=np.float64)
    out = np.interp(t, ts, ds)
    k = max(1, min(k, len(ts) - 1))
    lo = t < ts[0]
    if lo.any():
        v = (ds[k] - ds[0]) / (ts[k] - ts[0])
        out[lo] = ds[0] - v * (ts[0] - t[lo])
    hi = t > ts[-1]
    if hi.any():
        v = (ds[-1] - ds[-1 - k]) / (ts[-1] - ts[-1 - k])
        out[hi] = ds[-1] + v * (t[hi] - ts[-1])
    return out


# --------------------------------------------------------------------------- helpers


def _closed_resample(z: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Points ``n`` equal arc-length steps apart around a closed path (complex coords),
    plus the arc length of every input point and the closed length."""
    seg = np.abs(np.diff(np.concatenate([z, z[:1]])))
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    zc = np.concatenate([z, z[:1]])
    u = np.arange(n) * total / n
    return np.interp(u, s, np.real(zc)) + 1j * np.interp(u, s, np.imag(zc)), s[:-1], total


def _similarity(src: np.ndarray, dst: np.ndarray) -> tuple[complex, complex]:
    """Least-squares dst ~ c * src + t (complex c: rotation and scale)."""
    ms, md = src.mean(), dst.mean()
    a, b = src - ms, dst - md
    c = complex(np.sum(np.conj(a) * b) / np.sum(np.abs(a) ** 2))
    return c, complex(md - c * ms)


def _coarse(
    game: np.ndarray, real: np.ndarray, length: float
) -> tuple[bool, complex, complex, float, float]:
    """Best mirror option, similarity and start offset by exhaustive cyclic-shift search."""
    n = COARSE_POINTS
    g, _, _ = _closed_resample(game, n)
    gc = g - g.mean()
    idx = (np.arange(n)[None, :] + np.arange(n)[:, None]) % n  # idx[k, i] = i + k
    best: tuple[float, bool, int, complex, complex] | None = None
    real_total = 0.0
    for reflected in (False, True):
        r_in = np.conj(real) if reflected else real
        r, _, real_total = _closed_resample(r_in, n)
        rc = r - r.mean()
        cross = np.sum(np.conj(rc[idx]) * gc[None, :], axis=1)
        norm = float(np.sum(np.abs(rc) ** 2))
        res = float(np.sum(np.abs(gc) ** 2)) - np.abs(cross) ** 2 / norm
        k = int(np.argmin(res))
        if best is None or res[k] < best[0]:
            c = complex(cross[k] / norm)
            best = (float(res[k]), reflected, k, c, complex(g.mean() - c * r.mean()))
    assert best is not None
    _, reflected, k, c, t = best
    # real fraction u maps to game fraction u - k/n: the real lap's first sample is at
    start = ((-k / n) % 1.0) * length
    if start > length / 2:
        start -= length
    return reflected, c, t, start, real_total


def _project(
    p: np.ndarray, expect: np.ndarray, d3: np.ndarray, z3: np.ndarray, step: float, window: float
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest point of the (tripled) game path near the expected distance of each sample:
    unwrapped distance along the path and sideways distance."""
    w = max(1, round(window / step))
    offs = np.arange(-w, w + 1)
    centre = np.clip(np.round((expect - d3[0]) / step).astype(int), w + 1, len(d3) - w - 2)
    cand = centre[:, None] + offs[None, :]
    dist = np.abs(z3[cand] - p[:, None])
    m = cand[np.arange(len(p)), np.argmin(dist, axis=1)]
    best_d = np.empty(len(p))
    best_r = np.full(len(p), np.inf)
    for a, b in ((m - 1, m), (m, m + 1)):
        seg = z3[b] - z3[a]
        f = np.clip(np.real((p - z3[a]) * np.conj(seg)) / np.maximum(np.abs(seg) ** 2, 1e-12), 0, 1)
        q = z3[a] + f * seg
        r = np.abs(p - q)
        better = r < best_r
        best_r[better] = r[better]
        best_d[better] = (d3[a] + f * (d3[b] - d3[a]))[better]
    return best_d, best_r


# --------------------------------------------------------------------------- main


def register(
    game_d: np.ndarray,
    game_x: np.ndarray,
    game_z: np.ndarray,
    pos_t: np.ndarray,
    pos_x: np.ndarray,
    pos_y: np.ndarray,
) -> Placement:
    """Fit the real lap's positions (metres, own frame) to your lap's path.

    ``game_d`` is your lap's distance grid (0 .. track length, equal steps) and
    ``game_x``/``game_z`` the world position on it.
    """
    ok = np.isfinite(game_x) & np.isfinite(game_z)
    if ok.sum() < len(game_d) * 0.99:
        raise RegistrationError("your lap has no world positions to register against")
    if len(pos_t) < 20:
        raise RegistrationError("too few reference position samples")
    length = float(game_d[-1])
    step = float(game_d[1] - game_d[0])
    gpath = game_x + 1j * game_z
    real = np.asarray(pos_x, dtype=np.float64) + 1j * np.asarray(pos_y, dtype=np.float64)

    reflected, c, t, start, real_total = _coarse(gpath[:-1], real, length)
    # expected unwrapped distance of each sample from its arc-length fraction
    _, s_real, closed = _closed_resample(np.conj(real) if reflected else real, 8)
    expect = start + s_real / closed * length

    # game path over three laps, so unwrapped distances below 0 or above L can be looked up
    base_d, base_z = game_d[:-1], gpath[:-1]
    d3 = np.concatenate([base_d - length, base_d, base_d + length])
    z3 = np.concatenate([base_z, base_z, base_z])

    src = np.conj(real) if reflected else real
    window = WINDOW_FIRST_M
    iterations = 0
    while iterations < MAX_ITERATIONS:
        iterations += 1
        p = c * src + t
        dist_d, resid = _project(p, expect, d3, z3, step, window)
        keep = resid <= np.quantile(resid, TRIM)
        q = np.interp(dist_d, d3, z3.real) + 1j * np.interp(dist_d, d3, z3.imag)
        c_new, t_new = _similarity(src[keep], q[keep])
        moved = abs(c_new - c) * 1e3 + abs(t_new - t)
        c, t, expect, window = c_new, t_new, dist_d, WINDOW_M
        if moved < 1e-3:
            break

    p = c * src + t
    dist_d, resid = _project(p, expect, d3, z3, step, WINDOW_M)
    pos_d = np.maximum.accumulate(dist_d)
    times = np.asarray(pos_t, dtype=np.float64)
    start_offset = float(interp_extrapolate(np.array([0.0]), times, pos_d)[0])
    scale = abs(c)
    real_len = real_total * scale
    game_len = float(np.sum(np.abs(np.diff(gpath))))
    reg = Registration(
        reflected=reflected,
        rotation_deg=math.degrees(math.atan2(c.imag, c.real)),
        scale=scale,
        tx=t.real,
        tz=t.imag,
        rms_m=float(np.sqrt(np.mean(resid**2))),
        median_m=float(np.median(resid)),
        p95_m=float(np.quantile(resid, 0.95)),
        max_m=float(np.max(resid)),
        start_offset_m=start_offset,
        length_ratio=real_len / game_len if game_len > 0 else float("nan"),
        iterations=iterations,
        points=len(resid),
        quality="good",
    )
    _grade(reg)
    return Placement(reg, times, pos_d, resid, p.real, p.imag)


def _grade(reg: Registration) -> None:
    poor: list[str] = []
    check: list[str] = []
    if not SCALE_POOR[0] <= reg.scale <= SCALE_POOR[1]:
        poor.append(f"scale {reg.scale:.3f}: the circuits do not match in size")
    elif not SCALE_CHECK[0] <= reg.scale <= SCALE_CHECK[1]:
        check.append(f"scale {reg.scale:.3f} (expected close to 1)")
    if reg.median_m > MEDIAN_POOR_M or reg.p95_m > P95_POOR_M:
        poor.append(
            f"positions sit {reg.median_m:.1f} m (median) / {reg.p95_m:.1f} m (95 %) from "
            "your path: the layouts do not match"
        )
    elif reg.median_m > MEDIAN_CHECK_M or reg.p95_m > P95_CHECK_M:
        check.append(
            f"positions sit {reg.median_m:.1f} m (median) / {reg.p95_m:.1f} m (95 %) from your path"
        )
    if abs(reg.start_offset_m) > OFFSET_POOR_M:
        poor.append(f"the real start line is {reg.start_offset_m:+.0f} m from yours")
    elif abs(reg.start_offset_m) > OFFSET_CHECK_M:
        check.append(f"the real start line is {reg.start_offset_m:+.0f} m from yours")
    if abs(reg.length_ratio - 1) > LENGTH_CHECK:
        check.append(f"the real lap is {100 * (reg.length_ratio - 1):+.1f} % longer than yours")
    reg.notes = poor + check
    reg.quality = "poor" if poor else ("check" if check else "good")


def map_points(
    reg: Registration,
    game_d: np.ndarray,
    game_x: np.ndarray,
    game_z: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Lap distance (0..L) and sideways distance of real-frame points (e.g. corner markers),
    searching the whole lap."""
    gx, gz = reg.transform(xs, ys)
    p = gx + 1j * gz
    path = game_x + 1j * game_z
    dist = np.abs(path[None, :] - p[:, None])
    i = np.argmin(dist, axis=1)
    return game_d[i], dist[np.arange(len(p)), i]
