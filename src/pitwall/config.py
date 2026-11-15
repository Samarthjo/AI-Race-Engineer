"""Configuration: defaults, optional TOML file, environment overrides.

Lookup order (later wins):
  1. built-in defaults
  2. config file: $PITWALL_CONFIG, else ~/.config/pitwall/config.toml (if it exists)
  3. environment: PITWALL_DATA_DIR
  4. CLI options (applied by the caller)

The ``[reference]`` table sets where real-F1 reference data is downloaded from (see
``pitwall.reference.archive``); ``{season}`` in a URL is replaced by the season. The
``[engineer]`` table chooses the local language model (Ollama) of the race engineer.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pitwall.engineer.llm import DEFAULT_MODEL, DEFAULT_URL
from pitwall.reference.archive import SCHEDULE_URL, TI_BASE_URL, TIMEOUT_S

DEFAULT_CONFIG_PATH = Path("~/.config/pitwall/config.toml").expanduser()
DEFAULT_DATA_DIR = Path("~/PitwallData").expanduser()
F1_DEFAULT_PORT = 20777


@dataclass
class CaptureConfig:
    bind_host: str = "0.0.0.0"
    port: int = F1_DEFAULT_PORT
    forward: list[str] = field(default_factory=lambda: ["127.0.0.1:20778"])
    allow_sources: list[str] = field(default_factory=list)
    rotate_seconds: float = 300.0
    rotate_mb: int = 256
    compress: bool = True
    status_interval_s: float = 5.0
    expected_hz: int = 60


@dataclass
class ReferenceConfig:
    base_url: str = TI_BASE_URL
    schedule_url: str = SCHEDULE_URL
    timeout_s: float = TIMEOUT_S


@dataclass
class EngineerConfig:
    provider: str = "ollama"
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_URL
    timeout_s: float = 180.0
    temperature: float = 0.2
    num_ctx: int = 8192
    think: bool = False


@dataclass
class Config:
    data_dir: Path = DEFAULT_DATA_DIR
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    reference: ReferenceConfig = field(default_factory=ReferenceConfig)
    engineer: EngineerConfig = field(default_factory=EngineerConfig)
    source: Path | None = None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    cfg = Config()
    file_path = path or (Path(env["PITWALL_CONFIG"]) if env.get("PITWALL_CONFIG") else None)
    if file_path is None and DEFAULT_CONFIG_PATH.exists():
        file_path = DEFAULT_CONFIG_PATH
    if file_path is not None:
        with open(file_path, "rb") as fh:
            _apply(cfg, tomllib.load(fh))
        cfg.source = file_path
    if env.get("PITWALL_DATA_DIR"):
        cfg.data_dir = Path(env["PITWALL_DATA_DIR"]).expanduser()
    return cfg


def _apply(cfg: Config, data: dict[str, Any]) -> None:
    unknown = set(data) - {"data_dir", "capture", "reference", "engineer"}
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    if "data_dir" in data:
        cfg.data_dir = Path(str(data["data_dir"])).expanduser()
    tables: tuple[tuple[str, Any], ...] = (
        ("capture", cfg.capture),
        ("reference", cfg.reference),
        ("engineer", cfg.engineer),
    )
    for table, target in tables:
        values = data.get(table, {})
        bad = set(values) - set(type(target).__dataclass_fields__)
        if bad:
            raise ValueError(f"unknown [{table}] keys: {sorted(bad)}")
        for key, value in values.items():
            setattr(target, key, value)
