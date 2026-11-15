"""Stage 6: live telemetry, driven by replayed synthetic sessions with known answers.

The live engine must agree with the post-lap analysis: same lap times, the same delta at
the line and (to the millisecond) along the lap, the same findings in its provisional
report, and it must follow flashbacks and count lost packets. The feed must reach the page
quickly (under 100 ms from packet to WebSocket message on this machine)."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pitwall.analysis.compare import compare, reference_from_trace
from pitwall.analysis.trace import load_trace
from pitwall.api.app import create_app
from pitwall.engineer.service import build_context
from pitwall.f1udp.header import PacketHeader, PacketId
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.live.demo import DemoReplay
from pitwall.live.engine import LiveEngine
from pitwall.storage import db
from pitwall.synth.session import LapPlan, SessionSynth
from pitwall.synth.track import DriverProfile, build_track
from tests.test_ingest import write_capture

BASE = DriverProfile()
TRACK = build_track()
LIVE_PLANS = [
    LapPlan(kind="out", driver=BASE),
    LapPlan(driver=DriverProfile(brake_early_m={0: 15.0})),  # lap 2
    LapPlan(driver=BASE, flashback_at=0.5),  # lap 3
    LapPlan(driver=DriverProfile(throttle_delay_m={3: 30.0})),  # lap 4
]


def _live_session():
    return SessionSynth(track=TRACK, track_id=11, session_uid=0xB2).run(
        LIVE_PLANS, final_partial=0.2
    )


def _settle(engine: LiveEngine) -> None:
    engine._executor.submit(lambda: None).result()  # queued background work is done


def _replay(engine: LiveEngine, records, *, skip=None, samples=None) -> None:
    """Replay as fast as possible, but let background work finish where real time would
    (a reference is ready long before a lap ends, an analysis long before the next one)."""
    waited, laps = False, 0
    for i, rec in enumerate(records):
        if skip is not None and skip(i, rec):
            continue
        engine.observe(rec.payload, rec.mono_ns / 1e9)
        if not waited and engine.session is not None:
            _settle(engine)
            waited = True
        if len(engine.laps) != laps:
            laps = len(engine.laps)
            _settle(engine)
        if samples is not None and PacketHeader.unpack(rec.payload).packet_id == PacketId.LAP_DATA:
            st = engine.state
            if st.get("delta_s") is not None:
                samples.setdefault(st["lap_number"], []).append(
                    (st["lap_distance_m"], st["current_lap_time_ms"], st["delta_s"])
                )
    engine.flush()
    _settle(engine)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Recording A (ingested: the personal best), then session B replayed live."""
    data: Path = tmp_path_factory.mktemp("live")
    conn = db.connect(data / DB_NAME)
    a = SessionSynth(track=TRACK, track_id=11, session_uid=0xA1).run(
        [LapPlan(kind="out", driver=BASE), LapPlan(driver=BASE)], final_partial=0.2
    )
    ingest_capture(write_capture(data / "raw", "20261115T110000Z", a.records), data, conn)
    b = _live_session()
    engine = LiveEngine(data_dir=data)
    samples: dict[int, list] = {}
    _replay(engine, b.records, samples=samples)
    res = ingest_capture(write_capture(data / "raw", "20261115T120000Z", b.records), data, conn)
    return {
        "data": data,
        "conn": conn,
        "engine": engine,
        "samples": samples,
        "synth": b,
        "session": dict(db.get_session(conn, res.sessions[0].session_key) or {}),
    }


def test_laps_match_the_ingested_laps(world):
    engine, conn, session = world["engine"], world["conn"], world["session"]
    ingested = {r["lap_number"]: dict(r) for r in db.list_laps(conn, session["session_key"])}
    live = {lap.number: lap for lap in engine.laps}
    assert sorted(live) == [1, 2, 3, 4]
    for n, lap in live.items():
        assert lap.lap_time_ms == ingested[n]["lap_time_ms"], n
        assert lap.usable == bool(ingested[n]["usable"]), (n, lap.issues)
        assert list(lap.sectors_ms[:2]) == [ingested[n]["s1_ms"], ingested[n]["s2_ms"]]
    assert live[1].kind == "out" and not live[1].usable
    assert live[3].flashbacks == 1 and "flashback used (1x)" in live[3].issues


def test_live_delta_equals_the_post_lap_delta(world):
    engine, conn, data, session = world["engine"], world["conn"], world["data"], world["session"]
    assert engine.reference is not None and engine.reference.key == "personal_best"
    pb_session, pb_lap = db.personal_best_lap(conn, 11, 0, 0) or (None, None)
    assert pb_session is not None and pb_lap is not None
    ref = reference_from_trace(load_trace(data, dict(pb_session), dict(pb_lap)), "pb", "pb")
    for row in db.list_laps(conn, session["session_key"]):
        lap = dict(row)
        if lap["kind"] != "flying" or not lap["complete"]:
            continue
        post = compare(load_trace(data, session, lap), ref)
        live_lap = next(x for x in engine.laps if x.number == lap["lap_number"])
        # at the line: identical (both from the official lap time)
        assert live_lap.delta_s == pytest.approx(post.delta[-1], abs=1e-9)
        # along the lap: within the 1 ms the live feed rounds to
        d, _, delta = (np.array(x) for x in zip(*world["samples"][lap["lap_number"]], strict=True))
        assert len(d) > 1000
        assert np.max(np.abs(delta - np.interp(d, post.distance, post.delta))) <= 0.0015
        # sector deltas add up to the delta at the line
        assert sum(live_lap.sector_deltas) == pytest.approx(live_lap.delta_s, abs=0.002)


