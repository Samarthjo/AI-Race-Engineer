"""Track model: segments that cover the whole lap (corners, complexes, flat corners, straights).

Stored as JSON in ``<data_dir>/tracks/<key>.json``: readable, and editable by hand (names,
boundaries). A model starts as a **draft** detected from one of your fast laps
(:mod:`pitwall.track.draft`). Once you have checked it on the map and **confirmed** it, it is
used unchanged for every lap on that track, so laps are always cut into the same pieces.
Every edit bumps ``version``; analysis results carry the version they used.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SEGMENT_KINDS = ("corner", "complex", "flat", "straight")
STATUSES = ("draft", "confirmed")
LENGTH_TOLERANCE_M = 1.0

# Suggested names for braking corners of known circuits, in lap order. Applied to a draft only
# when the number of detected braking corners matches exactly; always shown as a draft for
# you to check.
KNOWN_CORNER_NAMES: dict[int, list[str]] = {
    11: [  # Monza
        "Variante del Rettifilo (T1-T2)",
        "Variante della Roggia (T4-T5)",
        "Lesmo 1 (T6)",
        "Lesmo 2 (T7)",
        "Variante Ascari (T8-T10)",
        "Curva Alboreto (T11)",
    ],
}


@dataclass
class Segment:
    id: str
    name: str
    kind: str  # corner | complex | flat | straight
    start_m: float
    end_m: float
    apexes_m: list[float] = field(default_factory=list)

    @property
    def is_corner(self) -> bool:
        return self.kind != "straight"

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


@dataclass
class TrackModel:
    key: str
    track_id: int | None
    track_name: str
    length_m: float
    status: str
    version: int
    synthetic: bool
    source: dict[str, Any]
    segments: list[Segment]
    created_utc: str = ""
    updated_utc: str = ""
    notes: str = ""

    def validate(self) -> list[str]:
        """Problems that make the model unusable (empty list = fine)."""
        errors: list[str] = []
        if self.status not in STATUSES:
            errors.append(f"status must be one of {STATUSES}")
        if not self.segments:
            return [*errors, "no segments"]
        ids = [s.id for s in self.segments]
        if len(set(ids)) != len(ids):
            errors.append("segment ids must be unique")
        if abs(self.segments[0].start_m) > 1e-6:
            errors.append("first segment must start at 0 m (the line)")
        if abs(self.segments[-1].end_m - self.length_m) > LENGTH_TOLERANCE_M:
            errors.append(f"last segment must end at the track length ({self.length_m} m)")
        for a, b in zip(self.segments, self.segments[1:], strict=False):
            if abs(a.end_m - b.start_m) > 1e-6:
                errors.append(f"gap or overlap between {a.id} and {b.id}")
        for s in self.segments:
            if s.kind not in SEGMENT_KINDS:
                errors.append(f"{s.id}: unknown kind {s.kind!r}")
            if s.end_m <= s.start_m:
                errors.append(f"{s.id}: ends before it starts")
            if any(not (s.start_m <= a <= s.end_m) for a in s.apexes_m):
                errors.append(f"{s.id}: apex outside the segment")
        return errors

    def fits(self, track_length_m: float | None) -> bool:
        return (
            track_length_m is not None
            and abs(float(track_length_m) - self.length_m) <= LENGTH_TOLERANCE_M
        )

    @property
    def corners(self) -> list[Segment]:
        return [s for s in self.segments if s.is_corner]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrackModel:
        d = dict(d)
        d["segments"] = [Segment(**s) for s in d["segments"]]
        return cls(**d)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def track_key(track_id: int | None, track_name: str | None, synthetic: bool) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (track_name or "track").lower()).strip("-") or "track"
    tid = "x" if track_id is None else str(track_id)
    return f"{'synthetic-' if synthetic else ''}track{tid}-{slug}"


def tracks_dir(data_dir: Path) -> Path:
    return data_dir / "tracks"


def model_path(data_dir: Path, key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]+", key):
        raise ValueError(f"invalid track key {key!r}")
    return tracks_dir(data_dir) / f"{key}.json"


def load_model(data_dir: Path, key: str) -> TrackModel | None:
    path = model_path(data_dir, key)
    if not path.exists():
        return None
    return TrackModel.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_model(data_dir: Path, model: TrackModel) -> Path:
    errors = model.validate()
    if errors:
        raise ValueError("invalid track model: " + "; ".join(errors))
    path = model_path(data_dir, model.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not model.created_utc:
        model.created_utc = _now()
    model.updated_utc = _now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(model.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def list_models(data_dir: Path) -> list[TrackModel]:
    d = tracks_dir(data_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(TrackModel.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (ValueError, KeyError, TypeError):
            continue  # a hand-edit broke it; `pitwall track show` reports why
    return out


def confirm_model(data_dir: Path, key: str) -> TrackModel:
    model = load_model(data_dir, key)
    if model is None:
        raise FileNotFoundError(f"no track model {key!r}")
    if model.status != "confirmed":
        model.status = "confirmed"
        model.version += 1
        save_model(data_dir, model)
    return model


def rename_segment(data_dir: Path, key: str, segment_id: str, name: str) -> TrackModel:
    model = load_model(data_dir, key)
    if model is None:
        raise FileNotFoundError(f"no track model {key!r}")
    for s in model.segments:
        if s.id == segment_id:
            s.name = name.strip() or s.name
            model.version += 1
            save_model(data_dir, model)
            return model
    raise KeyError(f"no segment {segment_id!r} in {key}")
