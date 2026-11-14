"""Stage 2 analysis against laps whose exact timing is known (synthetic driver model)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from pitwall.analysis.compare import (
    compare,
    reference_from_trace,
    resolve_reference,
    stitch_theoretical_best,
)
from pitwall.analysis.metrics import DEFINITIONS, lap_metrics
from pitwall.analysis.trace import NotComparableError, build_trace, load_trace
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.storage import db
from pitwall.synth.session import LapPlan, SessionSynth, SyntheticSession
from pitwall.synth.track import DriverProfile, build_track
from tests.test_ingest import write_capture

BASE = DriverProfile()
LATE_THROTTLE_T4 = DriverProfile(throttle_delay_m={3: 30.0})
SOFT_BRAKE_T1 = DriverProfile(brake_scale={0: 0.85})
SLOW_T6 = DriverProfile(corner_speed_scale={5: 0.95})


@pytest.fixture(scope="module")
def track():
    return build_track()


@pytest.fixture(scope="module")
def ingested(tmp_path_factory, track):
    plans = [
        LapPlan(kind="out", driver=BASE),
        LapPlan(driver=BASE),  # lap index 1: the reference (fastest, usable)
        LapPlan(driver=LATE_THROTTLE_T4),  # 2
        LapPlan(driver=SOFT_BRAKE_T1),  # 3
        LapPlan(driver=SLOW_T6),  # 4
    ]
    synth = SessionSynth(track=track, track_id=11).run(plans, final_partial=0.2)
    data = tmp_path_factory.mktemp("data")
    cap = write_capture(data / "raw", "20261114T170000Z", synth.records)
    conn = db.connect(data / DB_NAME)
    res = ingest_capture(cap, data, conn)
    session = dict(db.get_session(conn, res.sessions[0].session_key) or {})
    return data, conn, session, synth


def _lap(conn, session_key: str, lap_index: int) -> dict:
    row = db.get_lap(conn, session_key, lap_index)
    assert row is not None
    return dict(row)


def _trace(ingested, lap_index: int):
    data, conn, session, _ = ingested
    return load_trace(data, session, _lap(conn, session["session_key"], lap_index))


def test_lap_against_itself_is_zero(ingested):
    tr = _trace(ingested, 1)
    c = compare(tr, reference_from_trace(tr, "lap", "itself"))
    assert np.all(c.delta == 0.0)
    assert c.largest_losses == [] and c.largest_gains == []


def test_delta_at_the_line_is_the_lap_time_difference(ingested):
    ref = _trace(ingested, 1)
    for idx in (2, 3, 4):
        lap = _trace(ingested, idx)
        c = compare(lap, reference_from_trace(ref, "lap", "base"))
        assert c.delta[0] == 0.0
        assert c.delta[-1] == pytest.approx(lap.lap_time_s - ref.lap_time_s, abs=1e-9)


def test_delta_matches_the_true_timing_everywhere(ingested):
    """Measured delta vs the generator's exact time-at-distance: within 5 ms at every metre."""
    _, _, _, synth = ingested
    ref, lap = _trace(ingested, 1), _trace(ingested, 2)
    c = compare(lap, reference_from_trace(ref, "lap", "base"))
    p_lap, p_ref = synth.laps[2].profile, synth.laps[1].profile
    assert p_lap is not None and p_ref is not None
    truth = np.interp(c.distance, p_lap.track.s, p_lap.time) - np.interp(
        c.distance, p_ref.track.s, p_ref.time
    )
    assert np.max(np.abs(c.delta - truth)) < 0.005


def test_loss_is_found_where_it_was_injected(ingested, track):
    ref = _trace(ingested, 1)
    t4 = track.corners[3]
    c = compare(_trace(ingested, 2), reference_from_trace(ref, "lap", "base"))
    worst = c.largest_losses[0]
    assert worst["start_m"] <= t4.end_m + 30 and worst["end_m"] >= t4.end_m
    assert worst["seconds"] > 0.3
    # nothing is lost before T4
    before = c.distance < t4.start_m
    assert np.max(np.abs(c.delta[before])) < 0.002

    t1 = track.corners[0]
    c1 = compare(_trace(ingested, 3), reference_from_trace(ref, "lap", "base"))
    assert c1.largest_losses[0]["start_m"] < t1.end_m < c1.largest_losses[0]["end_m"] + 1


