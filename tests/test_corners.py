"""Stage 3: track model drafting, corner metrics, time accounting and findings, checked
against synthetic laps whose mistakes (and exact timing) are known."""

from __future__ import annotations

import numpy as np
import pytest

from pitwall.analysis.compare import compare, reference_from_trace
from pitwall.analysis.consistency import session_consistency
from pitwall.analysis.corners import analyse_corners
from pitwall.analysis.findings import lap_findings
from pitwall.analysis.trace import load_trace
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.storage import db
from pitwall.synth.session import LapPlan, SessionSynth
from pitwall.synth.track import DriverProfile, build_track
from pitwall.track.draft import draft_model
from tests.test_ingest import write_capture

BASE = DriverProfile()
SCENARIOS = {
    "base": BASE,  # lap index 1 (reference)
    "brake_early": DriverProfile(brake_early_m={0: 15.0}),  # 2
    "late_throttle": DriverProfile(throttle_delay_m={3: 30.0}),  # 3
    "slow_t2": DriverProfile(corner_speed_scale={1: 0.95}),  # 4
    "soft_brake": DriverProfile(brake_scale={0: 0.85}),  # 5
    "lock_spin": DriverProfile(lockup_m={2: 6.0}, wheelspin_m={5: 10.0}),  # 6
    "twin": BASE,  # 7: identical to the reference
    "near": DriverProfile(grip_scale=0.999),  # 8: almost identical
}
INDEX = {name: i + 1 for i, name in enumerate(SCENARIOS)}


@pytest.fixture(scope="module")
def track():
    return build_track()


@pytest.fixture(scope="module")
def setup(tmp_path_factory, track):
    plans = [LapPlan(kind="out", driver=BASE)] + [LapPlan(driver=d) for d in SCENARIOS.values()]
    synth = SessionSynth(track=track, track_id=11).run(plans, final_partial=0.2)
    data = tmp_path_factory.mktemp("data")
    cap = write_capture(data / "raw", "20261114T180000Z", synth.records)
    conn = db.connect(data / DB_NAME)
    res = ingest_capture(cap, data, conn)
    session = dict(db.get_session(conn, res.sessions[0].session_key) or {})

    def trace(i: int):
        row = db.get_lap(conn, session["session_key"], i)
        assert row is not None
        return load_trace(data, session, dict(row))

    ref = trace(INDEX["base"])
    model = draft_model(ref, track_id=11, track_name="Monza", synthetic=False, source={})
    reference = reference_from_trace(ref, "lap", "base")

    def analyse(name: str):
        return analyse_corners(compare(trace(INDEX[name]), reference), model)

    return {
        "data": data,
        "conn": conn,
        "session": session,
        "synth": synth,
        "model": model,
        "trace": trace,
        "analyse": analyse,
    }


# ------------------------------------------------------------------- track model


def test_draft_finds_every_corner(setup, track):
    model = setup["model"]
    assert model.validate() == []
    corners = model.corners
    assert len(corners) == len(track.corners) == 6
    for seg, true in zip(corners, track.corners, strict=True):
        assert seg.start_m < true.start_m and seg.end_m > true.end_m, seg
    kinds = [c.kind for c in corners]
    assert kinds == ["corner", "corner", "corner", "corner", "flat", "corner"]
    # straights fill the rest: the whole lap is covered, in order, without gaps
    assert model.segments[0].start_m == 0 and model.segments[-1].end_m == track.length
    assert [s.kind == "straight" for s in model.segments[::2]] == [True] * 7


def test_known_names_only_when_counts_match(setup):
    model = setup["model"]  # synthetic track: 5 braking corners, Monza has 6
    assert [c.name for c in model.corners][:2] == ["C1", "C2"]
    from pitwall.track.draft import _name
    from pitwall.track.model import Segment

    segs = [Segment("", "", "straight", 0, 10)]
    for i in range(6):
        segs += [
            Segment(f"c{i + 1}", f"C{i + 1}", "corner", 10 + i * 20, 20 + i * 20),
            Segment("", "", "straight", 20 + i * 20, 30 + i * 20),
        ]
    _name(segs, 11)
    assert segs[1].name == "Variante del Rettifilo (T1-T2)"
    assert segs[-2].name == "Curva Alboreto (T11)"
    assert segs[-1].name == "Straight to the line"


# --------------------------------------------------------------- time accounting


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_segments_add_up_to_the_lap_delta(setup, name):
    a = setup["analyse"](name)
    assert a.accounted_s() == pytest.approx(a.lap_delta_s, abs=1e-9)
    for r in a.corners:
        assert sum(r.phases.values()) == pytest.approx(r.loss_s, abs=1e-9)


def test_exit_carry_goes_to_the_corner(setup):
    a = setup["analyse"]("late_throttle")
    c4 = next(r for r in a.segments if r.segment.id == "c4")
    straight = next(r for r in a.segments if r.segment.id == c4.carried_to)
    assert c4.carried_s > 0.3
    assert abs(straight.total_s) < 0.01  # the straight itself was driven like the reference
    assert c4.total_s == pytest.approx(a.lap_delta_s, abs=0.03)


