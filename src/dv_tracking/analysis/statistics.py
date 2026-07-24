"""
Descriptive statistics for the per-site privacy findings.

Median / IQR / min / max / selected percentiles and simple proportions; no inferential tests, no confidence intervals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90, 99)


def percentile_of(sorted_values: list[float], percentile: int) -> float:
    """
    The percentile of a sorted sample using linear interpolation between ranks.

    Args:
        sorted_values: The sample values in ascending order; must be non-empty.
        percentile: The percentile to compute, from 0 to 100.
    Returns:
        value: The interpolated percentile value.
    """
    size = len(sorted_values)
    if size == 1:
        return sorted_values[0]
    rank = (percentile / 100.0) * (size - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    fraction = rank - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def proportion(count: int, total: int) -> float:
    """
    The proportion of a count out of a total.

    Args:
        count: The number of items satisfying the condition.
        total: The total number of items.
    Returns:
        fraction: count / total, or 0.0 when total is zero.
    """
    if total == 0:
        return 0.0
    return count / total


@dataclass(frozen=True)
class DistributionSummary:
    """Descriptive summary of one sample."""

    n: int
    median: float
    iqr_low: float
    iqr_high: float
    minimum: float
    maximum: float
    percentiles: dict[int, float]

    @classmethod
    def from_values(
        cls, values: list[float], *, percentiles: tuple[int, ...] = DEFAULT_PERCENTILES
    ) -> DistributionSummary:
        """
        Summarise one sample with median, IQR, min, max, and selected percentiles.

        Args:
            values: The sample values; an empty sample yields a zeroed summary.
            percentiles: The percentiles to report.
        Returns:
            summary: The descriptive summary of the sample.
        """
        if not values:
            return cls(
                n=0,
                median=0.0,
                iqr_low=0.0,
                iqr_high=0.0,
                minimum=0.0,
                maximum=0.0,
                percentiles={point: 0.0 for point in percentiles},
            )
        ordered = sorted(float(value) for value in values)
        return cls(
            n=len(ordered),
            median=percentile_of(ordered, 50),
            iqr_low=percentile_of(ordered, 25),
            iqr_high=percentile_of(ordered, 75),
            minimum=ordered[0],
            maximum=ordered[-1],
            percentiles={point: percentile_of(ordered, point) for point in percentiles},
        )
