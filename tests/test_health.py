from pitwall.capture.health import HealthTracker
from pitwall.f1udp.header import PacketId
from tests.synth import HIGH_RATE, SYNTH_SESSION_UID, make_packet, stream


def _feed(tracker: HealthTracker, datagrams, source="192.168.1.50"):
    for d in datagrams:
        tracker.observe(1_700_000_000_000_000_000 + d.mono_ns, d.mono_ns, d.payload, source)


def _check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


def test_clean_stream_has_no_loss_and_correct_rate():
    h = HealthTracker(expected_hz=60)
    _feed(h, stream(range(1, 601)))  # 10 s at 60 Hz
    rep = h.report()
    assert rep["loss"]["missing_est"] == 0
    assert _check(rep, "packet_loss")["status"] == "ok"
    assert _check(rep, "telemetry_rate")["status"] == "ok"
    assert _check(rep, "udp_format")["status"] == "ok"
    tel = next(t for t in rep["packet_types"] if t["name"] == "CAR_TELEMETRY")
    assert abs(tel["rate_hz"] - 60) < 1


def test_dropped_packets_are_counted():
    drop = {(f, PacketId.CAR_TELEMETRY) for f in range(100, 110)}  # 10 missing telemetry packets
    drop |= {(200, PacketId.MOTION), (201, PacketId.MOTION)}
    h = HealthTracker()
    _feed(h, stream(range(1, 601), drop=drop))
    sess = h.sessions[SYNTH_SESSION_UID]
    assert sess.streams[PacketId.CAR_TELEMETRY].loss() == (10, 599)
    assert sess.streams[PacketId.MOTION].loss() == (2, 599)
    assert h.total_loss()[0] == 12


def test_lower_send_rate_is_not_mistaken_for_loss():
    # 30 Hz send rate: overall frame advances by 2 per packet
    h = HealthTracker(expected_hz=30)
    _feed(h, stream(range(2, 1202, 2), hz=30))
    assert h.total_loss()[0] == 0
    assert _check(h.report(), "telemetry_rate")["status"] == "ok"


def test_flashback_rewind_detected_without_counting_loss():
    h = HealthTracker()
    t = 0
    for i, frame in enumerate([*range(1, 301), *range(200, 400)]):  # rewind to frame 200
        overall = i + 1  # overall counter keeps going
        t = 1_000_000_000 + i * 16_666_667
        for pid in HIGH_RATE:
            h.observe(t, t, make_packet(pid, frame, overall=overall), "ps5")
    rep = h.report()
    assert rep["sessions"][0]["flashback_rewinds"] == 1
    assert rep["loss"]["missing_est"] == 0


def test_pause_is_a_gap_not_loss():
    before = stream(range(1, 301))
    after = stream(range(1000, 1300), start_ns=before[-1].mono_ns + 30_000_000_000)
    h = HealthTracker()
    _feed(h, before + after)
    rep = h.report()
    assert rep["loss"]["missing_est"] == 0
    assert rep["gaps"]["count"] == 1
    assert 29 < rep["gaps"]["largest"][0]["duration_s"] < 31
    assert 9 < rep["active_s"] < 11  # pause excluded from active time


def test_new_session_tracked_separately():
    h = HealthTracker()
    _feed(h, stream(range(1, 301)))
    _feed(h, stream(range(1, 301), start_ns=20_000_000_000, session_uid=0xABC))
    assert set(h.sessions) == {SYNTH_SESSION_UID, 0xABC}
    assert h.total_loss()[0] == 0


def test_wrong_format_and_malformed_flagged():
    h = HealthTracker()
    for i in range(100):
        h.observe(i, i * 16_666_667, make_packet(PacketId.CAR_TELEMETRY, i, packet_format=2026))
    h.observe(0, 2_000_000_000, b"short")
    rep = h.report()
    assert _check(rep, "udp_format")["status"] == "warn"
    assert _check(rep, "malformed")["status"] == "warn"
    assert rep["malformed"] == 1


def test_no_packets_fails():
    rep = HealthTracker().report()
    assert _check(rep, "packets_received")["status"] == "fail"


def test_interval_stats_reset_each_call():
    h = HealthTracker()
    data = stream(range(1, 121))
    _feed(h, data)
    iv = h.interval(data[-1].mono_ns)
    assert iv.packets == len(data)
    assert 55 < iv.telemetry_hz < 65
    assert iv.packet_format == 2025
    iv2 = h.interval(data[-1].mono_ns + 1_000_000_000)
    assert iv2.packets == 0
