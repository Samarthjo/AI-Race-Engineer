"""Stage 4 benchmark against a "real F1" reference, on synthetic data with known answers.

The reference laps are written in the archive's own format (4 Hz car and position streams,
on/off brake, a different coordinate frame and start line), so everything from parsing to
findings runs as with real data. Two references: PRO drives the same car as you (only the
data fidelity differs) and FST drives a faster car (different physics: the normalised view
must remove that)."""

from __future__ import annotations

import numpy as np
import pytest

from pitwall.analysis.benchmark import Benchmark, benchmark, place_reference
from pitwall.analysis.trace import NotComparableError, load_trace
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.reference.service import (
    SYNTHETIC_EVENT,
    fetch_reference,
    make_archive,
    synthetic_archive_dir,
)
from pitwall.storage import db
from pitwall.synth.pro import write_archive
from pitwall.synth.session import LapPlan, SessionSynth
from pitwall.synth.track import DriverProfile, build_track
from pitwall.track.draft import draft_model
from tests.test_ingest import write_capture

BASE = DriverProfile()
SCENARIOS = {
    "base": BASE,  # lap index 1
    "twin": BASE,  # 2
    "brake_early": DriverProfile(brake_early_m={0: 30.0}),  # 3
    "slow_t2": DriverProfile(corner_speed_scale={1: 0.95}),  # 4
    "late_throttle": DriverProfile(throttle_delay_m={3: 40.0}),  # 5
    "lock_spin": DriverProfile(lockup_m={2: 6.0}, wheelspin_m={5: 10.0}),  # 6
}
INDEX = {name: i + 1 for i, name in enumerate(SCENARIOS)}


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    track = build_track()
    plans = [LapPlan(kind="out", driver=BASE)] + [LapPlan(driver=d) for d in SCENARIOS.values()]
    synth = SessionSynth(track=track, track_id=-1).run(plans, final_partial=0.2)
    data = tmp_path_factory.mktemp("bench")
    cap = write_capture(data / "raw", "20261115T090000Z", synth.records)
    conn = db.connect(data / DB_NAME)
    session = dict(
        db.get_session(conn, ingest_capture(cap, data, conn).sessions[0].session_key) or {}
    )
    write_archive(synthetic_archive_dir(data), track=track)
    ar = make_archive(data, 2025, synthetic=True)
    refs = {
        d: fetch_reference(conn, data, ar, SYNTHETIC_EVENT, "Qualifying", d) for d in ("PRO", "FST")
    }

    def trace(name: str):
        row = db.get_lap(conn, session["session_key"], INDEX[name])
        assert row is not None
        return load_trace(data, session, dict(row))

    model = draft_model(
        trace("base"), track_id=-1, track_name="Synthetic", synthetic=True, source={}
    )
    cache: dict[tuple[str, str], Benchmark] = {}

    def run(name: str, driver: str = "PRO") -> Benchmark:
        if (name, driver) not in cache:
            tr = trace(name)
            cache[(name, driver)] = benchmark(tr, place_reference(refs[driver], tr), model)
        return cache[(name, driver)]

    return {"refs": refs, "trace": trace, "model": model, "run": run, "track": track}


def _codes(b: Benchmark) -> list[tuple[str, str]]:
    return [(f.code, f.segment_id) for f in b.findings]


def test_reference_lap_time_is_kept_exactly(setup):
    b = setup["run"]("base")
    ref = setup["refs"]["PRO"]
    trace = b.placed.reference
    assert trace.time[0] == 0.0
    assert trace.time[-1] == pytest.approx(ref.lap_time_s, abs=1e-9)
    assert np.all(np.diff(trace.time) >= 0)
    lap = setup["trace"]("base")
    assert b.comparison.delta[-1] == pytest.approx(lap.lap_time_s - ref.lap_time_s, abs=1e-6)
    sectors = b.ref_sector_times()
    assert sectors is not None and sum(sectors) == pytest.approx(ref.lap_time_s, abs=0.002)
    assert b.placed.placement.registration.quality == "good"
    # the reference's start line was 12 m after the game's
    assert b.placed.placement.registration.start_offset_m == pytest.approx(12.0, abs=1.5)


