from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pitwall.api.app import create_app
from pitwall.api.live import LiveStatus
from pitwall.f1udp import f125
from pitwall.f1udp.header import PacketId
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.storage import db
from pitwall.synth.demo import write_demo_capture


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> tuple[TestClient, str]:
    data: Path = tmp_path_factory.mktemp("data")
    cap = write_demo_capture(data / "raw")
    res = ingest_capture(cap, data, db.connect(data / DB_NAME))
    app = create_app(data, live=None)
    return TestClient(app), res.sessions[0].session_key


def test_meta_and_sessions(client):
    c, key = client
    assert c.get("/api/meta").json()["reference_kinds"][0] == "session_best"
    sessions = c.get("/api/sessions").json()
    assert sessions[0]["session_key"] == key
    assert sessions[0]["synthetic"] is True
    assert sessions[0]["track_name"] == "Synthetic test track"


def test_laps_flags_comparable(client):
    c, key = client
    body = c.get("/api/laps", params={"session": key}).json()
    laps = body["laps"]
    assert laps[0]["kind"] == "out" and laps[0]["comparable"] is False
    assert laps[1]["comparable"] is True and laps[1]["usable"] is True
    assert laps[-1]["complete"] is False and laps[-1]["comparable"] is False


@pytest.mark.parametrize("ref", ["session_best", "personal_best", "theoretical_best"])
def test_compare_payload(client, ref):
    c, key = client
    r = c.get("/api/compare", params={"session": key, "lap": 4, "ref": ref})
    assert r.status_code == 200, r.text
    body = r.json()
    tr = body["trace"]
    n = len(tr["distance"])
    assert n > 1000 and tr["step_m"] == 2.0
    for k in ("delta", "loss_rate", "x", "z"):
        assert len(tr[k]) == n
    for side in ("lap", "ref"):
        assert set(tr[side]) == {"speed", "throttle", "brake", "gear", "steer", "rpm"}
        assert all(len(v) == n for v in tr[side].values())
    assert tr["distance"][-1] == body["session"]["track_length_m"]
    assert tr["delta"][0] == 0
    assert len(body["sectors"]) == 3
    assert body["delta_s"] == pytest.approx(sum(s["delta_s"] for s in body["sectors"]), abs=0.002)
    assert body["lap"]["metrics"]["top_speed_kmh"] > 300
    if ref == "theoretical_best":
        assert body["reference"]["metrics"] is None
    else:
        assert body["reference"]["metrics"]["top_speed_kmh"] > 300


def test_compare_with_chosen_lap(client):
    c, key = client
    r = c.get("/api/compare", params={"session": key, "lap": 4, "ref": "lap", "ref_lap": 2})
    assert r.status_code == 200
    assert r.json()["reference"]["sources"][0]["lap_index"] == 2


def test_compare_errors(client):
    c, key = client
    out_lap = c.get("/api/compare", params={"session": key, "lap": 0})
    assert out_lap.status_code == 422 and "out laps" in out_lap.json()["detail"]
    assert c.get("/api/compare", params={"session": "nope", "lap": 1}).status_code == 404
    assert c.get("/api/compare", params={"session": key, "lap": 99}).status_code == 404


def test_live_disabled(client):
    c, _ = client
    assert c.get("/api/live").json()["receiving"] is False


def _packet(pid: int, fill) -> bytes:
    arr = np.zeros(1, dtype=f125.PACKET_DTYPES[pid])
    h = arr["header"]
    h["packet_format"] = 2025
    h["game_year"] = 25
    h["packet_id"] = pid
    h["session_uid"] = 77
    h["player_car_index"] = 2
    fill(arr)
    return arr.tobytes()


def test_live_status_from_packets():
    live = LiveStatus(port=0)

    def lap(a):
        a["car"]["current_lap_num"][0, 2] = 3
        a["car"]["lap_distance"][0, 2] = 1234.5
        a["car"]["current_lap_time_in_ms"][0, 2] = 45_678

    def tel(a):
        a["car"]["speed"][0, 2] = 287
        a["car"]["gear"][0, 2] = 7

    def sess(a):
        a["track_id"] = 11
        a["session_type"] = 18

    live.observe(_packet(PacketId.LAP_DATA, lap), now=100.0)
    live.observe(_packet(PacketId.CAR_TELEMETRY, tel), now=100.5)
    live.observe(_packet(PacketId.SESSION, sess), now=101.0)
    snap = live.snapshot(now=101.5)
    assert snap["receiving"] is True
    assert snap["lap_number"] == 3 and snap["lap_distance_m"] == 1234.5
    assert snap["speed_kmh"] == 287 and snap["gear"] == 7
    assert snap["track_name"] == "Monza" and snap["session_type_name"] == "Time Trial"
    assert live.snapshot(now=110.0)["receiving"] is False


def test_live_listener_receives_udp():
    import socket
    import time

    live = LiveStatus(port=0)
    live.start()
    try:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(_packet(PacketId.CAR_TELEMETRY, lambda a: None), ("127.0.0.1", live.port))
        tx.close()
        deadline = time.monotonic() + 3
        while not live.snapshot()["receiving"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert live.snapshot()["receiving"] is True
    finally:
        live.stop()
