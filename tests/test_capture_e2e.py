"""End-to-end over real loopback sockets: send → capture → raw log + forward → replay."""

import socket
import threading
import time
from pathlib import Path

from pitwall.capture.rawlog import iter_capture
from pitwall.capture.replay import replay
from pitwall.capture.session import CaptureRun, analyse_capture, read_manifest
from pitwall.config import CaptureConfig
from tests.synth import stream


def _listener() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.5)
    return s, s.getsockname()[1]


def _drain(sock: socket.socket, expected: int, timeout: float = 5.0) -> list[bytes]:
    got: list[bytes] = []
    deadline = time.monotonic() + timeout
    while len(got) < expected and time.monotonic() < deadline:
        try:
            got.append(sock.recv(65535))
        except TimeoutError:
            continue
    return got


def _send_paced(payloads: list[bytes], port: int) -> None:
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i, p in enumerate(payloads):
        tx.sendto(p, ("127.0.0.1", port))
        if i % 50 == 49:
            time.sleep(0.002)  # stay well inside loopback buffers
    tx.close()


def test_capture_stores_forwards_and_reports(tmp_path: Path):
    fwd_sock, fwd_port = _listener()
    payloads = [d.payload for d in stream(range(1, 121))]  # 600 datagrams
    settings = CaptureConfig(
        bind_host="127.0.0.1",
        port=0,
        forward=[f"127.0.0.1:{fwd_port}"],
        rotate_seconds=3600,
        compress=True,
        status_interval_s=0.1,
    )
    statuses = []
    run = CaptureRun(
        tmp_path / "raw", settings, label="e2e", on_status=lambda s, c: statuses.append(s)
    )
    run.prepare()
    assert run.capture is not None
    port = run.capture.bound_address[1]
    thread = threading.Thread(target=run.run)
    thread.start()
    forwarded: list[bytes] = []
    drainer = threading.Thread(target=lambda: forwarded.extend(_drain(fwd_sock, len(payloads))))
    drainer.start()
    try:
        _send_paced(payloads, port)
        drainer.join(10)
        deadline = time.monotonic() + 5
        while run.capture.counters.stored < len(payloads) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        run.stop_event.set()
        thread.join(10)
        fwd_sock.close()

    assert forwarded == payloads  # byte-identical, in order
    stored = [r.payload for r in iter_capture(run.directory)]
    assert stored == payloads
    assert all(p.name.endswith(".pwraw.zst") for p in run.directory.glob("seg_*"))

    manifest = read_manifest(run.directory)
    assert manifest is not None
    assert manifest["status"] == "complete"
    assert manifest["label"] == "e2e"
    assert manifest["counters"]["stored"] == len(payloads)
    assert manifest["health"]["packets"] == len(payloads)
    assert manifest["health"]["loss"]["missing_est"] == 0
    assert statuses, "status callback never fired"

    health, summary = analyse_capture(run.directory)
    assert health.packets == len(payloads)
    assert summary.truncated_segments == []


def test_replay_reproduces_stream(tmp_path: Path):
    payloads = [d.payload for d in stream(range(1, 41))]
    # write a capture by capturing a paced send
    settings = CaptureConfig(bind_host="127.0.0.1", port=0, forward=[], compress=False)
    run = CaptureRun(tmp_path / "raw", settings)
    run.prepare()
    assert run.capture is not None
    thread = threading.Thread(target=run.run)
    thread.start()
    _send_paced(payloads, run.capture.bound_address[1])
    deadline = time.monotonic() + 5
    while run.capture.counters.stored < len(payloads) and time.monotonic() < deadline:
        time.sleep(0.02)
    run.stop_event.set()
    thread.join(10)

    rx, port = _listener()
    got: list[bytes] = []
    drainer = threading.Thread(target=lambda: got.extend(_drain(rx, len(payloads))))
    drainer.start()
    result = replay(iter_capture(run.directory), ("127.0.0.1", port), speed=0)
    drainer.join(10)
    rx.close()
    assert result.sent == len(payloads)
    assert got == payloads