def test_provisional_report_matches_the_post_lap_report(world):
    engine, conn, data, session = world["engine"], world["conn"], world["data"], world["session"]
    for n, expected in ((2, "find:c1:EARLY_BRAKE"), (4, "find:c4:LATE_THROTTLE")):
        live_lap = next(x for x in engine.laps if x.number == n)
        report = live_lap.report
        assert report is not None and report["status"] == "ready", report
        assert report["provisional"] is True and "personal best" in report["reference"]
        lap_index = next(
            r["lap_index"]
            for r in db.list_laps(conn, session["session_key"])
            if r["lap_number"] == n
        )
        post = build_context(
            conn, data, session["session_key"], lap_index, ref_kind="personal_best"
        ).report
        assert [f["fact_id"] for f in report["focus"]] == [f.fact_id for f in post.focus]
        assert report["focus"][0]["fact_id"] == expected
    skipped = next(x for x in engine.laps if x.number == 3).report
    assert (
        skipped is not None and skipped["status"] == "skipped" and "flashback" in skipped["reason"]
    )


def test_without_a_database_the_session_best_becomes_the_reference():
    engine = LiveEngine()
    _replay(engine, _live_session().records)
    assert engine.reference is not None and engine.reference.key == "session_best"
    assert "lap 2" in engine.reference.label  # the first clean lap
    last, first_clean = engine.laps[-1].lap_time_ms, engine.laps[1].lap_time_ms
    assert last is not None and first_clean is not None
    assert engine.laps[-1].delta_s == pytest.approx((last - first_clean) / 1000, abs=1e-9)


def test_lost_packets_are_counted():
    engine = LiveEngine()
    seen = {"lap": 0}

    def drop(_i, rec):
        if PacketHeader.unpack(rec.payload).packet_id != PacketId.LAP_DATA:
            return False
        seen["lap"] += 1
        return seen["lap"] % 10 == 0  # lose every 10th Lap Data packet

    _replay(engine, _live_session().records, skip=drop)
    lap2 = next(x for x in engine.laps if x.number == 2)
    assert lap2.summary()["loss_pct"] == pytest.approx(10.0, abs=1.0)
    assert not lap2.usable and any("packet loss" in i for i in lap2.issues)
    assert engine.snapshot()["loss_pct"] == pytest.approx(10.0, abs=1.5)


def test_feed_reaches_the_page_quickly(world):
    """UDP packet in -> WebSocket message out, measured on this machine."""
    engine = LiveEngine(port=0)
    app = create_app(world["data"], live=engine)
    records = _live_session().records
    with TestClient(app) as client, client.websocket_connect("/api/live/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["data"]["listening"] is True
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        session_packet = next(
            r for r in records if PacketHeader.unpack(r.payload).packet_id == PacketId.SESSION
        )
        latencies = []
        msg: dict = {}
        for rec in [session_packet, *records[2000:2400:40]]:
            before = engine.seq
            sent = time.monotonic()
            tx.sendto(rec.payload, ("127.0.0.1", engine.port))
            while True:
                msg = ws.receive_json()
                if msg["type"] == "state" and msg["data"]["seq"] > before:
                    latencies.append(time.monotonic() - sent)
                    break
        tx.close()
    assert max(latencies) < 0.1, latencies
    assert msg["data"]["receiving"] is True and msg["data"]["session"] is not None


def test_live_api_references_track_and_demo(world):
    engine = LiveEngine(data_dir=world["data"])
    app = create_app(world["data"], live=engine)
    with TestClient(app) as c:
        _replay(engine, _live_session().records)
        refs = c.get("/api/live/references").json()
        keys = [o["key"] for o in refs["options"]]
        assert keys[:3] == ["auto", "personal_best", "session_best"]
        assert refs["reference"]["key"] == "personal_best"
        assert c.post("/api/live/reference", params={"key": "session_best"}).status_code == 200
        _settle(engine)
        assert engine.reference is not None and engine.reference.key == "session_best"
        assert c.post("/api/live/reference", params={"key": "nope"}).status_code == 422
        track = c.get("/api/live/track").json()
        assert track["available"] and len(track["x"]) == len(track["distance"]) > 100
        snap = c.get("/api/live").json()
        assert snap["laps"][-1]["number"] == 4 and snap["reference"]["key"] == "session_best"
        started = c.post("/api/live/demo", params={"action": "start", "speed": 20}).json()
        assert started["running"] is True
        assert _wait_for(lambda: engine.session is not None and engine.session["synthetic"])
        assert c.post("/api/live/demo", params={"action": "stop"}).json()["running"] is False


def _wait_for(cond, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_demo_replay_is_marked_synthetic():
    engine = LiveEngine()
    replay = DemoReplay(engine, speed=20)
    replay.start()
    try:
        assert _wait_for(lambda: engine.session is not None)
        assert engine.session is not None and engine.session["synthetic"] is True
        assert engine.session["track_id"] == -1
    finally:
        replay.stop()
    # a real session after the demo starts afresh, not marked synthetic
    _replay(engine, _live_session().records[:3000])
    assert engine.session is not None and engine.session["synthetic"] is False
