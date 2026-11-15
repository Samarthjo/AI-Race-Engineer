"""Stage 7: driver history. Statistics on made-up numbers with known answers, then a
synthetic history of several sessions with known habits: what was fixed, what got worse,
what keeps coming back, and what the race engineer may say about it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from pitwall.capture.rawlog import RawRecord, RotatingRawLogWriter
from pitwall.capture.session import write_manifest
from pitwall.engineer.facts import Fact, parse_numbers
from pitwall.engineer.narrator import deterministic_output
from pitwall.engineer.service import build_context, grounding_for
from pitwall.engineer.validate import check_narrative
from pitwall.history import build as hbuild
from pitwall.history import patterns, recommendations, trends
from pitwall.history import service as hservice
from pitwall.history.profile import profile_facts
from pitwall.history.stats import NOT_ENOUGH, compare_groups
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.storage import db
from pitwall.synth.session import LapPlan, SessionSynth
from pitwall.synth.track import DriverProfile, build_track
from pitwall.track.model import load_model, rename_segment
from pitwall.track.service import session_track_key

DAY_NS = 86_400 * 10**9
T0_NS = 1_790_000_000 * 10**9  # 2026-09-21
TRACK = build_track()
GROUP = "track11-monza:0"

# ------------------------------------------------------------------ statistics


def test_no_conclusion_from_too_few_laps():
    three, four = [80.0, 80.1, 80.2], [79.0, 79.1, 79.2, 79.0]
    c = compare_groups(three, four, better="lower", min_change=0.05)
    assert c.status == NOT_ENOUGH and c.delta is None and c.n_before == 3


def test_a_clear_change_is_found_in_the_right_direction():
    before = [80.00, 80.05, 79.95, 80.02, 79.98, 80.04]
    after = [x - 0.30 for x in before]
    faster = compare_groups(before, after, better="lower", min_change=0.05)
    assert faster.status == "improved" and faster.delta == pytest.approx(-0.30)
    slower = compare_groups(after, before, better="lower", min_change=0.05)
    assert slower.status == "worse"
    # a speed going up is an improvement when higher is better
    assert compare_groups(after, before, better="higher", min_change=0.05).status == "improved"


def test_changes_inside_the_noise_or_below_the_minimum_are_unchanged():
    noisy = [80.0, 80.4, 79.6, 80.3, 79.7, 80.2, 79.8]
    shifted = [x - 0.10 for x in noisy]  # 0.1 s against a spread of about 0.3 s
    c = compare_groups(noisy, shifted, better="lower", min_change=0.05)
    assert c.status == "unchanged" and c.threshold is not None and c.threshold > 0.10
    steady = [80.0] * 6
    tiny = compare_groups(steady, [79.98] * 6, better="lower", min_change=0.05)
    assert tiny.status == "unchanged" and tiny.sigma == 0.0  # below the minimum change


def _row(session: str, lap: int, findings: list[tuple[str, str]], *, ref: int = 0, **kw: Any):
    return {
        "session_key": session,
        "lap_index": lap,
        "lap_number": lap + 1,
        "reference_lap": ref,
        "first_utc": session,
        "lap_time_ms": 80_000,
        "findings": [
            {
                "code": code,
                "segment_id": seg,
                "segment_name": seg.upper(),
                "title": code.title(),
                "impact_s": 0.1,
                "measured": {},
                "suggestion": "Try something.",
            }
            for code, seg in findings
        ],
        "segments": kw.pop("segments", []),
        **kw,
    }


def test_recurring_patterns_need_enough_laps_and_a_large_enough_share():
    early = [("EARLY_BRAKE", "c1")]
    rows = [_row("s1", 0, [])] + [_row("s1", i, early if i <= 3 else []) for i in range(1, 7)]
    out = patterns.recurring(rows)
    # the reference lap (lap 0) is not counted; 3 of 6 compared laps
    assert out["compared_laps"] == 6 and out["status"] == "ok"
    assert [(p["code"], p["laps"], p["of"]) for p in out["patterns"]] == [("EARLY_BRAKE", 3, 6)]
    rare = [_row("s1", 0, [])] + [_row("s1", i, early if i <= 2 else []) for i in range(1, 7)]
    assert patterns.recurring(rare)["patterns"] == []  # 2 laps: below the minimum count
    few = [_row("s1", 0, [])] + [_row("s1", i, early) for i in range(1, 4)]
    assert patterns.recurring(few)["status"] == "not enough data"


def test_a_fixed_habit_fades_out_of_the_recurring_patterns():
    early = [("EARLY_BRAKE", "c1")]
    old = [_row("a", i, early, ref=99) for i in range(30)]
    new = [_row("b", i, [], ref=99) for i in range(patterns.WINDOW)]
    assert patterns.recurring(old)["patterns"]
    assert patterns.recurring(old + new)["patterns"] == []  # outside the latest 40 laps


def _seg_row(session: str, lap: int, times: dict[str, float], metric: float | None = None):
    segs = [
        {"id": k, "time_s": v, **({"metrics": {"brake_onset_m": metric}} if k == "c1" else {})}
        for k, v in times.items()
    ]
    return _row(session, lap, [("EARLY_BRAKE", "c1")] if lap else [], segments=segs)


def test_recommendations_are_judged_on_their_target_metric():
    base = {"c1": 5.0, "s1": 10.0}
    s1 = [_seg_row("s1", i, base, 300.0 + (0 if i == 0 else -15)) for i in range(6)]
    recs = recommendations.session_recommendations(s1)
    assert [(r["code"], r["metric"], r["better"]) for r in recs] == [
        ("EARLY_BRAKE", "brake_onset_m", "higher")
    ]
    later = [_seg_row("s2", i, {"c1": 4.9, "s1": 10.0}, 300.0 + i * 0.2) for i in range(5)]
    ev = recommendations.evaluate(recs[0], s1, later)
    assert ev["outcome"] == "improved" and ev["metric_change"]["delta"] > 10
    assert ev["segment_time"]["status"] == "improved"
    earlier = [_seg_row("s2", i, base, 280.0 + i * 0.2) for i in range(5)]
    assert recommendations.evaluate(recs[0], s1, earlier)["outcome"] == "worse"
    two = [_seg_row("s2", i, base, 300.0) for i in range(2)]
    assert recommendations.evaluate(recs[0], s1, two)["outcome"] == "inconclusive"
    tracked = recommendations.track_recommendations(
        [{"session_key": "s1", "date": "d1"}, {"session_key": "s2", "date": "d2"}],
        {"s1": s1, "s2": later},
    )
    assert tracked[0]["outcome"] == "improved" and tracked[0]["next_session_key"] == "s2"
    alone = recommendations.track_recommendations([{"session_key": "s1"}], {"s1": s1})
    assert alone[0]["outcome"] == "open"


def test_a_corner_is_measured_with_its_exit_straight_and_flat_kinks_are_no_strength():
    info = [("c1", "C1", "corner"), ("s1", "Straight", "straight"), ("c2", "C2", "flat")]
    assert trends.corner_zones(info) == [
        ("c1", "C1", "corner", ["c1", "s1"]),
        ("c2", "C2", "flat", ["c2"]),
    ]
    rows = []
    for i in range(6):
        slow_exit = i % 2 == 1  # every other lap: 0.05 s in the corner, 0.30 s on the straight
        times = {"c1": 5.0 + 0.05 * slow_exit, "s1": 10.0 + 0.30 * slow_exit, "c2": 3.0}
        rows.append(_row("s", i, [], segments=[{"id": k, "time_s": v} for k, v in times.items()]))
    out = trends.corner_potential(rows, info)
    c1 = next(c for c in out["corners"] if c["segment_id"] == "c1")
    assert c1["mean_gap_s"] == pytest.approx(0.175) and c1["zone_name"] == "C1 + Straight"
    assert [c["segment_id"] for c in out["weakest"]] == ["c1"]
    assert out["strongest"] == []  # the only other corner is flat out


def test_a_trend_says_when_the_car_or_conditions_differ():
    before = [{"team_id": 1, "weather": 0, "track_temp_c": 30}]
    after = [{"team_id": 2, "weather": 0, "track_temp_c": 38}]
    caveats = trends._caveats(before, after)
    assert any("car" in c for c in caveats) and any("30 to 38" in c for c in caveats)
    assert trends._caveats(before, before) == []


# ------------------------------------------------------------- synthetic history


def _jitter(i: int, **kw: Any) -> DriverProfile:
    """Lap-to-lap noise: grip within ±0.15 %."""
    return DriverProfile(grip_scale=1.0 + 0.0015 * ((i * 7919) % 5 - 2) / 2, **kw)


def _capture(raw: Path, name: str, records: list[RawRecord], synthetic: bool) -> Path:
    cap = raw / name
    meta = {"capture_id": name, **({"synthetic": True} if synthetic else {})}
    w = RotatingRawLogWriter(cap, meta, rotate_bytes=4 * 1024 * 1024)
    for r in records:
        w.write(r.wall_ns, r.mono_ns, r.payload)
    w.close()
    if synthetic:  # the capture's manifest is what marks it synthetic
        write_manifest(cap, {"capture_id": name, "status": "complete", "synthetic": True})
    return cap


def _session(data: Path, conn, name: str, day: int, laps: list[DriverProfile], *, synthetic=False):
    plans = [LapPlan(kind="out", driver=DriverProfile())] + [LapPlan(driver=p) for p in laps]
    synth = SessionSynth(
        track=TRACK, track_id=11, session_uid=0x5000 + day, start_wall_ns=T0_NS + day * DAY_NS
    ).run(plans, final_partial=0.2)
    cap = _capture(data / "raw", name, synth.records, synthetic)
    return ingest_capture(cap, data, conn).sessions[0].session_key


def _srow(conn, key: str) -> dict[str, Any]:
    row = db.get_session(conn, key)
    assert row is not None
    return dict(row)


EARLY = {0: 15.0}  # corner index 0 is C1


@pytest.fixture(scope="module")
def hist(tmp_path_factory):
    data = tmp_path_factory.mktemp("hist")
    conn = db.connect(data / DB_NAME)
    # S1: brakes 15 m early into C1 on four laps
    s1 = [_jitter(0)] + [_jitter(i, brake_early_m=EARLY) for i in range(1, 5)] + [_jitter(5)]
    # S2: C1 fixed; full throttle 20 m late out of C4 on four laps
    late20 = [_jitter(i, throttle_delay_m={3: 20.0}) for i in range(1, 5)]
    s2 = [_jitter(0), *late20, _jitter(5)]
    # S3: 40 m late out of C4 on every lap; the early braking at C1 is back on three laps
    s3 = [
        _jitter(i, throttle_delay_m={3: 40.0}, brake_early_m=EARLY if i in (1, 3, 5) else {})
        for i in range(6)
    ]
    # S4: only two laps
    s4 = [_jitter(0), _jitter(1)]
    keys = [_session(data, conn, f"s{n + 1}", n, laps) for n, laps in enumerate((s1, s2, s3, s4))]
    built = hservice.ensure(conn, data)
    return {"data": data, "conn": conn, "keys": keys, "built": built}


def _rows(h):
    return hservice.load_rows(h["conn"], "track11-monza", 0)


def test_every_usable_lap_is_in_the_history_with_its_versions(hist):
    assert hist["built"]["sessions"] == 4 and hist["built"]["laps"] == 20
    rows = _rows(hist)
    assert len(rows) == 20 and {r["analysis_version"] for r in rows} == {hbuild.ANALYSIS_VERSION}
    model = load_model(hist["data"], "track11-monza")
    assert model is not None and {r["model_version"] for r in rows} == {model.version}
    assert hbuild.stale(hist["conn"], hist["data"]) == []  # nothing to redo
    assert hservice.ensure(hist["conn"], hist["data"])["sessions"] == 0
    s1 = [r for r in rows if r["session_key"] == hist["keys"][0]]
    ref = next(r for r in s1 if r["lap_index"] == r["reference_lap"])
    assert ref["findings"] == [] and ref["delta_s"] == 0.0
    early = [r for r in s1 if any(f["code"] == "EARLY_BRAKE" for f in r["findings"])]
    assert len(early) == 4
    assert early[0]["findings"][0]["title"] == "Brakes earlier than your best lap"


def test_recommendation_outcomes_follow_what_happened_next(hist):
    view = hservice.track_history(hist["conn"], hist["data"], GROUP)
    recs = {(r["session_key"], r["segment_id"], r["code"]): r for r in view["recommendations"]}
    s1, s2, s3, _ = hist["keys"]
    fixed = recs[(s1, "c1", "EARLY_BRAKE")]
    assert fixed["outcome"] == "improved" and fixed["metric"] == "brake_onset_m"
    assert fixed["metric_change"]["delta"] == pytest.approx(15, abs=2)  # braking 15 m later
    assert fixed["segment_time"]["status"] == "improved"
    worse = recs[(s2, "c4", "LATE_THROTTLE")]
    assert worse["outcome"] == "worse" and worse["metric"] == "full_throttle_m"
    assert worse["metric_change"]["delta"] == pytest.approx(20, abs=3)
    inconclusive = recs[(s3, "c1", "EARLY_BRAKE")]
    assert inconclusive["outcome"] == "inconclusive"  # the next session has only two laps
    assert inconclusive["metric_change"]["n_after"] == 2


def test_recurring_pattern_trend_and_corners(hist):
    view = hservice.track_history(hist["conn"], hist["data"], GROUP)
    pats = view["patterns"]
    assert pats["compared_laps"] == 16
    assert [(p["code"], p["segment_id"], p["laps"], p["sessions"]) for p in pats["patterns"]] == [
        ("EARLY_BRAKE", "c1", 7, 2)
    ]
    # the latest session has two laps: no trend can be told from it
    assert view["trend"]["status"] == NOT_ENOUGH
    # as of S3: slower than S1 and S2 (40 m late on the throttle everywhere)
    s3 = _srow(hist["conn"], hist["keys"][2])
    model = load_model(hist["data"], "track11-monza")
    upto = hservice.until(_rows(hist), s3)
    assert trends.lap_trend(upto)["status"] == "worse"
    weakest = [c["segment_id"] for c in view["corners"]["weakest"]]
    assert weakest[0] == "c4"  # its exit straight carries most of the loss
    c4 = next(c for c in view["corners"]["corners"] if c["segment_id"] == "c4")
    assert c4["zone"] == ["c4", "s4"] and model is not None


def test_the_engineer_quotes_the_history_and_stays_grounded(hist):
    conn, data = hist["conn"], hist["data"]
    s3 = hist["keys"][2]
    early_lap = next(
        r["lap_index"]
        for r in _rows(hist)
        if r["session_key"] == s3 and any(f["code"] == "EARLY_BRAKE" for f in r["findings"])
    )
    ctx = build_context(conn, data, s3, early_lap)
    report = ctx.report
    ids = report.history_facts
    assert "hist:summary" in ids and "hist:trend" in ids
    assert "hist:pattern:c1:EARLY_BRAKE" in ids and "hist:rec:c4:LATE_THROTTLE" in ids
    trend = report.fact("hist:trend").text
    assert "slower than before" in trend and "2 sessions before it" in trend
    rec = report.fact("hist:rec:c4:LATE_THROTTLE").text
    assert "went the wrong way" in rec and "full-throttle point" in rec
    assert "3 sessions, 18 usable laps" in report.fact("hist:summary").text
    focus = next(f for f in report.focus if f.segment_id == "c1")
    assert focus.recurring == "hist:pattern:c1:EARLY_BRAKE"
    for f in report.facts:
        assert parse_numbers(f.text, report.segment_names) == f.numbers
    g = grounding_for(ctx)
    assert check_narrative(deterministic_output(report), g) == []
    pattern = report.fact("hist:pattern:c1:EARLY_BRAKE")
    quoted = pattern.data["laps"], pattern.data["of"]
    good = {
        "summary": {
            "text": f"Braking early into C1 keeps coming back: {quoted[0]} of {quoted[1]} laps.",
            "cites": [pattern.id],
        },
        "sections": [],
        "focus": [],
    }
    assert check_narrative(good, g) == []
    bad = json.loads(json.dumps(good))
    bad["summary"]["text"] = "Braking early into C1 keeps coming back: 9 of 12 laps."
    assert any(v.kind == "number" for v in check_narrative(bad, g))
    # as of S2, nothing from S3 or S4 is known
    early_s2 = build_context(conn, data, hist["keys"][1], 1).report
    assert "2 sessions, 12 usable laps" in early_s2.fact("hist:summary").text


def test_real_and_synthetic_histories_never_mix(hist, tmp_path):
    data = tmp_path / "copy"
    shutil.copytree(hist["data"], data)
    conn = db.connect(data / DB_NAME)
    key = _session(data, conn, "syn", 4, [_jitter(i) for i in range(5)], synthetic=True)
    hservice.ensure(conn, data)
    syn_key = session_track_key(_srow(conn, key))
    assert syn_key.startswith("synthetic-")  # its own track key: never the real track's
    syn = hservice.load_rows(conn, syn_key, 0)
    assert len(syn) == 5 and {r["synthetic"] for r in syn} == {1}
    assert len(hservice.load_rows(conn, "track11-monza", 0)) == 20
    ov = hservice.overview(conn, data)
    assert ov["totals"]["real_sessions"] == 4 and ov["totals"]["synthetic_sessions"] == 1
    assert ov["totals"]["usable_laps"] == 20 and ov["totals"]["synthetic_usable_laps"] == 5
    assert ov["latest_session"]["synthetic"] is False  # your own session comes first
    flags = {g["id"]: g["synthetic"] for g in ov["groups"]}
    assert flags == {GROUP: False, f"{syn_key}:0": True}
    facts = build_context(conn, data, key, 1).report
    summary = facts.fact("hist:summary").text
    assert "1 session, 5 usable laps" in summary  # none of the real laps


def test_the_history_is_rebuilt_when_the_corners_or_the_code_change(hist, tmp_path, monkeypatch):
    data = tmp_path / "copy"
    shutil.copytree(hist["data"], data)
    conn = db.connect(data / DB_NAME)
    model = rename_segment(data, "track11-monza", "c1", "Prima Variante")
    assert len(hbuild.stale(conn, data)) == 4
    assert hservice.ensure(conn, data)["sessions"] == 4
    rows = hservice.load_rows(conn, "track11-monza", 0)
    assert {r["model_version"] for r in rows} == {model.version}
    view = hservice.track_history(conn, data, GROUP)
    assert view["excluded_laps"] == 0
    assert any(c["segment_name"] == "Prima Variante" for c in view["corners"]["corners"])
    monkeypatch.setattr(hbuild, "ANALYSIS_VERSION", "test-2")
    assert len(hbuild.stale(conn, data)) == 4


def test_a_session_that_cannot_be_analysed_is_recorded_not_retried(hist, tmp_path, monkeypatch):
    from pitwall.analysis.trace import NotComparableError

    data = tmp_path / "copy"
    shutil.copytree(hist["data"], data)
    conn = db.connect(data / DB_NAME)

    def refuse(*_a: Any, **_k: Any):
        raise NotComparableError("track model is 1000 m but this track is 5793 m")

    monkeypatch.setattr(hbuild, "model_for_session", refuse)
    s4 = _srow(conn, hist["keys"][3])
    assert hbuild.build(conn, data, [s4])["laps"] == 0
    state = hservice.cache_state(conn)
    assert state["by_status"] == {"ok": 3, "not comparable": 1}
    assert state["problems"][0]["reason"].startswith("track model is 1000 m")
    assert hbuild.stale(conn, data) == []  # not retried until something changes


# -------------------------------------------------------------- API and CLI


def test_history_api(hist):
    from fastapi.testclient import TestClient

    from pitwall.api.app import create_app

    c = TestClient(create_app(hist["data"], live=None))
    dash = c.get("/api/dashboard").json()
    assert dash["totals"]["tracks"] == 1 and dash["groups"][0]["id"] == GROUP
    assert dash["groups"][0]["trend"]["status"] == NOT_ENOUGH
    assert {r["code"] for r in dash["open_recommendations"]} == set()  # S4: nothing recurring
    view = c.get("/api/history", params={"group": GROUP}).json()
    assert view["group"]["sessions"] == 4 and len(view["sessions"]) == 4
    assert view["thresholds"]["evaluation_min_laps"] == recommendations.EVAL_MIN_LAPS
    pro = c.get("/api/history", params={"group": GROUP, "pro": "nope"}).json()
    assert "not stored" in pro["pro"]["error"]
    assert c.get("/api/history", params={"group": "track99-nowhere:0"}).status_code == 404
    assert c.get("/api/history", params={"group": "bad"}).status_code == 400
    rebuilt = c.post("/api/history/build").json()
    assert rebuilt["sessions"] == 0 and rebuilt["cache"]["by_status"] == {"ok": 4}
    assert c.post("/api/history/build", params={"force": True}).json()["laps"] == 20


def test_history_cli(hist):
    from typer.testing import CliRunner

    from pitwall.cli import app

    runner = CliRunner(env={"COLUMNS": "220"})
    args = ["--data-dir", str(hist["data"])]
    res = runner.invoke(app, ["history", *args])
    assert res.exit_code == 0, res.output
    assert GROUP in res.output and "Monza" in res.output and "not enough data" in res.output
    res = runner.invoke(app, ["history", "show", "monza", *args])
    assert res.exit_code == 0, res.output
    assert "Brakes earlier than your best lap" in res.output and "improved" in res.output
    assert "went the wrong way" not in res.output and "worse" in res.output
    assert "Recurring patterns" in res.output and "7 of 16 laps" in res.output
    res = runner.invoke(app, ["history", "build", *args])
    assert res.exit_code == 0 and "0 session(s) analysed" in res.output
    res = runner.invoke(app, ["history", "show", "nowhere", *args])
    assert res.exit_code == 1


def test_profile_without_history_says_so():
    session = {"session_key": "x", "first_utc": "2026", "track_name": "Monza", "formula": 0}
    facts: list[Fact] = profile_facts([], session, None)
    assert [f.id for f in facts] == ["hist:summary"] and "No history" in facts[0].text
