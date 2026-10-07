"""Rule on signals for PdM types without a LightGBM model (SPEC §11.1: fixture, oven, …).

Per signal of the type with warning thresholds (``plant.yaml``), from the feature row:

* the 1-hour mean beyond ``warn_hi`` / ``warn_lo`` → risk ≥ ``pdm_warn_p``, growing linearly to
  ``pdm_crit_p`` as the mean reaches ``limit_hi`` (or the edge of the physical range);
* otherwise the 4-hour Theil–Sen trend extrapolated over the label horizon crossing the warning
  threshold → half of ``pdm_warn_p`` (a watch, no alert);
* otherwise 0.

The unit's risk is the largest signal risk. It is a rule score in the same 0–1 scale as the model
probability, not a calibrated probability (the model card / UI say so).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from qost_ml.spec import PdmSpec, SignalSpec


@dataclass(frozen=True, slots=True)
class SignalRisk:
    signal: str
    risk: float
    feature: str
    """The feature that triggered the risk (for the explanation text)."""
    value: float


def _signal_risk(sig: SignalSpec, row: Mapping[str, float], spec: PdmSpec) -> SignalRisk | None:
    mean_name = f"{sig.code}_mean_1h"
    trend_name = f"{sig.code}_slope_4h"
    mean = row.get(mean_name, math.nan)
    trend = row.get(trend_name, math.nan)
    if math.isnan(mean):
        return None
    if sig.warn_hi is not None and mean >= sig.warn_hi:
        top = sig.limit_hi if sig.limit_hi is not None else sig.hi
        span_up = top - sig.warn_hi
        depth = (mean - sig.warn_hi) / span_up if span_up > 0 else 1.0
        return SignalRisk(sig.code, _scale(depth, spec), mean_name, mean)
    if sig.warn_lo is not None and mean <= sig.warn_lo:
        span_down = sig.warn_lo - sig.lo
        depth = (sig.warn_lo - mean) / span_down if span_down > 0 else 1.0
        return SignalRisk(sig.code, _scale(depth, spec), mean_name, mean)
    if not math.isnan(trend):
        projected = mean + trend / 4.0 * spec.horizon_h
        crosses_hi = sig.warn_hi is not None and trend > 0 and projected >= sig.warn_hi
        crosses_lo = sig.warn_lo is not None and trend < 0 and projected <= sig.warn_lo
        if crosses_hi or crosses_lo:
            return SignalRisk(sig.code, spec.warn_p / 2.0, trend_name, trend)
    return SignalRisk(sig.code, 0.0, mean_name, mean)


def _scale(depth: float, spec: PdmSpec) -> float:
    return spec.warn_p + (spec.crit_p - spec.warn_p) * min(1.0, max(0.0, depth))


def rule_risk(
    equipment_type: str, row: Mapping[str, float], spec: PdmSpec
) -> tuple[float, list[SignalRisk]]:
    """(risk 0–1, per-signal risks sorted from the highest) for one feature row."""
    risks = [
        r
        for sig in spec.types[equipment_type].signals
        if (r := _signal_risk(sig, row, spec)) is not None
    ]
    risks.sort(key=lambda r: (-r.risk, r.signal))
    return (risks[0].risk if risks else 0.0), risks
