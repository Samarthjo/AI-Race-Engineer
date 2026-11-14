from pathlib import Path

from typer.testing import CliRunner

from pitwall.cli import app
from pitwall.synth.session import LapPlan, SessionSynth
from tests.test_ingest import write_capture

runner = CliRunner(env={"COLUMNS": "200"})  # a typical terminal; 80 squeezes the lap table


def _data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    s = SessionSynth().run(
        [LapPlan(6.0, kind="out"), LapPlan(12.0), LapPlan(12.5, flashback_at=0.5)]
    )
    write_capture(data / "raw", "20261114T120000Z", s.records)
    return data


def test_ingest_sessions_laps_export_decode(tmp_path: Path):
    data = _data_dir(tmp_path)
    args = ["--data-dir", str(data)]

    res = runner.invoke(app, ["ingest", *args])
    assert res.exit_code == 0, res.output
    assert "Monza" in res.output and "Time Trial" in res.output

    res = runner.invoke(app, ["ingest", *args])
    assert "Nothing new to ingest" in res.output

    res = runner.invoke(app, ["ingest", "--force", *args])
    assert res.exit_code == 0 and "Monza" in res.output

    res = runner.invoke(app, ["sessions", *args])
    assert res.exit_code == 0, res.output
    assert "9abcdef0" in res.output  # last 8 hex digits of the session uid

    res = runner.invoke(app, ["laps", "9abcdef0", *args])
    assert res.exit_code == 0, res.output
    assert "12.000" in res.output and "flashback" in res.output

    res = runner.invoke(app, ["laps", "latest", *args])
    assert res.exit_code == 0, res.output

    out = tmp_path / "lap1.csv"
    res = runner.invoke(app, ["export", "latest", "--lap", "1", "--out", str(out), *args])
    assert res.exit_code == 0, res.output
    lines = out.read_text().splitlines()
    assert len(lines) == 1 + 720  # header + 12 s at 60 Hz
    assert "speed" in lines[0] and "lap_distance" in lines[0]

    res = runner.invoke(
        app, ["decode", "20261114T120000Z", "--packet", "car_telemetry", "--skip", "100", *args]
    )
    assert res.exit_code == 0, res.output
    assert '"speed": 300' in res.output


def test_laps_unknown_session(tmp_path: Path):
    res = runner.invoke(app, ["laps", "nope", "--data-dir", str(tmp_path)])
    assert res.exit_code == 1


def test_demo_and_compare(tmp_path: Path):
    args = ["--data-dir", str(tmp_path / "d")]
    res = runner.invoke(app, ["demo", *args])
    assert res.exit_code == 0, res.output
    assert "synthetic" in res.output
    res = runner.invoke(
        app, ["compare", "latest", "--lap", "4", "--ref", "theoretical_best", *args]
    )
    assert res.exit_code == 0, res.output
    assert "theoretical best" in res.output and "Largest losses" in res.output
    res = runner.invoke(app, ["compare", "latest", "--lap", "0", *args])
    assert res.exit_code == 1 and "Cannot compare" in res.output
    res = runner.invoke(
        app, ["compare", "latest", "--lap", "4", "--ref", "lap", "--ref-lap", "2", *args]
    )
    assert res.exit_code == 0, res.output


def test_corners_and_track_commands(tmp_path: Path):
    args = ["--data-dir", str(tmp_path / "d")]
    assert runner.invoke(app, ["demo", *args]).exit_code == 0
    res = runner.invoke(app, ["track", "list", *args])
    assert "No track models yet" in res.output
    res = runner.invoke(app, ["corners", "latest", "--lap", "4", *args])
    assert res.exit_code == 0, res.output
    assert "Corners (lap / reference)" in res.output and "Findings" in res.output
    assert "Later on the throttle" in res.output
    res = runner.invoke(app, ["track", "list", *args])
    assert "synthetic-track" in res.output and "draft" in res.output
    res = runner.invoke(app, ["track", "show", "latest", *args])
    assert res.exit_code == 0 and "c1" in res.output
    res = runner.invoke(app, ["track", "rename", "latest", "c1", "Hairpin", *args])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["track", "draft", "latest", *args])
    assert res.exit_code == 1 and "--force" in res.output
    res = runner.invoke(app, ["track", "draft", "latest", "--force", *args])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["track", "confirm", "latest", *args])
    assert res.exit_code == 0 and "confirmed" in res.output
