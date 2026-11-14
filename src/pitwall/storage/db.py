"""SQLite catalogue of captures, sessions and laps (data layer L2 metadata).

Schema changes are applied as numbered migrations tracked with ``PRAGMA user_version``.
Everything here can be rebuilt from the raw captures with ``pitwall ingest --force``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE captures (
        capture_id       TEXT PRIMARY KEY,
        path             TEXT NOT NULL,
        label            TEXT,
        note             TEXT,
        started_utc      TEXT,
        ingested_utc     TEXT NOT NULL,
        datagrams        INTEGER NOT NULL,
        decoded          INTEGER NOT NULL,
        decode_errors    TEXT NOT NULL,
        pipeline_version TEXT NOT NULL
    );
    CREATE TABLE sessions (
        session_key        TEXT PRIMARY KEY,
        capture_id         TEXT NOT NULL REFERENCES captures(capture_id) ON DELETE CASCADE,
        session_uid        TEXT NOT NULL,
        packet_format      INTEGER,
        game_version       TEXT,
        track_id           INTEGER,
        track_name         TEXT,
        track_length_m     INTEGER,
        session_type       INTEGER,
        session_type_name  TEXT,
        game_mode          INTEGER,
        game_mode_name     TEXT,
        formula            INTEGER,
        weather            INTEGER,
        track_temp_c       INTEGER,
        air_temp_c         INTEGER,
        sector2_start_m    REAL,
        sector3_start_m    REAL,
        player_car_index   INTEGER,
        team_id            INTEGER,
        first_utc          TEXT,
        last_utc           TEXT,
        packets            INTEGER,
        lap_count          INTEGER,
        complete_laps      INTEGER,
        valid_laps         INTEGER,
        usable_laps        INTEGER,
        best_usable_lap_ms INTEGER,
        best_valid_lap_ms  INTEGER,
        flashbacks         INTEGER,
        timeline_resets    INTEGER,
        assists_json       TEXT,
        settings_json      TEXT,
        frames_path        TEXT NOT NULL,
        details_path       TEXT NOT NULL,
        pipeline_version   TEXT NOT NULL
    );
    CREATE INDEX sessions_capture ON sessions(capture_id);
    CREATE TABLE laps (
        session_key         TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
        lap_index           INTEGER NOT NULL,
        lap_number          INTEGER NOT NULL,
        segment             INTEGER NOT NULL,
        kind                TEXT NOT NULL,
        complete            INTEGER NOT NULL,
        valid               INTEGER,
        usable              INTEGER NOT NULL,
        lap_time_ms         INTEGER,
        lap_time_source     TEXT,
        s1_ms               INTEGER,
        s2_ms               INTEGER,
        s3_ms               INTEGER,
        valid_history       INTEGER,
        invalid_flag_seen   INTEGER NOT NULL,
        flashbacks          INTEGER NOT NULL,
        pauses              INTEGER NOT NULL,
        frames              INTEGER NOT NULL,
        missing_frames      INTEGER NOT NULL,
        loss_pct            REAL NOT NULL,
        max_distance_gap_m  REAL NOT NULL,
        start_session_time  REAL,
        end_session_time    REAL,
        start_frame         INTEGER,
        end_frame           INTEGER,
        start_distance_m    REAL,
        end_distance_m      REAL,
        tyre_actual         INTEGER,
        tyre_visual         INTEGER,
        tyre_age_laps       INTEGER,
        fuel_start          REAL,
        fuel_end            REAL,
        tyre_wear_start     TEXT,
        tyre_wear_end       TEXT,
        assists_json        TEXT,
        setup_json          TEXT,
        issues_json         TEXT NOT NULL,
        notes_json          TEXT NOT NULL,
        pipeline_version    TEXT NOT NULL,
        PRIMARY KEY (session_key, lap_index)
    );
    """,
}
MIGRATIONS[2] = """
    ALTER TABLE captures ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sessions ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sessions ADD COLUMN capture_label TEXT;
    CREATE INDEX laps_usable ON laps(usable, lap_time_ms);
"""
SCHEMA_VERSION = max(MIGRATIONS)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this Pitwall "
            f"(v{SCHEMA_VERSION}); update Pitwall"
        )
    for version in sorted(v for v in MIGRATIONS if v > current):
        with conn:
            conn.executescript(MIGRATIONS[version])
            conn.execute(f"PRAGMA user_version = {version}")


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def replace_capture(
    conn: sqlite3.Connection,
    capture: dict[str, Any],
    sessions: Iterable[dict[str, Any]],
    laps: Iterable[dict[str, Any]],
) -> None:
    """Atomically replace everything stored for one capture."""
    with conn:
        conn.execute("DELETE FROM captures WHERE capture_id = ?", (capture["capture_id"],))
        _insert(conn, "captures", capture)
        for s in sessions:
            _insert(conn, "sessions", s)
        for lap in laps:
            _insert(conn, "laps", lap)


