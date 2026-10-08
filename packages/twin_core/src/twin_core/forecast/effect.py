"""Economic effect of a scenario (SPEC §10.5, ``config/business.yaml``).

effect per run = Δ cars × price × margin + Σ Δ reworks[area] × rework cost[area]
                 − extra shifts × shift cost

with common random numbers (base and scenario paired run by run). Revenue is never an effect
(``reporting.show_revenue: false``): only margin and cars. The scenario «с системой» is
``improvement_defaults`` turned into overrides (:func:`improvement_overrides`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from twin_core.config import TwinConfig
from twin_core.forecast.model import Paths
from twin_core.forecast.overrides import Overrides
from twin_core.forecast.params import CalibrationParams
from twin_core.forecast.result import Quantiles, quantiles

FloatArray = npt.NDArray[np.float64]
YEAR_FACTOR = 12


@dataclass(frozen=True, slots=True)
class Economics:
    price_kzt: float
    margin_rate: float
    rework_cost_kzt: dict[str, float]
    shift_cost_kzt: float

    @property
    def margin_per_car(self) -> float:
        return self.price_kzt * self.margin_rate


class Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    value: float | dict[str, float]
    assumption: bool
    source: str | None = None
    note: str | None = None
    overridden: bool = False


ASSUMPTION_KEYS = ("avg_price_kzt", "margin_rate", "rework_cost_kzt", "saturday_shift_cost_kzt")


def economics(
    cfg: TwinConfig, assumptions: dict[str, Any] | None = None
) -> tuple[Economics, list[Assumption]]:
    """Business parameters from ``business.yaml`` with request-level overrides of their values."""
    bp = cfg.business.params
    given = dict(assumptions or {})
    unknown = sorted(set(given) - set(ASSUMPTION_KEYS))
    if unknown:
        raise ValueError(f"unknown assumption(s): {', '.join(unknown)}")
    rework = dict(bp.rework_cost_kzt.value)
    if "rework_cost_kzt" in given:
        rework.update({k: float(v) for k, v in dict(given["rework_cost_kzt"]).items()})
    econ = Economics(
        price_kzt=float(given.get("avg_price_kzt", bp.avg_price_kzt.value)),
        margin_rate=float(given.get("margin_rate", bp.margin_rate.value)),
        rework_cost_kzt=rework,
        shift_cost_kzt=float(
            given.get("saturday_shift_cost_kzt", bp.saturday_shift_cost_kzt.value)
        ),
    )
    listed = [
        Assumption(
            key="avg_price_kzt",
            value=econ.price_kzt,
            assumption=bp.avg_price_kzt.assumption,
            source=bp.avg_price_kzt.source,
            note=bp.avg_price_kzt.note,
            overridden="avg_price_kzt" in given,
        ),
        Assumption(
            key="margin_rate",
            value=econ.margin_rate,
            assumption=bp.margin_rate.assumption,
            source=bp.margin_rate.source,
            note=bp.margin_rate.note,
            overridden="margin_rate" in given,
        ),
        Assumption(
            key="rework_cost_kzt",
            value=econ.rework_cost_kzt,
            assumption=bp.rework_cost_kzt.assumption,
            source=bp.rework_cost_kzt.source,
            note=bp.rework_cost_kzt.note,
            overridden="rework_cost_kzt" in given,
        ),
        Assumption(
            key="saturday_shift_cost_kzt",
            value=econ.shift_cost_kzt,
            assumption=bp.saturday_shift_cost_kzt.assumption,
            source=bp.saturday_shift_cost_kzt.source,
            note=bp.saturday_shift_cost_kzt.note,
            overridden="saturday_shift_cost_kzt" in given,
        ),
    ]
    return econ, listed


def effect_per_run(
    base: Paths,
    scenario: Paths,
    *,
    areas: tuple[str, ...],
    econ: Economics,
    extra_shifts: int,
) -> FloatArray:
    cars = scenario.total - base.total
    saved = base.rework - scenario.rework  # (runs, lines); line j belongs to areas[j]
    costs = np.array([econ.rework_cost_kzt.get(a, 0.0) for a in areas])
    value = cars * econ.margin_per_car + saved @ costs - extra_shifts * econ.shift_cost_kzt
    return np.asarray(value, dtype=np.float64)


class ReworkSaving(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    units: float
    kzt: float


class EffectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    month: str
    currency: str
    horizon: str
    """``full_month``: a typical whole month from its first day (so ×12 is meaningful)."""
    n_runs: int
    seed: int
    scenario: dict[str, Any]
    month_kzt: Quantiles
    year_kzt: Quantiles
    delta_cars: Quantiles
    margin_kzt: float
    rework_saved: dict[str, ReworkSaving]
    extra_shifts: int
    extra_shift_cost_kzt: float
    assumptions: list[Assumption]
    show_revenue: bool = False
    duration_ms: int | None = None


def _scaled(q: Quantiles, factor: float) -> Quantiles:
    return Quantiles(
        p10=round(q.p10 * factor, 1),
        p50=round(q.p50 * factor, 1),
        p90=round(q.p90 * factor, 1),
        mean=round(q.mean * factor, 1),
        sd=round(q.sd * factor, 1),
    )


def effect_result(
    cfg: TwinConfig,
    base: Paths,
    scenario: Paths,
    *,
    month: str,
    areas: tuple[str, ...],
    econ: Economics,
    assumptions: list[Assumption],
    extra_shifts: int,
    overrides: Overrides,
    n_runs: int,
    seed: int,
) -> EffectResult:
    per_run = effect_per_run(base, scenario, areas=areas, econ=econ, extra_shifts=extra_shifts)
    month_q = quantiles(per_run)
    cars = scenario.total - base.total
    saved = (base.rework - scenario.rework).mean(axis=0)
    rework = {
        area: ReworkSaving(
            units=round(float(saved[j]), 1),
            kzt=round(float(saved[j]) * econ.rework_cost_kzt.get(area, 0.0), 1),
        )
        for j, area in enumerate(areas)
    }
    return EffectResult(
        month=month,
        currency=cfg.business.currency,
        horizon="full_month",
        n_runs=n_runs,
        seed=seed,
        scenario=overrides.normalized(),
        month_kzt=month_q,
        year_kzt=_scaled(month_q, YEAR_FACTOR),
        delta_cars=quantiles(cars),
        margin_kzt=round(float(cars.mean()) * econ.margin_per_car, 1),
        rework_saved=rework,
        extra_shifts=extra_shifts,
        extra_shift_cost_kzt=round(extra_shifts * econ.shift_cost_kzt, 1),
        assumptions=assumptions,
        show_revenue=cfg.business.reporting.show_revenue,
    )


def paint_area(cfg: TwinConfig) -> str | None:
    """The paint area: where the filter units are, else the line with repaints."""
    pf = cfg.simulation.paint_filters
    if pf is not None:
        for code, eq in cfg.equipment.items():
            if eq.type == pf.equipment_type:
                return cfg.area_of_equipment(code).code
    for line in cfg.flow_lines:
        if cfg.lines[line].rework.repaint_share:
            return cfg.area_of_line(line).code
    return None


def improvement_overrides(cfg: TwinConfig, params: CalibrationParams) -> Overrides:
    """``business.yaml: improvement_defaults`` as forecast overrides (scenario «с системой»).

    MTTR x (1 - mttr_reduction) for every class; failure rate x (1 - unplanned_failure_reduction)
    for classes A and B; the filter policy; PAINT defect share down to the target (never up).
    """
    imp = cfg.business.improvement_defaults
    mttr = round(1.0 - imp.mttr_reduction, 6)
    reduction = imp.unplanned_failure_reduction
    mtbf = round(1.0 / (1.0 - reduction), 6) if reduction < 1 else 1000.0
    defect: dict[str, float] = {}
    area = paint_area(cfg)
    paint = params.areas.get(area) if area is not None else None
    if area is not None and paint is not None and paint.rate.mean > imp.paint_defect_rate_target:
        defect[area] = imp.paint_defect_rate_target
    return Overrides(
        mttr_multiplier={"A": mttr, "B": mttr, "C": mttr},
        mtbf_multiplier={"A": mtbf, "B": mtbf},
        filter_policy=imp.filter_policy,
        defect_rate=defect,
    )