@pytest.mark.parametrize("driver", ["PRO", "FST"])
def test_identical_driving_gives_no_findings(setup, driver):
    """Your lap against the same driving recorded at real-F1 fidelity (PRO), or against a
    faster car driven the same way (FST): no false findings."""
    for name in ("base", "twin"):
        b = setup["run"](name, driver)
        assert b.findings == [], (name, _codes(b))


def test_systematic_pace_is_separated(setup):
    same = setup["run"]("base", "PRO").systematic["time"]
    faster = setup["run"]("base", "FST").systematic["time"]
    for group in ("braking_corners", "straights"):
        assert abs(same[group]["pace_pct"]) < 0.2
        assert faster[group]["pace_pct"] > 0.5  # FST's car is faster everywhere
    assert setup["run"]("base", "FST").systematic["metrics"]["min_speed_kmh"]["offset"] < -0.5


@pytest.mark.parametrize("driver", ["PRO", "FST"])
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("brake_early", {("EARLY_BRAKE", "c1")}),
        ("slow_t2", {("LOW_MIN_SPEED", "c2")}),
        ("late_throttle", {("LATE_THROTTLE", "c4"), ("EXIT_DEFICIT", "c4")}),
    ],
)
def test_injected_mistakes_are_found_after_normalisation(setup, driver, name, expected):
    b = setup["run"](name, driver)
    found = _codes(b)
    assert len(found) == 1 and found[0] in expected, found
    f = b.findings[0]
    assert f.basis == "pro_normalised"
    assert f.time_impact_s >= 0.05
    assert f.measured["sigma"] is not None and f.measured["normalised"] is not None
    assert abs(f.measured["normalised"]) >= 2 * f.measured["sigma"]
    if name == "brake_early":
        assert f.measured["normalised"] == pytest.approx(-30.0, abs=12.0)


def test_comparisons_the_data_cannot_support_are_not_made(setup):
    b = setup["run"]("lock_spin")
    assert not {c for c, _ in _codes(b)} & {"LOCKUP", "WHEELSPIN", "SOFT_BRAKING", "COASTING"}
    text = " ".join(b.not_assessed)
    assert "lock-ups" in text and "steering" in text and "brake pressure" in text
    for r in b.corners:
        assert set(r.metrics) == {
            "brake_onset_m",
            "entry_speed_kmh",
            "min_speed_kmh",
            "throttle_pickup_m",
            "full_throttle_m",
            "exit_speed_kmh",
        }


def test_error_bars_are_measured_on_your_lap(setup):
    b = setup["run"]("base")
    c1 = b.error_bars["c1"]
    onset = c1["brake_onset_m"]
    assert abs(onset.bias) < 3.0 and 2.0 < onset.sd < 12.0 and onset.n >= 30
    assert c1["min_speed_kmh"].bias >= -0.5  # a sampled minimum is never far below the true one
    # every compared value carries an uncertainty and a confidence label
    for r in b.corners:
        for m in r.metrics.values():
            if m.delta is not None:
                assert m.sigma is not None and m.sigma > 0
                assert m.confidence in ("high", "medium", "within uncertainty")


def test_a_reference_from_another_track_is_refused(setup, tmp_path):
    other = build_track(
        layout=(
            (0.0, 0.0, 30.0),
            (1200.0, 200.0, 80.0),
            (1500.0, 1100.0, 40.0),
            (300.0, 1300.0, 120.0),
        )
    )
    write_archive(synthetic_archive_dir(tmp_path), track=other)
    conn = db.connect(tmp_path / DB_NAME)
    ref = fetch_reference(
        conn,
        tmp_path,
        make_archive(tmp_path, 2025, synthetic=True),
        SYNTHETIC_EVENT,
        "Qualifying",
        "PRO",
    )
    with pytest.raises(NotComparableError, match="cannot place"):
        place_reference(ref, setup["trace"]("base"))