def test_trace_sector_times_match_official(ingested):
    _, _, session, _ = ingested
    tr = _trace(ingested, 2)
    s2, s3 = session["sector2_start_m"], session["sector3_start_m"]
    measured = (tr.time_at(s2), tr.time_at(s3) - tr.time_at(s2), tr.lap_time_s - tr.time_at(s3))
    for m, official in zip(measured, tr.official_sectors_ms, strict=True):
        assert official is not None
        assert m * 1000 == pytest.approx(official, abs=2.0)


def test_sector_deltas_use_official_times(ingested):
    ref, lap = _trace(ingested, 1), _trace(ingested, 4)  # slower in T6, sector 3
    c = compare(lap, reference_from_trace(ref, "lap", "base"))
    assert [s["delta_source"] for s in c.sectors] == ["official"] * 3
    assert c.sectors[2]["delta_s"] > 0.05  # T6 is in sector 3
    assert c.sectors[1]["delta_s"] == 0  # T2-T4 untouched
    # T6 leads onto the main straight across the line, so the (periodic) lap also starts
    # slightly slower: a small, real sector-1 loss.
    assert 0 <= c.sectors[0]["delta_s"] < 0.02


def test_out_lap_and_incomplete_lap_are_not_comparable(ingested):
    data, conn, session, _ = ingested
    laps = db.list_laps(conn, session["session_key"])
    with pytest.raises(NotComparableError, match="out laps"):
        load_trace(data, session, dict(laps[0]))
    with pytest.raises(NotComparableError, match="not complete"):
        load_trace(data, session, dict(laps[-1]))


def test_references(ingested):
    data, conn, session, _ = ingested
    best = resolve_reference(conn, data, session, "session_best")
    assert best.sources[0]["lap_index"] == 1
    assert best.official_time_ms == _lap(conn, session["session_key"], 1)["lap_time_ms"]

    pb = resolve_reference(conn, data, session, "personal_best")
    assert pb.sources[0]["lap_index"] == 1

    other = resolve_reference(
        conn, data, session, "lap", ref_session_key=session["session_key"], ref_lap_index=3
    )
    assert other.sources[0]["lap_index"] == 3
    with pytest.raises(NotComparableError):
        resolve_reference(conn, data, session, "nonsense")


def test_theoretical_best_is_stitched_from_best_sectors(ingested):
    data, conn, session, _ = ingested
    theo = resolve_reference(conn, data, session, "theoretical_best")
    rows = db.list_laps(conn, session["session_key"])
    usable = [r for r in rows if r["usable"]]
    best = [min(r[f"s{i}_ms"] for r in usable) for i in (1, 2, 3)]
    assert theo.official_time_ms == sum(best)
    assert theo.lap_time_s * 1000 == pytest.approx(sum(best), abs=3.0)
    assert np.all(np.diff(theo.time) > 0), "stitched time must keep increasing"
    assert {s["sectors"][0] for s in theo.sources} == {1, 2, 3}


def test_stitching_three_identical_laps_gives_that_lap(ingested):
    _, _, session, _ = ingested
    tr = _trace(ingested, 1)
    s1, s2, s3 = (int(x or 0) for x in tr.official_sectors_ms)
    theo = stitch_theoretical_best(
        (tr, tr, tr), (s1, s2, s3), (session["sector2_start_m"], session["sector3_start_m"])
    )
    assert np.allclose(theo.time, tr.time, atol=1e-9)


def test_build_trace_rejects_lap_starting_mid_track():
    t = np.arange(100) / 60
    table = pa.table(
        {
            "lap_distance": 2000 + t * 80,
            "current_lap_time_in_ms": np.round(t * 1000),
            "speed": np.full(100, 288.0),
            "throttle": np.ones(100),
            "brake": np.zeros(100),
            "steer": np.zeros(100),
            "gear": np.full(100, 7),
            "engine_rpm": np.full(100, 11000.0),
            "drs": np.zeros(100),
            "world_position_x": t,
            "world_position_z": t,
        }
    )
    with pytest.raises(NotComparableError, match="after the line"):
        build_trace(
            table,
            session_key="s",
            lap_index=0,
            lap_number=1,
            lap_time_ms=60000,
            track_length_m=5000,
        )


