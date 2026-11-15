"""Choosing and fetching reference laps; which real data may be compared with which lap.

Rules (each refusal says why; nothing is compared "roughly"):

* **Era guard.** F1 25 in its F1 25 UDP format models the 2025 cars, so only the 2025
  season is a valid reference (``ERA_SEASON``). The 2026 Season Pack format is not supported.
* **Car class.** Only "F1 Modern" laps (formula 0) are compared with real Formula 1 laps.
* **Track.** The game track must be the circuit of the reference event (``TRACK_EVENTS``).
  Reverse layouts have no real reference.
* **Synthetic data** (``pitwall demo``, tests) is only ever compared with synthetic data.

Choosing a lap: season -> event -> session (Q1/Q2/Q3 part optional) -> driver -> the fastest
lap that is not deleted, not marked inaccurate, not filled in by FastF1 and not an out or
in lap (``LapRow.issues``), unless a specific lap is asked for.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from pitwall.analysis.trace import NotComparableError
from pitwall.reference.archive import (
    PROVIDER,
    SCHEDULE_URL,
    TI_BASE_URL,
    TIMEOUT_S,
    Archive,
    FetchError,
    RawCache,
    http_get,
)
from pitwall.reference.laps import (
    LapRow,
    ReferenceDataError,
    RefLap,
    fastest_usable,
    parse_lap_rows,
    parse_telemetry,
    ref_key,
)
from pitwall.reference.store import (
    REFERENCE_PIPELINE_VERSION,
    lap_from_row,
    raw_dir,
    refs_dir,
    save_ref_lap,
)
from pitwall.storage import db

ERA_SEASON: dict[int, int] = {2025: 2025}  # UDP packet format -> season of its real cars
REAL_FORMULA = 0  # "F1 Modern"
DEFAULT_SESSION = "Qualifying"
SYNTHETIC_PROVIDER = "synthetic"
SYNTHETIC_EVENT = "Synthetic Grand Prix"

# F1 25 track id -> 2025 event (names as in FastF1's schedule and the archive folders)
TRACK_EVENTS: dict[int, str] = {
    0: "Australian Grand Prix",
    2: "Chinese Grand Prix",
    3: "Bahrain Grand Prix",
    4: "Spanish Grand Prix",
    5: "Monaco Grand Prix",
    6: "Canadian Grand Prix",
    7: "British Grand Prix",
    9: "Hungarian Grand Prix",
    10: "Belgian Grand Prix",
    11: "Italian Grand Prix",
    12: "Singapore Grand Prix",
    13: "Japanese Grand Prix",
    14: "Abu Dhabi Grand Prix",
    15: "United States Grand Prix",
    16: "São Paulo Grand Prix",
    17: "Austrian Grand Prix",
    19: "Mexico City Grand Prix",
    20: "Azerbaijan Grand Prix",
    26: "Dutch Grand Prix",
    27: "Emilia Romagna Grand Prix",
    29: "Saudi Arabian Grand Prix",
    30: "Miami Grand Prix",
    31: "Las Vegas Grand Prix",
    32: "Qatar Grand Prix",
}


def season_for(session: dict[str, Any]) -> int:
    """The real season whose cars the game session models (era guard)."""
    fmt = session.get("packet_format")
    season = ERA_SEASON.get(fmt) if fmt is not None else None
    if season is None:
        raise NotComparableError(
            f"no real-F1 season is defined for UDP format {fmt}; Pitwall supports the "
            "F1 25 format (2025 cars) only"
        )
    return season


def event_for(session: dict[str, Any]) -> str:
    """The reference event for the session's track."""
    if session.get("synthetic"):
        return SYNTHETIC_EVENT
    event = TRACK_EVENTS.get(session.get("track_id"))  # type: ignore[arg-type]
    if event is None:
        raise NotComparableError(
            f"{session.get('track_name') or 'this track'} has no real-F1 reference "
            "(reverse or unknown layout)"
        )
    return event


def check_pairing(session: dict[str, Any], ref: RefLap) -> None:
    """Raise NotComparableError when the reference may not be compared with the session."""
    synthetic = bool(session.get("synthetic"))
    if synthetic != ref.synthetic:
        raise NotComparableError(
            "synthetic laps are only compared with synthetic references, and real laps with "
            "real ones"
        )
    season = season_for(session)
    if ref.season != season:
        raise NotComparableError(
            f"F1 25 base cars are {season} cars; a {ref.season} reference would compare "
            "different cars (era guard)"
        )
    formula = session.get("formula")
    if not synthetic and formula is not None and formula != REAL_FORMULA:
        raise NotComparableError("only F1 Modern laps are compared with real Formula 1 laps")
    event = event_for(session)
    if ref.event != event:
        raise NotComparableError(
            f"the reference is from the {ref.event}, but this lap is at "
            f"{session.get('track_name')} ({event})"
        )


def synthetic_archive_dir(data_dir: Path) -> Path:
    """Where ``pitwall demo`` writes its synthetic reference archive."""
    return refs_dir(data_dir) / "synthetic-archive"


