"""Stage 4 reference data: archive access through the raw cache, lap tables, telemetry
checks and normalisation, the store, and the pairing rules (era, track, synthetic)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from pitwall.analysis.trace import NotComparableError
from pitwall.reference.archive import FetchError, RawCache, http_get, parse_schedule
from pitwall.reference.laps import (
    ReferenceDataError,
    classification,
    fastest_usable,
    parse_lap_rows,
    parse_telemetry,
    ref_key,
    slug,
)
from pitwall.reference.service import (
    SYNTHETIC_EVENT,
    check_pairing,
    choose_lap,
    event_for,
    fetch_reference,
    load_reference,
    make_archive,
    season_for,
    synthetic_archive_dir,
)
from pitwall.storage import db
from pitwall.synth.pro import write_archive


@pytest.fixture(scope="module")
def archive_root(tmp_path_factory) -> Path:
    data = tmp_path_factory.mktemp("refdata")
    write_archive(synthetic_archive_dir(data))
    return data


def _archive(data: Path, **kw):
    return make_archive(data, 2025, synthetic=True, **kw)


# ------------------------------------------------------------------ lap tables


def test_lap_rows_and_choosing_the_fastest_valid_lap(archive_root):
    ar = _archive(archive_root)
    rows = parse_lap_rows(ar.driver_laptimes(SYNTHETIC_EVENT, "Qualifying", "PRO"), "PRO")
    assert [r.lap_number for r in rows] == [1, 2, 3, 4]
    out_lap, flying, in_lap, deleted = rows
    assert "out lap" in out_lap.issues and "no lap time" in out_lap.issues
    assert flying.usable and flying.session_part == "Q1"
    assert "in lap" in in_lap.issues
    assert deleted.lap_time_ms is not None and deleted.lap_time_ms < (flying.lap_time_ms or 0)
    assert deleted.issues == ["deleted (TRACK LIMITS (synthetic))"]
    # the faster lap was deleted: the fastest *valid* lap is the flying lap
    assert fastest_usable(rows) == flying
    assert fastest_usable(rows, part="Q3") is None
    with pytest.raises(ReferenceDataError, match="deleted"):
        choose_lap(rows, 4, None)
    with pytest.raises(LookupError):
        choose_lap(rows, 9, None)
    everyone = parse_lap_rows(ar.session_laptimes(SYNTHETIC_EVENT, "Qualifying"))
    cls = classification(everyone)
    assert [r.driver for r in cls] == ["FST", "PRO"]  # the faster car is faster


def test_missing_values_and_accuracy_not_reported():
    rows = parse_lap_rows({"lap": [1], "time": [80.5], "drv": ["XYZ"]})
    assert rows[0].lap_time_ms == 80500
    assert rows[0].issues == ["timing accuracy not reported"]
    with pytest.raises(ReferenceDataError):
        parse_lap_rows({"lap": [1, 2], "time": [80.5]})
    with pytest.raises(ReferenceDataError):
        parse_lap_rows({"lap": [1], "time": [80.5], "s1": [1, 2]})


def test_keys_are_safe_slugs():
    assert slug("São Paulo Grand Prix") == "sao-paulo-grand-prix"
    key = ref_key("tracinginsights", 2025, "Italian Grand Prix", "Qualifying", "VER", 17)
    assert key == "tracinginsights-2025-italian-grand-prix-qualifying-ver-l17"


# ------------------------------------------------------------------ telemetry


def _tel(ar, driver="PRO") -> dict:
    return ar.telemetry(SYNTHETIC_EVENT, "Qualifying", driver, 2)


def _lap_time(ar, driver="PRO") -> float:
    rows = parse_lap_rows(ar.driver_laptimes(SYNTHETIC_EVENT, "Qualifying", driver), driver)
    ms = rows[1].lap_time_ms
    assert ms is not None
    return ms / 1000


def test_only_measured_samples_are_kept(archive_root):
    ar = _archive(archive_root)
    tel, lap_time = _tel(ar), _lap_time(ar)
    samples, caps, checks = parse_telemetry(tel, lap_time)
    n = len(tel["time"])
    assert caps.car_samples + caps.pos_samples < n  # the interpolated rows are dropped
    assert 0.2 <= caps.car_interval_s <= 0.28 and caps.car_max_gap_s > 0.5
    assert caps.brake == "binary" and not caps.steering and not caps.wheel_slip
    assert set(np.unique(samples.brake)) <= {0.0, 1.0}
    assert samples.throttle.max() <= 1.0
    # interpolated lap-start and lap-end rows are not samples
    assert samples.car_t[0] > 0 and samples.pos_t[0] > 0
    assert samples.car_t[-1] < lap_time and samples.pos_t[-1] < lap_time
    assert np.all(np.diff(samples.car_t) > 0) and np.all(np.diff(samples.pos_t) > 0)
    assert checks.end_s == pytest.approx(lap_time, abs=1e-6)
    assert checks.path_length_m == pytest.approx(checks.integrated_length_m, rel=0.03)


def test_telemetry_checks_refuse_bad_data(archive_root):
    ar = _archive(archive_root)
    tel, lap_time = _tel(ar), _lap_time(ar)
    with pytest.raises(ReferenceDataError, match="official lap time"):
        parse_telemetry(tel, lap_time + 0.5)
    pressure = dict(tel, brake=[v * 0.5 + 0.25 for v in tel["brake"]])
    with pytest.raises(ReferenceDataError):
        parse_telemetry(pressure, lap_time)
    metres = dict(tel, x=[v * 10 for v in tel["x"]], y=[v * 10 for v in tel["y"]])
    with pytest.raises(ReferenceDataError, match="position units"):
        parse_telemetry(metres, lap_time)
    smooth = dict(tel, speed=[v + 0.37 for v in tel["speed"]])
    with pytest.raises(ReferenceDataError, match="format not recognised"):
        parse_telemetry(smooth, lap_time)
    with pytest.raises(ReferenceDataError, match="no 'gear'"):
        parse_telemetry({k: v for k, v in tel.items() if k != "gear"}, lap_time)


# ------------------------------------------------------------------ cache and store


def test_raw_cache_keeps_files_and_works_offline(tmp_path, archive_root):
    calls: list[str] = []
    src = synthetic_archive_dir(archive_root)

    def fetch(url: str) -> bytes:
        calls.append(url)
        return http_get(url)

    url = (src / "schedule_2025.json").resolve().as_uri()
    cache = RawCache(tmp_path / "raw", fetch=fetch)
    first = cache.get(("x", "schedule.json"), url)
    second = cache.get(("x", "schedule.json"), url)
    assert first == second and len(calls) == 1
    meta = cache.source(("x", "schedule.json"))
    assert meta is not None and meta["url"] == url and len(meta["sha256"]) == 64
    offline = RawCache(tmp_path / "raw", fetch=fetch, offline=True)
    assert offline.get(("x", "schedule.json"), url) == first
    with pytest.raises(FetchError, match="offline"):
        offline.get(("x", "other.json"), url)
    with pytest.raises(ValueError):
        cache.path(("..", "evil"))


def test_schedule_and_event_lookup(archive_root):
    ar = _archive(archive_root)
    (event,) = ar.events()
    assert event.name == SYNTHETIC_EVENT and event.sessions == ("Qualifying",)
    assert ar.find_event("synthetic").name == SYNTHETIC_EVENT
    with pytest.raises(LookupError):
        ar.find_event("monza")
    real = parse_schedule(
        {
            "round_number": {"0": 0, "1": 16},
            "event_name": {"0": "Pre-Season Testing", "1": "Italian Grand Prix"},
            "location": {"0": "Sakhir", "1": "Monza"},
            "country": {"0": "Bahrain", "1": "Italy"},
            "event_format": {"0": "testing", "1": "conventional"},
            "event_date": {"0": "2025-02-28T19:00:00", "1": "2025-09-07T15:00:00"},
            "session4": {"0": None, "1": "Qualifying"},
            "session5": {"0": None, "1": "Race"},
        }
    )
    assert [e.name for e in real] == ["Italian Grand Prix"]  # testing is skipped
    assert real[0].sessions == ("Qualifying", "Race") and real[0].date == "2025-09-07"


def test_fetch_store_and_reload(tmp_path, archive_root):
    data = tmp_path / "d"
    ar = make_archive(
        data,
        2025,
        synthetic=True,
        base_url=synthetic_archive_dir(archive_root).resolve().as_uri() + "/{season}/",
        schedule_url=synthetic_archive_dir(archive_root).resolve().as_uri()
        + "/schedule_{season}.json",
    )
    conn = db.connect(data / "pitwall.db")
    ref = fetch_reference(conn, data, ar, SYNTHETIC_EVENT, "Qualifying", "pro")
    assert ref.key == "synthetic-2025-synthetic-grand-prix-qualifying-pro-l2"
    assert ref.synthetic and ref.driver == "PRO" and ref.driver_name == "Synthetic Pro"
    assert ref.lap_time_ms == round(_lap_time(ar) * 1000)
    assert sum(s for s in ref.sectors_ms if s is not None) == pytest.approx(ref.lap_time_ms, abs=2)
    assert [s["parts"][-1] for s in ref.sources] == ["laptimes.json", "2_tel.json"]
    assert all(len(s["sha256"]) == 64 for s in ref.sources)
    rows = db.list_ref_laps(conn)
    assert [r["ref_key"] for r in rows] == [ref.key]
    again = load_reference(conn, data, ref.key)
    np.testing.assert_array_equal(again.samples.car_t, ref.samples.car_t)
    np.testing.assert_array_equal(again.samples.x, ref.samples.x)
    assert again.capabilities == ref.capabilities
    # a lap stored by older code is rebuilt from the raw cache, without the network
    conn.execute("UPDATE ref_laps SET pipeline_version = '0.0.1'")
    conn.commit()
    rebuilt = load_reference(conn, data, ref.key)
    np.testing.assert_array_equal(rebuilt.samples.speed, ref.samples.speed)
    row = db.get_ref_lap(conn, ref.key)
    assert row is not None and row["pipeline_version"] != "0.0.1"
    with pytest.raises(LookupError):
        load_reference(conn, data, "nope")


# ------------------------------------------------------------------ pairing rules


def _session(**kw):
    s = {
        "packet_format": 2025,
        "formula": 0,
        "track_id": 11,
        "track_name": "Monza",
        "synthetic": 0,
    }
    s.update(kw)
    return s


def test_era_and_track_guards(tmp_path, archive_root):
    assert season_for(_session()) == 2025
    with pytest.raises(NotComparableError, match="format"):
        season_for(_session(packet_format=2024))
    assert event_for(_session()) == "Italian Grand Prix"
    assert event_for(_session(synthetic=1, track_id=-1)) == SYNTHETIC_EVENT
    with pytest.raises(NotComparableError, match="no real-F1 reference"):
        event_for(_session(track_id=39, track_name="Silverstone (reverse)"))

    data = tmp_path / "d"
    conn = db.connect(data / "pitwall.db")
    ar = make_archive(
        data,
        2025,
        synthetic=True,
        base_url=synthetic_archive_dir(archive_root).resolve().as_uri() + "/{season}/",
    )
    ref = fetch_reference(conn, data, ar, SYNTHETIC_EVENT, "Qualifying", "PRO")
    check_pairing(_session(synthetic=1, track_id=-1, track_name="Synthetic test track"), ref)
    with pytest.raises(NotComparableError, match="synthetic"):
        check_pairing(_session(), ref)
    ref.synthetic = False
    with pytest.raises(NotComparableError, match="Italian Grand Prix"):
        check_pairing(_session(), ref)
    ref.event, ref.season = "Italian Grand Prix", 2024
    with pytest.raises(NotComparableError, match="era guard"):
        check_pairing(_session(), ref)
    ref.season = 2025
    check_pairing(_session(), ref)
    with pytest.raises(NotComparableError, match="F1 Modern"):
        check_pairing(_session(formula=1), ref)


# ------------------------------------------------------------------ HTTP errors


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        code = {"/missing": 404, "/denied": 403}.get(self.path, 200)
        self.send_response(code)
        self.end_headers()
        if code == 200:
            self.wfile.write(json.dumps({"ok": True}).encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_http_errors_are_explained():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.loads(http_get(base + "/ok")) == {"ok": True}
        with pytest.raises(FetchError, match="not in the archive"):
            http_get(base + "/missing")
        with pytest.raises(FetchError, match="refused"):
            http_get(base + "/denied")
    finally:
        server.shutdown()
    with pytest.raises(FetchError, match="cannot reach"):
        http_get("http://127.0.0.1:9/nothing", timeout=2)
    with pytest.raises(FetchError, match="unsupported"):
        http_get("ftp://example.invalid/x")
