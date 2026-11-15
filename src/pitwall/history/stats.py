"""Comparing two groups of laps without fooling yourself.

Medians, not means (one bad lap does not move them much). The noise comes from the laps'
own spread around their group's median (median absolute deviation, scaled to a standard
deviation). A change counts only if it is larger than a minimum meaningful size *and* than
two standard errors; with fewer than ``min_laps`` laps on either side there is no conclusion.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any

MIN_LAPS = 4  # laps on each side of a comparison
MEDIAN_SE = 1.2533  # standard error of a median: 1.2533 * sd / sqrt(n)
MAD_SD = 1.4826  # median absolute deviation -> standard deviation (normal data)
NOT_ENOUGH = "not enough data"


@dataclass
class Change:
    status: str  # improved | worse | unchanged | not enough data
    before: float | None  # median of the earlier group
    after: float | None
    delta: float | None  # after - before, in the metric's own units
    sigma: float | None  # 1 standard error of the delta
    threshold: float | None  # smallest change that counts
    n_before: int
    n_after: int

    def to_dict(self, nd: int = 3) -> dict[str, Any]:
        return {k: round(v, nd) if isinstance(v, float) else v for k, v in asdict(self).items()}


def compare_groups(
    before: list[float],
    after: list[float],
    *,
    better: str,
    min_change: float,
    min_laps: int = MIN_LAPS,
) -> Change:
    """``better`` is "lower" (times, distances to full throttle) or "higher" (speeds...)."""
    nb, na = len(before), len(after)
    if nb < min_laps or na < min_laps:
        return Change(NOT_ENOUGH, None, None, None, None, None, nb, na)
    mb, ma = statistics.median(before), statistics.median(after)
    resid = [abs(x - mb) for x in before] + [abs(x - ma) for x in after]
    sd = MAD_SD * statistics.median(resid)
    sigma = MEDIAN_SE * sd * math.sqrt(1 / nb + 1 / na)
    threshold = max(min_change, 2 * sigma)
    delta = ma - mb
    gain = -delta if better == "lower" else delta
    if gain >= threshold:
        status = "improved"
    elif gain <= -threshold:
        status = "worse"
    else:
        status = "unchanged"
    return Change(status, mb, ma, delta, sigma, threshold, nb, na)
