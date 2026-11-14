"""Draft a track model from one fast lap.

Corners are found from **how the lap was driven**:

* a braking or lifting zone (brake >= 10 %, or throttle < 90 % for 10 m or more) marks a
  corner; the corner runs from 50 m before that point to 50 m after full throttle is back
  (>= 98 % held for 30 m);
* corners whose zones overlap or sit closer than 80 m are merged into one **complex**
  (chicanes);
* **flat** corners (taken without lifting) are found from the curvature of the driven line:
  radius under 600 m for at least 40 m;
* everything in between is a **straight**, so the segments cover 100 % of the lap.

The result is a draft. Check it on the map and confirm it (`pitwall track confirm`).
"""

from __future__ import annotations

import numpy as np

from pitwall.analysis.trace import LapTrace
from pitwall.track.model import KNOWN_CORNER_NAMES, Segment, TrackModel, track_key

BRAKE_ON = 0.10
LIFT_BELOW = 0.90
LIFT_MIN_M = 10.0
FULL = 0.98
FULL_HOLD_M = 30.0
EVENT_JOIN_M = 30.0
PRE_M = 50.0
POST_M = 50.0
MERGE_GAP_M = 80.0
FLAT_RADIUS_M = 600.0
FLAT_MIN_LEN_M = 40.0
FLAT_MARGIN_M = 25.0
MIN_SEGMENT_M = 10.0


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where mask is True."""
    if not len(mask):
        return []
    m = np.concatenate([[False], mask, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(m))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _smooth(y: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return y
    k = np.ones(n) / n
    return np.convolve(np.pad(y, (n // 2, n - 1 - n // 2), mode="edge"), k, "valid")


def curvature(x: np.ndarray, z: np.ndarray, step: float) -> np.ndarray:
    """Signed curvature (1/m) of the driven line, from smoothed world positions."""
    w = max(1, round(10.0 / step))
    xs, zs = _smooth(x, w), _smooth(z, w)
    heading = np.unwrap(np.arctan2(np.gradient(zs), np.gradient(xs)))
    k = np.gradient(heading, step)
    return _smooth(k, max(1, round(20.0 / step)))


def detect_corners(trace: LapTrace) -> list[tuple[float, float, list[float], bool]]:
    """(start_m, end_m, apexes_m, flat) for every corner, before merging and naming."""
    d = trace.distance
    step = float(d[1] - d[0])
    ch = trace.channels
    brake = np.nan_to_num(ch["brake"])
    throttle = np.nan_to_num(ch["throttle"], nan=1.0)
    speed = ch["speed"]
    n = len(d)

    slowing = (brake >= BRAKE_ON) | (throttle < LIFT_BELOW)
    events = [(a, b) for a, b in _runs(slowing) if (b - a) * step >= min(LIFT_MIN_M, 5.0)]
    # A short lift without braking is only a corner if it lasts LIFT_MIN_M.
    events = [
        (a, b) for a, b in events if (brake[a:b] >= BRAKE_ON).any() or (b - a) * step >= LIFT_MIN_M
    ]
    joined: list[list[int]] = []
    for a, b in events:
        if joined and (a - joined[-1][1]) * step < EVENT_JOIN_M:
            joined[-1][1] = b
        else:
            joined.append([a, b])

    hold = max(1, round(FULL_HOLD_M / step))
    full = throttle >= FULL
    corners: list[tuple[float, float, list[float], bool]] = []
    for a, b in joined:
        # full throttle back (and held) after the event
        j = b
        while j < n and not full[j : j + hold].all():
            j += 1
        j = min(j, n - 1)
        core = slice(a, max(j, a + 1))
        apex = a + int(np.argmin(speed[core]))
        start = max(0.0, float(d[a]) - PRE_M)
        end = min(float(d[-1]), float(d[j]) + POST_M)
        corners.append((start, end, [float(d[apex])], False))

    # flat corners from the line's curvature, where no braking/lifting corner is
    x, z = ch.get("x"), ch.get("z")
    if x is not None and z is not None and np.isfinite(x).all() and np.isfinite(z).all():
        k = np.abs(curvature(x, z, step))
        for a, b in _runs(k > 1.0 / FLAT_RADIUS_M):
            if (b - a) * step < FLAT_MIN_LEN_M:
                continue
            s0 = max(0.0, float(d[a]) - FLAT_MARGIN_M)
            s1 = min(float(d[-1]), float(d[b - 1]) + FLAT_MARGIN_M)
            if any(s0 < c1 and s1 > c0 for c0, c1, _, _ in corners):
                continue  # already inside a braking/lifting corner
            apex = a + int(np.argmax(k[a:b]))
            corners.append((s0, s1, [float(d[apex])], True))
    corners.sort(key=lambda c: c[0])
    return corners


def draft_segments(trace: LapTrace, track_id: int | None = None) -> list[Segment]:
    length = float(trace.distance[-1])
    raw = detect_corners(trace)

    merged: list[dict] = []
    for s0, s1, apexes, flat in raw:
        if merged and s0 - merged[-1]["end"] < MERGE_GAP_M:
            m = merged[-1]
            m["end"] = max(m["end"], s1)
            m["apexes"].extend(apexes)
            m["flat"] = m["flat"] and flat
            m["parts"] += 1
        else:
            merged.append(
                {"start": s0, "end": s1, "apexes": list(apexes), "flat": flat, "parts": 1}
            )

    # Keep corners apart from the line: a corner may not start at 0 m or end at the length,
    # so the first and last segments are straights through the start/finish line.
    for m in merged:
        m["start"] = max(m["start"], MIN_SEGMENT_M)
        m["end"] = min(m["end"], length - MIN_SEGMENT_M)

    segments: list[Segment] = []
    cursor = 0.0
    for corner_no, m in enumerate(merged, start=1):
        if m["start"] - cursor >= MIN_SEGMENT_M:
            segments.append(Segment("", "", "straight", round(cursor, 1), round(m["start"], 1)))
        else:
            m["start"] = cursor
        kind = "flat" if m["flat"] else ("complex" if m["parts"] > 1 else "corner")
        segments.append(
            Segment(
                f"c{corner_no}",
                f"C{corner_no}",
                kind,
                round(m["start"], 1),
                round(m["end"], 1),
                sorted(round(a, 1) for a in m["apexes"]),
            )
        )
        cursor = m["end"]
    if length - cursor > 1e-6:
        if segments and length - cursor < MIN_SEGMENT_M:
            segments[-1].end_m = length
        else:
            segments.append(Segment("", "", "straight", round(cursor, 1), length))
    if segments:
        segments[0].start_m = 0.0
        segments[-1].end_m = length

    _name(segments, track_id)
    return segments


def _name(segments: list[Segment], track_id: int | None) -> None:
    braking = [s for s in segments if s.kind in ("corner", "complex")]
    names = KNOWN_CORNER_NAMES.get(track_id) if track_id is not None else None
    if names and len(names) == len(braking):
        for s, name in zip(braking, names, strict=True):
            s.name = name
    for s in segments:
        if s.kind == "flat":
            s.name = f"{s.name} (flat)"
    straight_no = 0
    for i, s in enumerate(segments):
        if s.kind != "straight":
            continue
        s.id = f"s{straight_no}"
        straight_no += 1
        nxt = next((t for t in segments[i + 1 :] if t.is_corner), None)
        s.name = f"Straight to {nxt.name}" if nxt else "Straight to the line"


def draft_model(
    trace: LapTrace,
    *,
    track_id: int | None,
    track_name: str,
    synthetic: bool,
    source: dict,
) -> TrackModel:
    return TrackModel(
        key=track_key(track_id, track_name, synthetic),
        track_id=track_id,
        track_name=track_name,
        length_m=float(trace.distance[-1]),
        status="draft",
        version=1,
        synthetic=synthetic,
        source=source,
        segments=draft_segments(trace, track_id),
        notes=(
            "Detected automatically from one lap. Check the corners on the map; edit names or "
            "boundaries here if needed, then confirm with `pitwall track confirm`."
        ),
    )