def make_archive(
    data_dir: Path,
    season: int,
    *,
    base_url: str = TI_BASE_URL,
    schedule_url: str = SCHEDULE_URL,
    timeout_s: float = TIMEOUT_S,
    offline: bool = False,
    synthetic: bool = False,
) -> Archive:
    """The archive for a season, read through the raw cache in the data directory.

    Synthetic archives are read from ``synthetic_archive_dir`` unless URLs are given."""
    if synthetic and base_url == TI_BASE_URL:
        root = synthetic_archive_dir(data_dir).resolve().as_uri()
        base_url, schedule_url = root + "/{season}/", root + "/schedule_{season}.json"

    def fetch(url: str) -> bytes:
        return http_get(url, timeout_s)

    return Archive(
        season=season,
        cache=RawCache(raw_dir(data_dir), fetch=fetch, offline=offline),
        base_url=base_url,
        schedule_url=schedule_url,
        provider=SYNTHETIC_PROVIDER if synthetic else PROVIDER,
        synthetic=synthetic,
    )


def driver_laps(archive: Archive, event: str, session: str, driver: str) -> list[LapRow]:
    return parse_lap_rows(archive.driver_laptimes(event, session, driver), driver)


def _driver_info(archive: Archive, event: str, session: str, driver: str) -> dict[str, Any]:
    try:
        for d in archive.drivers(event, session):
            if str(d.get("driver", "")).upper() == driver.upper():
                return d
    except (FetchError, ReferenceDataError):
        pass
    return {}


def choose_lap(rows: list[LapRow], lap: int | None, part: str | None) -> LapRow:
    if lap is None:
        row = fastest_usable(rows, part)
        if row is None:
            where = f" in {part}" if part else ""
            raise LookupError(f"no lap{where} can be a reference (all deleted, in/out or untimed)")
        return row
    row = next((r for r in rows if r.lap_number == lap), None)
    if row is None:
        raise LookupError(f"lap {lap} is not in the archive")
    if not row.usable:
        raise ReferenceDataError(f"lap {lap} cannot be a reference: {', '.join(row.issues)}")
    return row


def fetch_reference(
    conn: sqlite3.Connection,
    data_dir: Path,
    archive: Archive,
    event: str,
    session: str,
    driver: str,
    *,
    lap: int | None = None,
    part: str | None = None,
) -> RefLap:
    """Download (or read from the cache), check, normalise and store one reference lap."""
    driver = driver.upper()
    rows = driver_laps(archive, event, session, driver)
    row = choose_lap(rows, lap, part)
    assert row.lap_time_ms is not None
    tel_parts = (event, session, driver, f"{row.lap_number}_tel.json")
    tel = archive.telemetry(event, session, driver, row.lap_number)
    samples, caps, checks = parse_telemetry(tel, row.lap_time_ms / 1000)
    sectors = row.sectors_ms
    if all(s is not None for s in sectors):
        total = sum(s for s in sectors if s is not None)
        if abs(total - row.lap_time_ms) > 2:
            checks.notes.append(
                f"sector times add up to {total} ms, the lap time is {row.lap_time_ms} ms"
            )
    info = _driver_info(archive, event, session, driver)
    sources = []
    for parts in ((event, session, driver, "laptimes.json"), tel_parts):
        src = archive.source(*parts) or {"url": archive.url(*parts)}
        sources.append({"parts": list(parts), **src})
    name = " ".join(str(info[k]) for k in ("fn", "ln") if info.get(k)) or None
    ref = RefLap(
        key=ref_key(archive.provider, archive.season, event, session, driver, row.lap_number),
        provider=archive.provider,
        synthetic=archive.synthetic,
        season=archive.season,
        event=event,
        session=session,
        session_part=row.session_part,
        driver=driver,
        driver_number=row.driver_number or (str(info["dn"]) if info.get("dn") else None),
        driver_name=name,
        team=row.team or info.get("team"),
        lap_number=row.lap_number,
        lap_time_ms=row.lap_time_ms,
        sectors_ms=row.sectors_ms,
        compound=row.compound,
        tyre_life=row.tyre_life,
        track_status=row.track_status,
        capabilities=caps,
        checks=checks.to_dict(),
        sources=sources,
        fetched_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        samples=samples,
    )
    save_ref_lap(conn, data_dir, ref)
    return ref


def load_reference(conn: sqlite3.Connection, data_dir: Path, key: str) -> RefLap:
    """A stored reference lap; rebuilt from the raw cache when it was made by older code."""
    row = db.get_ref_lap(conn, key)
    if row is None:
        raise LookupError(f"reference {key!r} is not stored; fetch it first")
    r = dict(row)
    if r["pipeline_version"] != REFERENCE_PIPELINE_VERSION:
        archive = make_archive(data_dir, r["season"], offline=True, synthetic=bool(r["synthetic"]))
        archive.provider = r["provider"]
        return fetch_reference(
            conn, data_dir, archive, r["event"], r["session"], r["driver"], lap=r["lap_number"]
        )
    return lap_from_row(data_dir, r)
