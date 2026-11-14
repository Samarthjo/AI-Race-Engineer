"""Canonical timeline of a stream: what actually counts after flashbacks and resets.

A flashback rewinds ``frameIdentifier`` (``overallFrameIdentifier`` keeps counting). The
game also sends an ``FLBK`` event naming the frame it rewound to. Rows recorded *after*
that frame before the flashback describe a future that was erased, so they are dropped.

A rewind with no matching ``FLBK`` event is never treated as a flashback (that could delete
real laps). Instead the timeline starts a new **segment**; laps never span segments.
Repeated frames (same ``frameIdentifier`` twice in a row) keep the first copy.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass
class Timeline:
    keep: np.ndarray  # indices into the stream's receive-order rows, canonical order
    segment: np.ndarray  # segment id per kept row
    after_rewind: np.ndarray  # True for the first kept row after a flashback truncation
    flashbacks_applied: int
    resets: int
    duplicates: int
    dropped: int  # rows erased by flashbacks


def cadence(overall_frames: np.ndarray) -> int:
    """Typical frame step between consecutive packets of a stream (1 at 60 Hz)."""
    steps = np.diff(overall_frames.astype(np.int64))
    steps = steps[steps > 0]
    if len(steps) == 0:
        return 1
    return max(1, int(np.median(steps)))


def canonicalize(
    frames: np.ndarray, flashback_targets: Sequence[int], tolerance: int = 10
) -> Timeline:
    """Resolve flashbacks/resets for one stream given its frameIdentifiers in receive order."""
    targets = sorted({int(t) for t in flashback_targets})
    kept_idx: list[int] = []
    kept_frame: list[int] = []
    kept_seg: list[int] = []
    rewind_marks: set[int] = set()  # positions in kept lists that follow a truncation
    seg = 0
    seg_start = 0
    applied = resets = duplicates = dropped = 0

    for i, f in enumerate(frames.tolist()):
        if len(kept_frame) > seg_start:
            last = kept_frame[-1]
            if f == last:
                duplicates += 1
                continue
            if f < last:
                if _is_flashback(f, targets, tolerance):
                    seg_frames = kept_frame[seg_start:]
                    cut = seg_start + bisect_left(seg_frames, f)
                    dropped += len(kept_frame) - cut
                    del kept_idx[cut:]
                    del kept_frame[cut:]
                    del kept_seg[cut:]
                    rewind_marks = {m for m in rewind_marks if m < cut}
                    rewind_marks.add(cut)
                    applied += 1
                else:
                    seg += 1
                    resets += 1
                    seg_start = len(kept_frame)
        kept_idx.append(i)
        kept_frame.append(f)
        kept_seg.append(seg)

    after = np.zeros(len(kept_idx), dtype=bool)
    for m in rewind_marks:
        if m < len(after):
            after[m] = True
    return Timeline(
        keep=np.asarray(kept_idx, dtype=np.int64),
        segment=np.asarray(kept_seg, dtype=np.int32),
        after_rewind=after,
        flashbacks_applied=applied,
        resets=resets,
        duplicates=duplicates,
        dropped=dropped,
    )


def _is_flashback(frame: int, targets: list[int], tolerance: int) -> bool:
    """A rewind is a flashback if some FLBK target t satisfies t <= frame <= t + tolerance."""
    lo = bisect_left(targets, frame - tolerance)
    hi = bisect_right(targets, frame)
    return hi > lo


def count_missing(
    overall: np.ndarray, step: int, skip: np.ndarray | None = None, pause: np.ndarray | None = None
) -> int:
    """Estimated packets missing between consecutive rows, given the stream cadence.

    ``skip[i]`` / ``pause[i]`` mark rows whose gap to the previous row is explained
    (flashback truncation, game paused) and must not count as loss.
    """
    if len(overall) < 2:
        return 0
    steps = np.diff(overall.astype(np.int64))
    explained = np.zeros(len(steps), dtype=bool)
    if skip is not None:
        explained |= skip[1:]
    if pause is not None:
        explained |= pause[1:]
    big = (steps > 1.5 * step) & ~explained
    return int(np.sum(np.rint(steps[big] / step).astype(np.int64) - 1))
