from __future__ import annotations

from pathlib import Path

import pytest

from pitwall.analysis.trace import NotComparableError
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.storage import db
from pitwall.synth.demo import write_demo_capture
from pitwall.track.model import (
    Segment,
    TrackModel,
    confirm_model,
    list_models,
    load_model,
    model_path,
    rename_segment,
    save_model,
    track_key,
)
from pitwall.track.service import model_for_session


def _model(**kw) -> TrackModel:
    segs = [
        Segment("s0", "Straight to C1", "straight", 0, 100),
        Segment("c1", "C1", "corner", 100, 200, [150]),
        Segment("s1", "Straight to the line", "straight", 200, 1000),
    ]
    d = dict(
        key="track11-monza",
        track_id=11,
        track_name="Monza",
        length_m=1000.0,
        status="draft",
        version=1,
        synthetic=False,
        source={},
        segments=segs,
    )
    d.update(kw)
    return TrackModel(**d)  # type: ignore[arg-type]


def test_keys():
    assert track_key(11, "Monza", False) == "track11-monza"
    assert track_key(-1, "Synthetic test track", True) == "synthetic-track-1-synthetic-test-track"
    with pytest.raises(ValueError):
        model_path(Path("/tmp"), "../evil")


def test_validation():
    assert _model().validate() == []
    gap = _model()
    gap.segments[1].start_m = 110
    assert any("gap" in e for e in gap.validate())
    bad_apex = _model()
    bad_apex.segments[1].apexes_m = [500]
    assert any("apex" in e for e in bad_apex.validate())
    short = _model(length_m=1200.0)
    assert any("track length" in e for e in short.validate())


def test_save_load_confirm_rename(tmp_path: Path):
    m = _model()
    save_model(tmp_path, m)
    loaded = load_model(tmp_path, "track11-monza")
    assert loaded is not None and loaded.segments[1].apexes_m == [150]
    c = confirm_model(tmp_path, "track11-monza")
    assert c.status == "confirmed" and c.version == 2
    assert confirm_model(tmp_path, "track11-monza").version == 2  # idempotent
    r = rename_segment(tmp_path, "track11-monza", "c1", "Rettifilo")
    assert r.segments[1].name == "Rettifilo" and r.version == 3
    with pytest.raises(KeyError):
        rename_segment(tmp_path, "track11-monza", "c9", "x")
    assert [m.key for m in list_models(tmp_path)] == ["track11-monza"]
    with pytest.raises(ValueError):
        save_model(tmp_path, _model(status="final"))


def test_model_for_session_drafts_once_and_guards_length(tmp_path: Path):
    data = tmp_path / "d"
    cap = write_demo_capture(data / "raw")
    conn = db.connect(data / DB_NAME)
    res = ingest_capture(cap, data, conn)
    session = dict(db.get_session(conn, res.sessions[0].session_key) or {})
    m = model_for_session(conn, data, session)
    assert m.status == "draft" and m.synthetic and m.key.startswith("synthetic-")
    assert len(m.corners) == 6
    assert load_model(data, m.key) is not None
    confirm_model(data, m.key)
    again = model_for_session(conn, data, session)
    assert again.status == "confirmed"  # an existing model is reused, not re-drafted
    session_other = dict(session, track_length_m=4000)
    with pytest.raises(NotComparableError, match="re-draft"):
        model_for_session(conn, data, session_other)
