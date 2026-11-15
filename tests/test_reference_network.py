"""Real-data check of the reference layer (opt-in: needs the internet).

Run with ``PITWALL_NETWORK_TESTS=1 uv run pytest tests/test_reference_network.py``. It
downloads Max Verstappen's 2025 Italian Grand Prix pole lap from the reference archive and
checks it against the official result (1:18.792, the fastest lap in F1 history at the time).
"""

from __future__ import annotations

import os

import pytest

from pitwall.reference.laps import classification, parse_lap_rows
from pitwall.reference.service import fetch_reference, make_archive
from pitwall.storage import db

pytestmark = pytest.mark.skipif(
    os.environ.get("PITWALL_NETWORK_TESTS") != "1", reason="set PITWALL_NETWORK_TESTS=1"
)


def test_monza_2025_pole_lap(tmp_path):
    archive = make_archive(tmp_path, 2025)
    event = archive.find_event("monza")
    assert event.name == "Italian Grand Prix"
    rows = classification(parse_lap_rows(archive.session_laptimes(event.name, "Qualifying")))
    assert [r.driver for r in rows[:3]] == ["VER", "NOR", "PIA"]
    conn = db.connect(tmp_path / "pitwall.db")
    ref = fetch_reference(conn, tmp_path, archive, event.name, "Qualifying", "VER")
    assert ref.lap_time_ms == 78_792 and ref.session_part == "Q3"
    assert sum(s for s in ref.sectors_ms if s is not None) == 78_792
    assert ref.capabilities.brake == "binary"
    assert 0.2 <= ref.capabilities.car_interval_s <= 0.3
    assert ref.checks["path_length_m"] == pytest.approx(5700, rel=0.03)
