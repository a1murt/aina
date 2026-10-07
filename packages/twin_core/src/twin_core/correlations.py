"""Defect correlations (SPEC §11.4): Spearman between the hourly PAINT defect rate and factors.

The caller aggregates the last ``LOOKBACK_DAYS`` (14) days into hours: PQ, defects, mean filter
ΔP and mean humidity per hour. Factors: mean ΔP and the humidity deviation from the centre of the
humidity signal's normal band (``warn_lo..warn_hi`` in ``plant.yaml``: 45–65% → 55%). An insight is
raised when |ρ| ≥ 0.3 and p < 0.05; the points are returned for the scatter plot.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from twin_core.config.plant import Signal
from twin_core.stats import spearman

LOOKBACK_DAYS = 14
MIN_ABS_RHO = 0.3
ALPHA = 0.05
MIN_HOURS = 10
"""Fewer usable hours → no verdict."""


@dataclass(frozen=True, slots=True)
class HourStat:
    """One hour of one area: inspected units, defects and mean process values."""

    hour: datetime
    pq: int
    defects: int
    factors: Mapping[str, float]
    """Factor name → value for this hour (NaN or missing = no data)."""

    @property
    def defect_rate(self) -> float | None:
        return self.defects / self.pq if self.pq > 0 else None


@dataclass(frozen=True, slots=True)
class CorrelationInsight:
    factor: str
    rho: float
    p_value: float
    n: int
    insight: bool
    """|ρ| ≥ ``min_abs_rho`` and p < ``alpha``: show the card."""
    direction: int
    """+1 — defects grow with the factor, −1 — they fall."""
    points: tuple[tuple[float, float], ...]
    """(factor value, defect rate) for the scatter plot."""


def band_center(signal: Signal) -> float:
    """Centre of the signal's normal band (``warn_lo..warn_hi``)."""
    if signal.warn_lo is None or signal.warn_hi is None:
        raise ValueError(f"signal {signal.code} has no warn_lo/warn_hi band")
    return (signal.warn_lo + signal.warn_hi) / 2.0


def deviation(values: Sequence[float], center: float) -> list[float]:
    """|value − centre| (e.g. humidity deviation from 55%)."""
    return [abs(v - center) for v in values]


def defect_correlations(
    hours: Sequence[HourStat],
    factors: Sequence[str],
    *,
    min_abs_rho: float = MIN_ABS_RHO,
    alpha: float = ALPHA,
    min_hours: int = MIN_HOURS,
) -> list[CorrelationInsight]:
    """Spearman ρ between the hourly defect rate and each factor (hours without PQ are skipped).

    Factors with too few usable hours or a constant series are left out of the result.
    """
    out: list[CorrelationInsight] = []
    for name in factors:
        xs: list[float] = []
        ys: list[float] = []
        for h in hours:
            rate = h.defect_rate
            value = h.factors.get(name)
            if rate is None or value is None or not math.isfinite(value):
                continue
            xs.append(float(value))
            ys.append(rate)
        if len(xs) < min_hours:
            continue
        result = spearman(xs, ys)
        if result is None:
            continue
        out.append(
            CorrelationInsight(
                factor=name,
                rho=result.rho,
                p_value=result.p_value,
                n=result.n,
                insight=abs(result.rho) >= min_abs_rho and result.p_value < alpha,
                direction=1 if result.rho >= 0 else -1,
                points=tuple(zip(xs, ys, strict=True)),
            )
        )
    return out