# ------------------------------------------------------------- detector accuracy


def _truth_onset(profile, window_start: float, corner, threshold=0.10):
    """First point with brake >= threshold between the segment start and the corner exit."""
    s = profile.track.s
    zone = np.flatnonzero((s >= window_start) & (s <= corner.end_m) & (profile.brake >= threshold))
    return float(s[zone[0]]) if len(zone) else None


def test_brake_onset_and_min_speed_match_ground_truth(setup, track):
    synth = setup["synth"]
    for name in ("base", "brake_early", "soft_brake", "slow_t2"):
        a = setup["analyse"](name)
        profile = synth.laps[INDEX[name]].profile
        for r, corner in zip(a.corners, track.corners, strict=True):
            assert r.lap is not None
            truth = _truth_onset(profile, r.segment.start_m, corner)
            if truth is None:
                assert r.lap.brake_onset_m is None
                continue
            assert r.lap.brake_onset_m is not None
            assert abs(r.lap.brake_onset_m - truth) <= 3.0, (name, corner.index)
            on = (profile.track.s >= r.segment.start_m) & (profile.track.s <= r.segment.end_m)
            true_min = float(profile.speed[on].min() * 3.6)
            assert abs(r.lap.min_speed_kmh - true_min) <= 1.5, (name, corner.index)


# ------------------------------------------------------------------- findings


def _findings(setup, name):
    return [(f.code, f.segment_id, f) for f in lap_findings(setup["analyse"](name))]


@pytest.mark.parametrize("name", ["twin", "near"])
def test_identical_or_near_identical_laps_have_no_findings(setup, name):
    assert _findings(setup, name) == []


def test_brake_15_m_early_gives_exactly_one_early_brake(setup):
    found = _findings(setup, "brake_early")
    assert [(c, s) for c, s, _ in found] == [("EARLY_BRAKE", "c1")]
    f = found[0][2]
    assert f.measured["delta_m"] == pytest.approx(-15.0, abs=2.0)
    assert f.confidence == "high"
    assert "later" in (f.suggestion or "")


def test_late_throttle(setup):
    found = _findings(setup, "late_throttle")
    assert [(c, s) for c, s, _ in found] == [("LATE_THROTTLE", "c4")]
    f = found[0][2]
    assert f.measured["delta_m"] == pytest.approx(30.0, abs=3.0)
    assert "carried" in f.phases


def test_slow_corner_gives_low_min_speed(setup):
    found = _findings(setup, "slow_t2")
    assert [(c, s) for c, s, _ in found] == [("LOW_MIN_SPEED", "c2")]
    assert found[0][2].measured["delta_kmh"] == pytest.approx(-7.5, abs=1.5)


def test_soft_braking_is_the_root_cause_not_early_braking(setup):
    found = _findings(setup, "soft_brake")
    assert [(c, s) for c, s, _ in found] == [("SOFT_BRAKING", "c1")]
    f = found[0][2]
    assert f.measured["lap"] == pytest.approx(0.85, abs=0.01)
    assert f.measured["brake_point_delta_m"] is not None and f.measured["brake_point_delta_m"] < -6


def test_lockup_and_wheelspin_events(setup):
    found = _findings(setup, "lock_spin")
    assert sorted((c, s) for c, s, _ in found) == [("LOCKUP", "c3"), ("WHEELSPIN", "c6")]
    by_code = {c: f for c, _, f in found}
    assert by_code["LOCKUP"].measured["lap_m"] == pytest.approx(6.0, abs=2.0)
    assert by_code["WHEELSPIN"].measured["lap_m"] == pytest.approx(10.0, abs=2.0)


def test_findings_are_sorted_by_impact(setup):
    a = setup["analyse"]("late_throttle")
    impacts = [f.time_impact_s for f in lap_findings(a)]
    assert impacts == sorted(impacts, reverse=True)


# ---------------------------------------------------------------- consistency


def test_session_consistency(setup):
    c = session_consistency(setup["conn"], setup["data"], setup["session"], setup["model"])
    usable = [
        r for r in db.list_laps(setup["conn"], setup["session"]["session_key"]) if r["usable"]
    ]
    assert c["laps"] == len(usable) == len(SCENARIOS)
    assert len(c["segments"]) == len(setup["model"].segments)
    best_lap = min(r["lap_time_ms"] for r in usable) / 1000
    assert c["ideal_lap_s"] <= best_lap + 0.002
    c1 = next(s for s in c["segments"] if s["id"] == "c1")
    assert c1["inconsistent"] is True  # brake-early and soft-brake laps vary C1 a lot
    c3 = next(s for s in c["segments"] if s["id"] == "c3")
    assert c3["inconsistent"] is False
