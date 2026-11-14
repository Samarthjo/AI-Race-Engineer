"""End-to-end ingest against a synthetic session whose true laps are known exactly."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from pitwall.capture.rawlog import RawRecord, RotatingRawLogWriter
from pitwall.ingest.pipeline import DB_NAME, IngestResult, ingest_capture
from pitwall.storage import db
from pitwall.synth.session import LapPlan, SessionSynth, SyntheticSession

PLANS = [
    LapPlan(6.0, kind="out"),
    LapPlan(12.0),  # clean
    LapPlan(12.5, flashback_at=0.5),  # flashback mid-lap
    LapPlan(12.25, invalid_from=0.5),  # track limits
    LapPlan(12.1, pause_at=0.3),  # paused 30 s
    LapPlan(12.2, drop_at=0.6, drop_frames=30),  # 0.5 s of packets lost
]


def write_capture(raw_dir: Path, name: str, records: list[RawRecord]) -> Path:
    cap = raw_dir / name
    w = RotatingRawLogWriter(cap, {"capture_id": name}, rotate_bytes=4 * 1024 * 1024)
    for r in records:
        w.write(r.wall_ns, r.mono_ns, r.payload)
    w.close()
    return cap


@pytest.fixture(scope="module")
def synth() -> SyntheticSession:
    return SessionSynth().run(PLANS, final_partial=0.4)


@pytest.fixture(scope="module")
def ingested(tmp_path_factory, synth) -> tuple[Path, IngestResult]:
    data = tmp_path_factory.mktemp("data")
    cap = write_capture(data / "raw", "20261114T120000Z", synth.records)
    conn = db.connect(data / DB_NAME)
    return data, ingest_capture(cap, data, conn)


def test_decodes_everything(ingested):
    _, res = ingested
    assert res.decoded == res.datagrams
    assert res.errors == {}
    assert len(res.sessions) == 1


def test_laps_match_ground_truth(ingested, synth):
    _, res = ingested
    laps = res.sessions[0].laps
    assert len(laps) == len(PLANS) + 1  # + final incomplete lap
    for lap, truth in zip(laps, synth.laps, strict=False):
        assert lap.lap_number == truth.lap_number
        assert lap.complete
        assert lap.lap_time_ms == truth.lap_time_ms
        assert lap.lap_time_source == "session_history"
        assert lap.valid == truth.valid
        assert lap.frames == truth.frames
        if truth.kind == "flying":
            assert (lap.s1_ms, lap.s2_ms, lap.s3_ms) == (truth.s1_ms, truth.s2_ms, truth.s3_ms)
            assert lap.s1_ms + lap.s2_ms + lap.s3_ms == lap.lap_time_ms
        assert lap.notes == [] or all("lap time sources disagree" not in n for n in lap.notes)


def test_lap_classification_and_usability(ingested):
    _, res = ingested
    out, clean, flash, invalid, paused, lossy, partial = res.sessions[0].laps

    assert out.kind == "out" and not out.usable and "out lap" in out.issues

    assert clean.kind == "flying" and clean.usable and clean.issues == [] and clean.notes == []

    assert flash.flashbacks == 1 and not flash.usable
    assert flash.pauses == 0, "a flashback must not be reported as a pause"
    assert any("flashback" in i for i in flash.issues)

    assert invalid.valid is False and invalid.invalid_flag_seen and not invalid.usable

    assert paused.pauses == 1 and paused.usable, "pausing does not corrupt a lap"
    assert paused.missing_frames == 0
    assert any("paused" in n for n in paused.notes)

    assert lossy.missing_frames == 30 and not lossy.usable
    assert any("packet loss" in i for i in lossy.issues)

    assert not partial.complete and partial.lap_time_ms is None and not partial.usable


def test_lap_context(ingested):
    _, res = ingested
    clean = res.sessions[0].laps[1]
    assert clean.tyre_actual == 18 and clean.tyre_visual == 17 and clean.tyre_age_laps == 2
    assert clean.fuel_start > clean.fuel_end
    assert clean.tyre_wear_start is not None and clean.tyre_wear_end is not None
    assert clean.assists["control_assists_on"] == []
    assert clean.assists["gearbox_assist"] == 1
    assert clean.setup is not None and clean.setup["front_wing"] == 20
    assert clean.setup["tyre_pressure_fl"] == pytest.approx(23.0)


def test_session_summary(ingested):
    _, res = ingested
    s = res.sessions[0].summary
    assert s["track_name"] == "Monza"
    assert s["session_type_name"] == "Time Trial"
    assert s["game_mode_name"] == "Time Trial"
    assert s["packet_format"] == 2025
    assert s["lap_count"] == 7
    assert s["usable_laps"] == 2
    assert s["best_usable_lap_ms"] == 12000
    assert s["flashbacks"] == 1
    assert s["sector2_start_m"] == pytest.approx(1000 / 3, abs=0.01)


def test_frames_table(ingested, synth):
    data, res = ingested
    key = res.sessions[0].session_key
    conn = db.connect(data / DB_NAME)
    row = db.find_sessions(conn, key)[0]
    table = pq.read_table(data / row["frames_path"])
    assert table.num_rows == sum(lap.frames for lap in res.sessions[0].laps)
    fi = table["frame_identifier"].to_numpy().astype(np.int64)
    assert np.all(np.diff(fi) > 0), "canonical frames are strictly increasing"
    lap_index = table["lap_index"].to_numpy()
    for lap in res.sessions[0].laps:
        assert int((lap_index == lap.lap_index).sum()) == lap.frames
    clean = table.filter(table["lap_index"].to_numpy() == 1)
    speed = clean["speed"].to_numpy()
    assert np.all(speed == 300)  # 1000 m in 12 s
    for col in ("throttle", "world_position_x", "fuel_in_tank", "wheel_speed_fl", "tyres_wear_rl"):
        assert clean[col].null_count == 0, col
    dist = clean["lap_distance"].to_numpy()
    assert dist[0] == pytest.approx(0.0) and dist[-1] == pytest.approx(1000 * 719 / 720, rel=1e-4)


def test_session_json_has_events_and_history(ingested):
    data, res = ingested
    conn = db.connect(data / DB_NAME)
    row = db.find_sessions(conn, res.sessions[0].session_key)[0]
    details = json.loads((data / row["details_path"]).read_text())
    codes = [e["code"] for e in details["events"]]
    assert codes.count("FLBK") == 1 and "SSTA" in codes
    assert details["session_history_player"]["num_laps"] >= 6
    assert details["participant_player"]["team_id"] == 1
    assert "name" not in details["participant_player"]
    assert details["streams"]["LAP_DATA"]["dropped_by_flashback"] == 60


def test_database_rows(ingested):
    data, res = ingested
    conn = db.connect(data / DB_NAME)
    rows = db.list_laps(conn, res.sessions[0].session_key)
    assert [r["lap_time_ms"] for r in rows[:6]] == [6000, 12000, 12500, 12250, 12100, 12200]
    assert [r["usable"] for r in rows] == [0, 1, 0, 0, 1, 0, 0]
    assert json.loads(rows[2]["issues_json"]) == ["flashback used (1x)"]


def test_reingest_is_deterministic_and_idempotent(tmp_path, synth):
    tables = []
    lap_rows = []
    for name in ("a", "b"):
        data = tmp_path / name
        cap = write_capture(data / "raw", "20261114T120000Z", synth.records)
        conn = db.connect(data / DB_NAME)
        res = ingest_capture(cap, data, conn)
        res = ingest_capture(cap, data, conn)  # second run replaces the first
        assert len(db.list_sessions(conn)) == 1
        row = db.list_sessions(conn)[0]
        tables.append(pq.read_table(data / row["frames_path"]))
        lap_rows.append([dict(r) for r in db.list_laps(conn, res.sessions[0].session_key)])
    assert tables[0].equals(tables[1])
    assert lap_rows[0] == lap_rows[1]


def test_garbage_and_wrong_format_are_counted_not_fatal(tmp_path, synth):
    bad = [
        RawRecord(1, 1, b"short"),
        RawRecord(2, 2, synth.records[5].payload[:-3]),  # truncated packet
        RawRecord(3, 3, b"\xe8\x07" + synth.records[5].payload[2:]),  # packetFormat 2024
    ]
    data = tmp_path / "d"
    cap = write_capture(data / "raw", "20261114T130000Z", bad + synth.records)
    res = ingest_capture(cap, data, db.connect(data / DB_NAME))
    assert res.errors == {"too_short": 1, "size_mismatch": 1, "unsupported_format": 1}
    assert res.decoded == len(synth.records)
    assert len(res.sessions[0].laps) == 7


def test_two_sessions_in_one_capture(tmp_path):
    a = SessionSynth(session_uid=1).run([LapPlan(12.0), LapPlan(12.0)], final_partial=None)
    b = SessionSynth(session_uid=2, track_id=7).run([LapPlan(12.0)], final_partial=None)
    shift = a.records[-1].mono_ns + 5_000_000_000
    b_records = [RawRecord(r.wall_ns + shift, r.mono_ns + shift, r.payload) for r in b.records]
    data = tmp_path / "d"
    cap = write_capture(data / "raw", "20261114T140000Z", a.records + b_records)
    res = ingest_capture(cap, data, db.connect(data / DB_NAME))
    tracks = sorted(s.summary["track_name"] for s in res.sessions)
    assert tracks == ["Monza", "Silverstone"]


def test_assists_are_reported(tmp_path):
    s = SessionSynth(traction_control=2, anti_lock_brakes=1).run([LapPlan(12.0), LapPlan(12.0)])
    data = tmp_path / "d"
    cap = write_capture(data / "raw", "20261114T150000Z", s.records)
    res = ingest_capture(cap, data, db.connect(data / DB_NAME))
    lap = res.sessions[0].laps[1]
    assert lap.assists["control_assists_on"] == ["traction control", "ABS"]
    assert any("assists on" in n for n in lap.notes)
