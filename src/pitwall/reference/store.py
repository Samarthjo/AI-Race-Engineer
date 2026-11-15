"""Normalised reference laps on disk: samples in Parquet, catalogue rows in SQLite.

``<data dir>/references/raw/``      downloaded files, byte-for-byte (never modified)
``<data dir>/references/laps/``     one Parquet file per reference lap (measured samples)

Both derived parts can be rebuilt from the raw cache. A stored lap made by an older
``REFERENCE_PIPELINE_VERSION`` is rebuilt automatically the next time it is used.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from pitwall.reference.laps import Capabilities, RefLap, Samples
from pitwall.storage import db

REFERENCE_PIPELINE_VERSION = "1.0.0"
CAR_COLUMNS = ("speed", "throttle", "brake", "gear", "rpm", "drs", "car_distance")
POS_COLUMNS = ("x", "y", "z")


def refs_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "references"


def raw_dir(data_dir: Path) -> Path:
    return refs_dir(data_dir) / "raw"


def samples_rel_path(key: str) -> str:
    return f"references/laps/{key}.parquet"


def _write_samples(path: Path, s: Samples) -> None:
    n_car, n_pos = len(s.car_t), len(s.pos_t)
    nan_car, nan_pos = np.full(n_pos, np.nan), np.full(n_car, np.nan)
    cols: dict[str, Any] = {
        "stream": ["car"] * n_car + ["pos"] * n_pos,
        "t_s": np.concatenate([s.car_t, s.pos_t]),
    }
    for c in CAR_COLUMNS:
        cols[c] = np.concatenate([getattr(s, c), nan_car])
    for c in POS_COLUMNS:
        cols[c] = np.concatenate([nan_pos, getattr(s, c)])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    pq.write_table(pa.table(cols), tmp)
    os.replace(tmp, path)


def _read_samples(path: Path) -> Samples:
    t = pq.read_table(path)
    stream = np.asarray(t.column("stream").to_pylist())
    car, pos = stream == "car", stream == "pos"

    def col(name: str, sel: np.ndarray) -> np.ndarray:
        return np.asarray(t.column(name).to_numpy(zero_copy_only=False), dtype=np.float64)[sel]

    return Samples(
        car_t=col("t_s", car),
        **{c: col(c, car) for c in CAR_COLUMNS},
        pos_t=col("t_s", pos),
        **{c: col(c, pos) for c in POS_COLUMNS},
    )


def save_ref_lap(conn: sqlite3.Connection, data_dir: Path, lap: RefLap) -> None:
    rel = samples_rel_path(lap.key)
    _write_samples(Path(data_dir) / rel, lap.samples)
    db.replace_ref_lap(
        conn,
        {
            "ref_key": lap.key,
            "provider": lap.provider,
            "synthetic": int(lap.synthetic),
            "season": lap.season,
            "event": lap.event,
            "session": lap.session,
            "session_part": lap.session_part,
            "driver": lap.driver,
            "driver_number": lap.driver_number,
            "driver_name": lap.driver_name,
            "team": lap.team,
            "lap_number": lap.lap_number,
            "lap_time_ms": lap.lap_time_ms,
            "s1_ms": lap.sectors_ms[0],
            "s2_ms": lap.sectors_ms[1],
            "s3_ms": lap.sectors_ms[2],
            "compound": lap.compound,
            "tyre_life": lap.tyre_life,
            "track_status": lap.track_status,
            "capabilities_json": json.dumps(lap.capabilities.to_dict(), sort_keys=True),
            "checks_json": json.dumps(lap.checks, sort_keys=True),
            "sources_json": json.dumps(lap.sources, sort_keys=True),
            "samples_path": rel,
            "fetched_utc": lap.fetched_utc,
            "pipeline_version": REFERENCE_PIPELINE_VERSION,
        },
    )


def lap_from_row(data_dir: Path, row: dict[str, Any]) -> RefLap:
    return RefLap(
        key=row["ref_key"],
        provider=row["provider"],
        synthetic=bool(row["synthetic"]),
        season=row["season"],
        event=row["event"],
        session=row["session"],
        session_part=row["session_part"],
        driver=row["driver"],
        driver_number=row["driver_number"],
        driver_name=row["driver_name"],
        team=row["team"],
        lap_number=row["lap_number"],
        lap_time_ms=row["lap_time_ms"],
        sectors_ms=(row["s1_ms"], row["s2_ms"], row["s3_ms"]),
        compound=row["compound"],
        tyre_life=row["tyre_life"],
        track_status=row["track_status"],
        capabilities=Capabilities.from_dict(json.loads(row["capabilities_json"])),
        checks=json.loads(row["checks_json"]),
        sources=json.loads(row["sources_json"]),
        fetched_utc=row["fetched_utc"],
        samples=_read_samples(Path(data_dir) / row["samples_path"]),
    )


def ref_lap_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Catalogue row for listings and the API (no samples)."""
    return {
        "key": row["ref_key"],
        "provider": row["provider"],
        "synthetic": bool(row["synthetic"]),
        "season": row["season"],
        "event": row["event"],
        "session": row["session"],
        "session_part": row["session_part"],
        "driver": row["driver"],
        "driver_number": row["driver_number"],
        "driver_name": row["driver_name"],
        "team": row["team"],
        "lap_number": row["lap_number"],
        "lap_time_ms": row["lap_time_ms"],
        "sectors_ms": [row["s1_ms"], row["s2_ms"], row["s3_ms"]],
        "compound": row["compound"],
        "tyre_life": row["tyre_life"],
        "track_status": row["track_status"],
        "capabilities": json.loads(row["capabilities_json"]),
        "checks": json.loads(row["checks_json"]),
        "fetched_utc": row["fetched_utc"],
        "current": row["pipeline_version"] == REFERENCE_PIPELINE_VERSION,
    }
