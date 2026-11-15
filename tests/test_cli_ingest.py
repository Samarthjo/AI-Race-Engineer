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
    res = runner.invoke(app, ["demo", *args])
    assert res.exit_code == 0, res.output
    # the history build after the demo drafts the track's corners (a draft to confirm)
    assert "History: 1 session(s) analysed" in res.output
    res = runner.invoke(app, ["track", "list", *args])
    assert "synthetic-track" in res.output and "draft" in res.output
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


def test_demo_references_and_benchmark_cli(tmp_path):
    from typer.testing import CliRunner

    from pitwall.cli import app

    runner = CliRunner(env={"COLUMNS": "200", "PITWALL_DATA_DIR": str(tmp_path)})
    r = runner.invoke(app, ["demo"])
    assert r.exit_code == 0, r.output
    assert "synthetic reference laps" in r.output
    listing = runner.invoke(app, ["ref", "list"])
    assert "synthetic-2025-synthetic-grand-prix-qualifying-pro-l2" in listing.output
    events = runner.invoke(app, ["ref", "events", "--synthetic"])
    assert events.exit_code == 0 and "Synthetic Grand Prix" in events.output
    laps = runner.invoke(app, ["ref", "laps", "synthetic", "--synthetic"])
    assert laps.exit_code == 0 and "FST" in laps.output and "PRO" in laps.output
    one = runner.invoke(app, ["ref", "laps", "synthetic", "--synthetic", "--driver", "PRO"])
    assert "deleted" in one.output and "out lap" in one.output
    fetched = runner.invoke(app, ["ref", "fetch", "synthetic", "FST", "--synthetic"])
    assert fetched.exit_code == 0 and "Stored" in fetched.output
    bench = runner.invoke(app, ["benchmark", "latest", "--lap", "1", "--driver", "PRO"])
    assert bench.exit_code == 0, bench.output
    assert "Brakes earlier than PRO" in bench.output and "Not assessed" in bench.output
    none = runner.invoke(app, ["benchmark", "latest", "--lap", "1"])
    assert none.exit_code == 1
    wrong = runner.invoke(app, ["benchmark", "latest", "--lap", "1", "--ref", "nope"])
    assert wrong.exit_code == 1 and "Cannot benchmark" in wrong.output