def test_lap_metrics_definitions_and_values():
    # 10 frames of 0.1 s: 6 full throttle, 2 braking, 2 coasting; lap time 1.0 s
    n = 10
    table = pa.table(
        {
            "current_lap_time_in_ms": np.arange(n) * 100,
            "speed": np.array([100, 150, 200, 250, 300, 310, 200, 150, 140, 130.0]),
            "throttle": np.array([1, 1, 1, 1, 1, 1, 0, 0, 0.02, 0.0]),
            "brake": np.array([0, 0, 0, 0, 0, 0, 0.9, 0.8, 0, 0.0]),
            "gear": np.array([3, 4, 5, 6, 7, 8, 6, 5, 5, 5]),
            "engine_rpm": np.full(n, 11000.0),
            "drs": np.zeros(n),
            "fuel_in_tank": np.linspace(10, 9.5, n),
        }
    )
    m = lap_metrics(table, lap_time_s=1.0, track_length_m=100)
    assert set(m) == set(DEFINITIONS)
    assert m["full_throttle_pct"] == 60.0
    assert m["braking_pct"] == 20.0
    assert m["coasting_pct"] == 20.0
    assert m["top_speed_kmh"] == 310.0
    assert m["avg_speed_kmh"] == 360.0
    assert m["gear_changes"] == 7  # 3>4>5>6>7>8>6>5
    assert m["fuel_used_kg"] == 0.5
    assert m["ers_deployed_mj"] is None  # channel missing -> no number, never a guess


def test_synthetic_track_and_driver_model(track):
    assert np.hypot(track.x[0] - track.x[-1], track.z[0] - track.z[-1]) < 0.01
    assert float(track.length).is_integer()
    assert len(track.corners) == 6
    from pitwall.synth.track import simulate_lap

    base = simulate_lap(track)
    assert np.all(np.diff(base.time) > 0)
    soft = simulate_lap(track, SOFT_BRAKE_T1)
    assert soft.lap_time > base.lap_time

    def brake_start(p) -> float:
        c = track.corners[0]
        zone = (track.s > c.start_m - 600) & (track.s < c.start_m) & (p.brake > 0.05)
        return float(track.s[np.flatnonzero(zone)[0]])

    assert brake_start(soft) < brake_start(base) - 10  # brakes earlier
    slow = simulate_lap(track, SLOW_T6)
    t6 = track.corners[5]
    on = (track.s >= t6.start_m) & (track.s <= t6.end_m)
    assert slow.speed[on].min() < base.speed[on].min() * 0.96


def test_personal_best_never_mixes_synthetic_and_real(tmp_path: Path, track):
    data = tmp_path / "d"
    conn = db.connect(data / DB_NAME)
    real = SessionSynth(track=track, track_id=11, session_uid=1).run(
        [LapPlan(driver=SLOW_T6), LapPlan(driver=SLOW_T6)], final_partial=None
    )
    fake: SyntheticSession = SessionSynth(track=track, track_id=11, session_uid=2).run(
        [LapPlan(driver=BASE), LapPlan(driver=BASE)], final_partial=None
    )
    ingest_capture(write_capture(data / "raw", "REAL", real.records), data, conn)
    fake_dir = write_capture(data / "raw", "FAKE", fake.records)
    (fake_dir / "capture.json").write_text('{"synthetic": true, "label": "demo"}')
    ingest_capture(fake_dir, data, conn)
    real_session = dict(db.find_sessions(conn, "REAL/")[0])
    pb = resolve_reference(conn, data, real_session, "personal_best")
    assert pb.sources[0]["session_key"].startswith("REAL/")
    fake_session = dict(db.find_sessions(conn, "FAKE/")[0])
    assert fake_session["synthetic"] == 1
    assert fake_session["track_name"] == "Synthetic test track"
