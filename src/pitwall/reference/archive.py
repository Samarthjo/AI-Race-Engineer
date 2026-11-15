"""Downloading real-F1 reference data, through a raw cache.

Formula 1's own timing archive (livetiming.formula1.com, which FastF1 reads) has answered
401 Unauthorized without an F1 TV login since October 2026. Pitwall therefore reads the
**TracingInsights archive** by default: one public GitHub repository per season
(github.com/TracingInsights/<season>, Apache-2.0) that republishes, as JSON, the data FastF1
produced from the official feed: lap tables and per-lap car and position telemetry. Files
are served by raw.githubusercontent.com. The event calendar comes from FastF1's own schedule
backup (github.com/theOehrly/f1schedule).

Archive layout (one folder per event and session, names as in the schedule)::

    <event>/<session>/drivers.json
    <event>/<session>/session_laptimes.json        all drivers' laps
    <event>/<session>/corners.json                 corner positions, track outline
    <event>/<session>/<DRIVER>/laptimes.json       one driver's laps
    <event>/<session>/<DRIVER>/<lap>_tel.json      one lap's telemetry

Every downloaded file is stored byte-for-byte in the raw cache
(``<data dir>/references/raw/...``) next to a ``.source.json`` with its URL, SHA-256 and
download time. Cached files are never modified or downloaded again, so a reference keeps
working offline and always gives the same result. The URLs can be changed in the config file
(``[reference]``), for example to a mirror.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pitwall import __version__

PROVIDER = "tracinginsights"
TI_BASE_URL = "https://raw.githubusercontent.com/TracingInsights/{season}/main/"
SCHEDULE_URL = (
    "https://raw.githubusercontent.com/theOehrly/f1schedule/master/schedule_{season}.json"
)
TIMEOUT_S = 30.0
SOURCE_SUFFIX = ".source.json"


class FetchError(RuntimeError):
    """A reference file could not be downloaded and is not in the cache."""


Fetch = Callable[[str], bytes]


def http_get(url: str, timeout: float = TIMEOUT_S) -> bytes:
    """GET a URL. ``file://`` URLs read local files (tests and the synthetic demo archive)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise FetchError(f"not in the archive: {path}") from exc
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"unsupported URL: {url}")
    host = parsed.netloc
    request = urllib.request.Request(url, headers={"User-Agent": f"pitwall/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FetchError(f"not in the archive (HTTP 404): {url}") from exc
        if exc.code in (401, 403, 407):
            raise FetchError(
                f"{host} refused the request (HTTP {exc.code}); a firewall, proxy or network "
                "policy may block it (see Troubleshooting in the user guide)"
            ) from exc
        raise FetchError(f"{host} answered HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"cannot reach {host}: {reason}") from exc


def _safe_part(part: str) -> str:
    if not part or part in (".", "..") or any(c in part for c in "/\\\0"):
        raise ValueError(f"unsafe name in a reference path: {part!r}")
    return part


@dataclass
class RawCache:
    """Downloaded reference files, kept byte-for-byte (the L0 layer of reference data)."""

    root: Path
    fetch: Fetch = http_get
    offline: bool = False

    def path(self, rel: tuple[str, ...]) -> Path:
        return self.root.joinpath(*(_safe_part(p) for p in rel))

    def get(self, rel: tuple[str, ...], url: str) -> bytes:
        path = self.path(rel)
        if path.exists():
            return path.read_bytes()
        if self.offline:
            raise FetchError(f"not downloaded yet, and working offline: {url}")
        data = self.fetch(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tag = f"{os.getpid()}-{threading.get_ident()}.part"
        meta = {
            "url": url,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp_meta = path.with_name(f"{path.name}{SOURCE_SUFFIX}.{tag}")
        tmp_meta.write_text(json.dumps(meta, indent=2) + "\n")
        os.replace(tmp_meta, path.with_name(path.name + SOURCE_SUFFIX))
        tmp = path.with_name(f"{path.name}.{tag}")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # the data file appears last, and only complete
        return data

    def source(self, rel: tuple[str, ...]) -> dict[str, Any] | None:
        side = self.path(rel)
        side = side.with_name(side.name + SOURCE_SUFFIX)
        if not side.exists():
            return None
        return json.loads(side.read_text())


@dataclass(frozen=True)
class Event:
    round: int
    name: str
    location: str
    country: str
    date: str | None
    sessions: tuple[str, ...]

    def matches(self, query: str) -> bool:
        q = query.casefold().strip()
        return any(q in v.casefold() for v in (self.name, self.location, self.country))


def parse_schedule(data: dict[str, Any]) -> list[Event]:
    """FastF1's schedule backup: columns of ``{row: value}`` (pandas "columns" JSON)."""
    names = data.get("event_name") or {}
    events = []
    for k in sorted(names, key=int):
        if (data.get("event_format") or {}).get(k) == "testing":
            continue
        sessions = tuple(
            str(s) for i in range(1, 6) if (s := (data.get(f"session{i}") or {}).get(k))
        )
        events.append(
            Event(
                round=int((data.get("round_number") or {}).get(k, 0)),
                name=str(names[k]),
                location=str((data.get("location") or {}).get(k) or ""),
                country=str((data.get("country") or {}).get(k) or ""),
                date=(str((data.get("event_date") or {}).get(k) or "")[:10] or None),
                sessions=sessions,
            )
        )
    return events


@dataclass
class Archive:
    """One season of the reference archive, read through the raw cache."""

    season: int
    cache: RawCache
    base_url: str = TI_BASE_URL
    schedule_url: str = SCHEDULE_URL
    provider: str = PROVIDER
    synthetic: bool = False

    def url(self, *parts: str) -> str:
        base = self.base_url.format(season=self.season).rstrip("/")
        return base + "/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)

    def _rel(self, *parts: str) -> tuple[str, ...]:
        return (self.provider, str(self.season), *parts)

    def raw(self, *parts: str) -> bytes:
        return self.cache.get(self._rel(*parts), self.url(*parts))

    def source(self, *parts: str) -> dict[str, Any] | None:
        """URL, SHA-256 and download time of a cached file."""
        return self.cache.source(self._rel(*parts))

    def _json(self, data: bytes, where: str) -> Any:
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f"{where} is not valid JSON") from exc

    def json(self, *parts: str) -> Any:
        return self._json(self.raw(*parts), self.url(*parts))

    # ------------------------------------------------------------------ calendar

    def events(self) -> list[Event]:
        url = self.schedule_url.format(season=self.season)
        rel = ("schedule", self.provider, f"{self.season}.json")
        return parse_schedule(self._json(self.cache.get(rel, url), url))

    def find_event(self, query: str) -> Event:
        events = self.events()
        exact = [e for e in events if e.name.casefold() == query.casefold().strip()]
        found = exact or [e for e in events if e.matches(query)]
        if not found:
            raise LookupError(f"no {self.season} event matches {query!r}")
        if len(found) > 1:
            names = ", ".join(e.name for e in found)
            raise LookupError(f"{query!r} matches several events ({names}); be more specific")
        return found[0]

    # ------------------------------------------------------------------ session data

    def drivers(self, event: str, session: str) -> list[dict[str, Any]]:
        data = self.json(event, session, "drivers.json")
        return list(data.get("drivers") or [])

    def session_laptimes(self, event: str, session: str) -> dict[str, Any]:
        return self.json(event, session, "session_laptimes.json")

    def driver_laptimes(self, event: str, session: str, driver: str) -> dict[str, Any]:
        return self.json(event, session, driver, "laptimes.json")

    def telemetry(self, event: str, session: str, driver: str, lap: int) -> dict[str, Any]:
        data = self.json(event, session, driver, f"{int(lap)}_tel.json")
        return dict(data.get("tel") or {})

    def telemetry_parts(self, event: str, session: str, driver: str, lap: int) -> tuple[str, ...]:
        return (event, session, driver, f"{int(lap)}_tel.json")

    def corners(self, event: str, session: str) -> dict[str, Any]:
        return self.json(event, session, "corners.json")
