"""Stage 4: placing a real-F1 lap on your track (registration and distance map), checked on
the synthetic track with known transforms and start-line offsets."""

from __future__ import annotations

import numpy as np
import pytest

from pitwall.reference.laps import Samples, parse_telemetry
from pitwall.synth.pro import FrameTransform, lap_telemetry
from pitwall.synth.track import build_track, simulate_lap
from pitwall.track.registration import map_points, register

TRACK = build_track()
PROFILE = simulate_lap(TRACK)


def _game_path(step: float = 1.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    d = np.arange(0.0, TRACK.length + step / 2, step)
    d[-1] = TRACK.length
    return d, np.interp(d, TRACK.s, TRACK.x), np.interp(d, TRACK.s, TRACK.z)


def _real_lap(transform: FrameTransform, offset: float, seed: int = 3) -> Samples:
    tel = lap_telemetry(PROFILE, transform, offset, np.random.default_rng(seed))
    samples, _, _ = parse_telemetry(tel, round(PROFILE.lap_time, 3))
    return samples


CASES = [
    (FrameTransform(), 12.0),
    (FrameTransform(reflected=False, rotation_deg=120.0, tx=-900.0, ty=3000.0), -35.0),
    (FrameTransform(reflected=True, rotation_deg=-75.0, scale=0.98), 0.0),
]


@pytest.mark.parametrize(("transform", "offset"), CASES)
def test_recovers_the_transform_and_the_start_line(transform, offset):
    d, x, z = _game_path()
    s = _real_lap(transform, offset)
    placement = register(d, x, z, s.pos_t, s.x, s.y)
    reg = placement.registration
    assert reg.quality == "good", reg.notes
    assert reg.scale == pytest.approx(1 / transform.scale, rel=2e-3)
    assert reg.start_offset_m == pytest.approx(offset, abs=1.5)
    assert reg.median_m < 0.5 and reg.p95_m < 1.0
    # round trip: real positions back into the game frame land on the game's positions
    t0 = PROFILE.time_at(offset % TRACK.length)
    true_d = PROFILE.distance_at_time((t0 + s.pos_t) % PROFILE.lap_time)
    gx, gz = reg.transform(s.x, s.y)
    err = np.hypot(
        gx - np.interp(true_d, TRACK.s, TRACK.x), gz - np.interp(true_d, TRACK.s, TRACK.z)
    )
    assert np.max(err) < 1.0
    # distance map: increasing, and each sample at its true lap distance (unwrapped)
    assert np.all(np.diff(placement.pos_d) >= 0)
    wrapped = np.mod(placement.pos_d, TRACK.length)
    along = np.abs((wrapped - true_d + TRACK.length / 2) % TRACK.length - TRACK.length / 2)
    assert np.max(along) < 1.0
    assert placement.pos_d[-1] - placement.pos_d[0] == pytest.approx(TRACK.length, abs=40)


def test_corner_markers_land_on_the_right_corners():
    d, x, z = _game_path()
    transform = FrameTransform()
    s = _real_lap(transform, 12.0)
    reg = register(d, x, z, s.pos_t, s.x, s.y).registration
    apex = np.array([c.apex_m for c in TRACK.corners])
    ax, ay = transform.game_to_real(
        np.interp(apex, TRACK.s, TRACK.x), np.interp(apex, TRACK.s, TRACK.z)
    )
    mapped, side = map_points(reg, d, x, z, ax, ay)
    assert np.max(np.abs(mapped - apex)) <= 2.0
    assert np.max(side) < 1.0


def test_a_different_layout_is_refused():
    other = build_track(
        layout=(
            (0.0, 0.0, 30.0),
            (1200.0, 200.0, 80.0),
            (1500.0, 1100.0, 40.0),
            (300.0, 1300.0, 120.0),
        )
    )
    tel = lap_telemetry(simulate_lap(other), FrameTransform(), 0.0, np.random.default_rng(5))
    s, _, _ = parse_telemetry(tel, round(simulate_lap(other).lap_time, 3))
    d, x, z = _game_path()
    reg = register(d, x, z, s.pos_t, s.x, s.y).registration
    assert reg.quality == "poor"
    assert reg.notes


def test_needs_positions():
    from pitwall.track.registration import RegistrationError

    d, x, z = _game_path()
    s = _real_lap(FrameTransform(), 0.0)
    with pytest.raises(RegistrationError):
        register(d, x * np.nan, z, s.pos_t, s.x, s.y)
    with pytest.raises(RegistrationError):
        register(d, x, z, s.pos_t[:5], s.x[:5], s.y[:5])
