"""Robust statistics shared by analytics and ML (SPEC §11.1–11.4).

* :func:`theil_sen` — Theil–Sen line: slope = median of all pairwise slopes, intercept = median of
  ``y - slope * x`` (Conover). Robust to ~29% outliers; O(n²) pairs, fine for windows of a few
  hundred samples (6 h at 60 s = 360 points = 64 620 pairs).
* :func:`spearman` — Spearman rank correlation with a two-sided p-value (Student t approximation,
  as ``scipy.stats.spearmanr``).

NaN pairs are dropped; functions return ``None`` when there is not enough data.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class Line:
    """Fitted line ``y = intercept + slope * x``."""

    slope: float
    intercept: float
    n: int
    """Number of points used (NaN pairs dropped)."""

    def at(self, x: float) -> float:
        return self.intercept + self.slope * x


def theil_sen_segmented(
    x: Sequence[float] | FloatArray,
    y: Sequence[float] | FloatArray,
    segments: Sequence[int] | npt.NDArray[np.int64],
    *,
    min_level_points: int = 3,
) -> Line | None:
    """Theil–Sen slope from pairs *within* the same segment; level from the last segment.

    A level shift (a replaced filter, a re-based sensor) moves the signal without changing its
    drift: pairs across the shift would bias the slope, so only pairs inside a segment count, and
    the intercept (the value at ``x = 0``) is the median of ``y - slope * x`` over the newest
    segment alone. ``segments`` are non-decreasing ids per point. ``None`` without at least one
    usable pair or with fewer than ``min_level_points`` points in the last segment.
    """
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    sa = np.asarray(segments, dtype=np.int64)
    if not (xa.shape == ya.shape == sa.shape) or xa.ndim != 1:
        raise ValueError("x, y and segments must be 1-D of the same length")
    keep = np.isfinite(xa) & np.isfinite(ya)
    xa, ya, sa = xa[keep], ya[keep], sa[keep]
    n = len(xa)
    if n < 2:
        return None
    i, j = np.triu_indices(n, k=1)
    dx = xa[j] - xa[i]
    valid = (dx != 0) & (sa[i] == sa[j])
    if not valid.any():
        return None
    slope = float(np.median((ya[j][valid] - ya[i][valid]) / dx[valid]))
    last = sa == sa[-1]
    if int(last.sum()) < min_level_points:
        return None
    intercept = float(np.median(ya[last] - slope * xa[last]))
    return Line(slope=slope, intercept=intercept, n=n)


@dataclass(frozen=True, slots=True)
class RankCorrelation:
    rho: float
    p_value: float
    n: int


def _clean(
    x: Sequence[float] | FloatArray, y: Sequence[float] | FloatArray
) -> tuple[FloatArray, FloatArray]:
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    if xa.shape != ya.shape or xa.ndim != 1:
        raise ValueError(f"x and y must be 1-D of the same length, got {xa.shape} and {ya.shape}")
    keep = np.isfinite(xa) & np.isfinite(ya)
    return xa[keep], ya[keep]


def theil_sen(x: Sequence[float] | FloatArray, y: Sequence[float] | FloatArray) -> Line | None:
    """Theil–Sen line through ``(x, y)``; ``None`` with fewer than two distinct ``x``."""
    xa, ya = _clean(x, y)
    n = len(xa)
    if n < 2:
        return None
    i, j = np.triu_indices(n, k=1)
    dx = xa[j] - xa[i]
    valid = dx != 0
    if not valid.any():
        return None
    slopes = (ya[j][valid] - ya[i][valid]) / dx[valid]
    slope = float(np.median(slopes))
    intercept = float(np.median(ya - slope * xa))
    return Line(slope=slope, intercept=intercept, n=n)


def _ranks(values: FloatArray) -> FloatArray:
    """Average ranks (1-based), ties share the mean rank."""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=np.float64)
    start = 0
    n = len(values)
    while start < n:
        end = start + 1
        while end < n and sorted_values[end] == sorted_values[start]:
            end += 1
        ranks[order[start:end]] = (start + end + 1) / 2.0
        start = end
    return ranks


def spearman(
    x: Sequence[float] | FloatArray, y: Sequence[float] | FloatArray
) -> RankCorrelation | None:
    """Spearman's rho and two-sided p-value; ``None`` with < 3 points or a constant input."""
    from scipy.special import stdtr  # local import: scipy is heavy and rarely needed

    xa, ya = _clean(x, y)
    n = len(xa)
    if n < 3:
        return None
    rx, ry = _ranks(xa), _ranks(ya)
    sx, sy = float(np.std(rx)), float(np.std(ry))
    if sx == 0.0 or sy == 0.0:
        return None
    rho = float(np.mean((rx - rx.mean()) * (ry - ry.mean())) / (sx * sy))
    rho = max(-1.0, min(1.0, rho))
    dof = n - 2
    if abs(rho) >= 1.0:
        p_value = 0.0
    else:
        t = abs(rho) * math.sqrt(dof / ((1.0 - rho) * (1.0 + rho)))
        p_value = float(2.0 * stdtr(dof, -t))
    return RankCorrelation(rho=rho, p_value=p_value, n=n)
