from pathlib import Path

import pytest
from typer.testing import CliRunner

from pitwall.capture.rawlog import RotatingRawLogWriter
from pitwall.capture.receiver import parse_endpoint
from pitwall.cli import app
from pitwall.config import load_config
from tests.synth import stream

runner = CliRunner()


def test_config_file_and_env(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'data_dir = "/tmp/pw"\n[capture]\nport = 20999\nforward = []\nexpected_hz = 30\n'
    )
    cfg = load_config(cfg_file, env={})
    assert cfg.data_dir == Path("/tmp/pw")
    assert cfg.capture.port == 20999
    assert cfg.capture.forward == []
    assert cfg.capture.expected_hz == 30
    cfg = load_config(cfg_file, env={"PITWALL_DATA_DIR": str(tmp_path / "d")})
    assert cfg.data_dir == tmp_path / "d"


def test_config_rejects_unknown_keys(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[capture]\nprot = 1\n")
    with pytest.raises(ValueError, match="prot"):
        load_config(cfg_file, env={})


@pytest.mark.parametrize("bad", ["20777", "host:", ":20777", "h:0", "h:70000", "h:x"])
def test_parse_endpoint_rejects_bad(bad):
    with pytest.raises(ValueError):
        parse_endpoint(bad)


def test_parse_endpoint_ok():
    assert parse_endpoint("127.0.0.1:20778") == ("127.0.0.1", 20778)


def _make_capture(raw_dir: Path, name: str = "20260101T000000Z") -> Path:
    d = raw_dir / name
    w = RotatingRawLogWriter(d, {"capture_id": name}, rotate_bytes=50_000)
    for dg in stream(range(1, 301)):
        w.write(1_700_000_000_000_000_000 + dg.mono_ns, dg.mono_ns, dg.payload)
    w.close()
    return d


def test_cli_stats_list_compact(tmp_path: Path):
    raw = tmp_path / "data" / "raw"
    d = _make_capture(raw)
    args = ["--data-dir", str(tmp_path / "data")]

    res = runner.invoke(app, ["capture", "stats", d.name, *args])
    assert res.exit_code == 0, res.output
    assert "CAR_TELEMETRY" in res.output

    res = runner.invoke(app, ["capture", "stats", str(d), "--json"])
    assert res.exit_code == 0, res.output
    assert '"packets": 1500' in res.output

    res = runner.invoke(app, ["capture", "list", *args])
    assert res.exit_code == 0, res.output
    assert d.name in res.output

    res = runner.invoke(app, ["capture", "compact", d.name, *args])
    assert res.exit_code == 0, res.output
    assert not list(d.glob("*.pwraw"))
    assert list(d.glob("*.pwraw.zst"))


def test_cli_missing_capture(tmp_path: Path):
    res = runner.invoke(app, ["capture", "stats", "nope", "--data-dir", str(tmp_path)])
    assert res.exit_code == 1


def test_cli_version():
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0
    assert "pitwall" in res.output


def test_example_config_is_valid():
    example = Path(__file__).parent.parent / "docs" / "config.example.toml"
    cfg = load_config(example, env={})
    assert cfg.capture.port == 20777
    assert cfg.capture.expected_hz == 60
