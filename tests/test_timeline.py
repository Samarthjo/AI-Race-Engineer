import numpy as np

from pitwall.ingest.session_builder import asof_index, exact_index
from pitwall.ingest.timeline import cadence, canonicalize, count_missing


def _frames(*parts: range) -> np.ndarray:
    return np.concatenate([np.arange(p.start, p.stop) for p in parts]).astype(np.uint32)


def test_no_rewind_keeps_everything():
    tl = canonicalize(_frames(range(1, 101)), [])
    assert len(tl.keep) == 100
    assert tl.flashbacks_applied == tl.resets == tl.dropped == 0


def test_flashback_erases_the_overwritten_future():
    # frames 1..10 driven, flashback to 5, then 6..12 driven again
    frames = _frames(range(1, 11), range(6, 13))
    tl = canonicalize(frames, flashback_targets=[5])
    kept = frames[tl.keep]
    assert kept.tolist() == list(range(1, 13))
    assert tl.flashbacks_applied == 1
    assert tl.dropped == 5  # old 6..10
    # the kept 6 is the *new* one (index 10 in receive order)
    assert tl.keep[5] == 10
    assert tl.after_rewind.tolist().index(True) == 5
    assert (tl.segment == 0).all()


def test_rewind_without_flbk_starts_new_segment_and_deletes_nothing():
    frames = _frames(range(1, 11), range(1, 6))
    tl = canonicalize(frames, flashback_targets=[])
    assert len(tl.keep) == 15
    assert tl.resets == 1
    assert tl.segment.tolist() == [0] * 10 + [1] * 5


def test_flbk_far_away_is_not_used():
    frames = _frames(range(1, 101), range(20, 30))
    tl = canonicalize(frames, flashback_targets=[80], tolerance=10)
    assert tl.flashbacks_applied == 0 and tl.resets == 1


def test_duplicates_dropped():
    frames = np.asarray([1, 2, 2, 3, 3, 3, 4], dtype=np.uint32)
    tl = canonicalize(frames, [])
    assert frames[tl.keep].tolist() == [1, 2, 3, 4]
    assert tl.duplicates == 3


def test_cadence_and_missing():
    assert cadence(np.arange(0, 200, 2)) == 2
    ov = np.asarray([1, 2, 3, 7, 8, 9])  # 4,5,6 missing
    assert count_missing(ov, 1) == 3
    skip = np.asarray([False, False, False, True, False, False])
    assert count_missing(ov, 1, skip=skip) == 0
    assert count_missing(np.arange(0, 20, 2), 2) == 0


def test_exact_and_asof_index():
    base = np.asarray([1, 2, 3, 4, 5])
    other = np.asarray([2, 4, 5, 9])
    assert exact_index(base, other).tolist() == [-1, 0, -1, 1, 2]
    assert asof_index(base, np.asarray([0, 3])).tolist() == [0, 0, 1, 1, 1]
    assert asof_index(base, np.asarray([2])).tolist() == [-1, 0, 0, 0, 0]
    # non-increasing input falls back to a dictionary join
    assert exact_index(base, np.asarray([5, 1])).tolist() == [1, -1, -1, -1, 0]