def ingested(conn: sqlite3.Connection) -> dict[str, str]:
    """capture_id -> pipeline_version it was ingested with."""
    return {
        r["capture_id"]: r["pipeline_version"]
        for r in conn.execute("SELECT capture_id, pipeline_version FROM captures")
    }


def list_sessions(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM sessions ORDER BY first_utc DESC, session_key"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def find_sessions(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """Sessions whose key contains ``query`` ("latest" = most recent session)."""
    if query == "latest":
        return list_sessions(conn, limit=1)
    like = f"%{query}%"
    return list(
        conn.execute(
            "SELECT * FROM sessions WHERE session_key LIKE ? ORDER BY first_utc DESC", (like,)
        )
    )


def list_laps(conn: sqlite3.Connection, session_key: str) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM laps WHERE session_key = ? ORDER BY lap_index", (session_key,))
    )


def to_json_text(value: Any) -> str | None:
    return _json(value)


def get_session(conn: sqlite3.Connection, session_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE session_key = ?", (session_key,)).fetchone()


def get_lap(conn: sqlite3.Connection, session_key: str, lap_index: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM laps WHERE session_key = ? AND lap_index = ?", (session_key, lap_index)
    ).fetchone()


def best_usable_lap(conn: sqlite3.Connection, session_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM laps WHERE session_key = ? AND usable = 1 AND lap_time_ms IS NOT NULL "
        "ORDER BY lap_time_ms, lap_index LIMIT 1",
        (session_key,),
    ).fetchone()


def personal_best_lap(
    conn: sqlite3.Connection, track_id: int | None, formula: int | None, synthetic: int
) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    """Fastest usable lap on a track/car class over all sessions (real and synthetic apart)."""
    row = conn.execute(
        "SELECT l.session_key, l.lap_index FROM laps l JOIN sessions s USING (session_key) "
        "WHERE s.track_id IS ? AND s.formula IS ? AND s.synthetic = ? AND l.usable = 1 "
        "AND l.lap_time_ms IS NOT NULL ORDER BY l.lap_time_ms, s.first_utc LIMIT 1",
        (track_id, formula, int(synthetic)),
    ).fetchone()
    if row is None:
        return None
    session = get_session(conn, row["session_key"])
    lap = get_lap(conn, row["session_key"], row["lap_index"])
    assert session is not None and lap is not None
    return session, lap


def best_sector_laps(
    conn: sqlite3.Connection, session_key: str
) -> tuple[tuple[sqlite3.Row, int], tuple[sqlite3.Row, int], tuple[sqlite3.Row, int]] | None:
    """For each sector, the usable lap with the fastest official sector time."""
    out = []
    for col in ("s1_ms", "s2_ms", "s3_ms"):
        row = conn.execute(
            f"SELECT * FROM laps WHERE session_key = ? AND usable = 1 AND {col} IS NOT NULL "
            f"ORDER BY {col}, lap_index LIMIT 1",
            (session_key,),
        ).fetchone()
        if row is None:
            return None
        out.append((row, int(row[col])))
    return out[0], out[1], out[2]
