"""Forecast results (SPEC §10.2): quantiles, P(target), shortfall, histogram, daily fan, deltas.

The result is a pydantic model so that the API publishes its schema (``/director`` needs the fan
P10–P90 by day, the histogram, P(5 500), P(4 800) and base-vs-scenario deltas). Totals are cars:
probabilities and the histogram use totals rounded to whole cars.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from twin_core.config import TwinConfig
from twin_core.forecast.model import Paths, Setup
from twin_core.forecast.params import PlantState, Targets
from twin_core.kpi import plan_to_date, required_rate

FloatArray = npt.NDArray[np.float64]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Quantiles(_Frozen):
    p10: float
    p50: float
    p90: float
    mean: float
    sd: float


class FanPoint(_Frozen):
    date: date
    p10: float
    p50: float
    p90: float
    plan_cum: dict[str, float]
    """Target distributed over the month's working shifts up to the end of the day."""


class Histogram(_Frozen):
    edges: list[float]
    counts: list[int]


class HorizonInfo(_Frozen):
    steps: int
    hours: float
    working_shifts: float
    """Working shifts ahead on the configured calendar (fractional for the current one)."""
    month_shifts: int
    extra_shifts: list[dict[str, str]]
    """``[{date, shift}]`` added by the scenario."""


class Outcome(_Frozen):
    """Distribution of the month total for one set of overrides."""

    summary: Quantiles
    p_reach: dict[str, float]
    expected_shortfall: dict[str, float]


class Delta(_Frozen):
    """Scenario minus base with common random numbers."""

    p50: float
    mean: float
    p_reach: dict[str, float]
    paired: Quantiles
    """Per-run differences of the month total."""


class StateDigest(_Frozen):
    as_of: datetime
    mtd: int
    buffers: dict[str, float]
    open_downs: list[str]
    filter_dp: dict[str, float]
    source: str
    digest: str


class ForecastResult(_Frozen):
    month: str
    as_of: datetime
    mode: str = "fast"
    seed: int
    n_runs: int
    mtd: int
    targets: dict[str, int]
    required_rate: dict[str, float | None]
    """Per remaining working shift to reach each target (``kpi.required_rate``)."""
    horizon: HorizonInfo
    summary: Quantiles
    p_reach: dict[str, float]
    expected_shortfall: dict[str, float]
    histogram: Histogram
    fan: list[FanPoint]
    rework_expected: dict[str, float]
    """Area -> expected defective first passes sent to repair or repaint over the horizon."""
    overrides: dict[str, Any] = Field(default_factory=dict)
    base: Outcome | None = None
    delta: Delta | None = None
    calibration_id: int | None = None
    state: StateDigest
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int | None = None


def _r(value: float, digits: int = 1) -> float:
    return round(float(value), digits)


def quantiles(values: FloatArray) -> Quantiles:
    if values.size == 0:
        return Quantiles(p10=0.0, p50=0.0, p90=0.0, mean=0.0, sd=0.0)
    p10, p50, p90 = np.percentile(values, [10, 50, 90])
    return Quantiles(
        p10=_r(p10),
        p50=_r(p50),
        p90=_r(p90),
        mean=_r(float(values.mean())),
        sd=_r(float(values.std())),
    )


def p_reach(total: FloatArray, targets: dict[str, int]) -> dict[str, float]:
    cars = np.rint(total)
    return {name: _r(float((cars >= qty).mean()), 4) for name, qty in targets.items()}


def outcome(paths: Paths, targets: dict[str, int]) -> Outcome:
    total = paths.total
    return Outcome(
        summary=quantiles(total),
        p_reach=p_reach(total, targets),
        expected_shortfall={
            name: _r(float(np.maximum(qty - total, 0.0).mean())) for name, qty in targets.items()
        },
    )


def delta(base: Paths, scenario: Paths, targets: dict[str, int]) -> Delta:
    diff = scenario.total - base.total
    b, s = p_reach(base.total, targets), p_reach(scenario.total, targets)
    return Delta(
        p50=_r(float(np.percentile(scenario.total, 50) - np.percentile(base.total, 50))),
        mean=_r(float(diff.mean())),
        p_reach={k: _r(s[k] - b[k], 4) for k in targets},
        paired=quantiles(diff),
    )


def _plan_curve(cfg: TwinConfig, setup: Setup, targets: dict[str, int]) -> list[dict[str, float]]:
    """Cumulative plan at the end of each horizon day (targets spread over working shifts)."""
    h = setup.horizon
    shifts = cfg.calendar.shifts_between(h.month_start, h.month_end, working_only=True)
    shifts = [s for s in shifts if s.start >= h.month_start]
    total = len(shifts)
    tz = cfg.timezone
    out: list[dict[str, float]] = []
    for day in h.days:
        done = sum(1 for s in shifts if s.end.astimezone(tz).date() <= day)
        out.append(
            {name: _r(plan_to_date(qty, total, done) or 0.0) for name, qty in targets.items()}
        )
    return out


def build_result(
    cfg: TwinConfig,
    setup: Setup,
    paths: Paths,
    *,
    month: str,
    state: PlantState,
    targets: Targets,
    seed: int,
    n_runs: int,
    base: Paths | None = None,
    calibration_id: int | None = None,
    warnings: list[str] | None = None,
) -> ForecastResult:
    h = setup.horizon
    tg = targets.as_dict()
    total = paths.total
    cars = np.rint(total)
    bins = cfg.simulation.forecast.histogram_bins
    lo, hi = float(cars.min()), float(cars.max())
    if hi - lo < 1.0:
        lo, hi = lo - 0.5, hi + 0.5
    counts, edges = np.histogram(cars, bins=bins, range=(lo, hi))
    plan = _plan_curve(cfg, setup, tg)
    fan: list[FanPoint] = []
    for i, day in enumerate(h.days):
        row = paths.daily[i]
        p10, p50, p90 = np.percentile(row, [10, 50, 90])
        fan.append(FanPoint(date=day, p10=_r(p10), p50=_r(p50), p90=_r(p90), plan_cum=plan[i]))
    rework: dict[str, float] = {}
    for j, area in enumerate(setup.areas):
        rework[area] = _r(float(paths.rework[:, j].mean()))
    tz = cfg.timezone
    own = outcome(paths, tg)
    return ForecastResult(
        month=month,
        as_of=h.as_of,
        seed=seed,
        n_runs=n_runs,
        mtd=setup.mtd,
        targets=tg,
        required_rate={
            name: (
                None
                if (rate := required_rate(qty, setup.mtd, h.remaining_shifts)) is None
                else _r(rate, 4)
            )
            for name, qty in tg.items()
        },
        horizon=HorizonInfo(
            steps=h.n_steps,
            hours=_r(h.hours, 2),
            working_shifts=_r(h.remaining_shifts, 4),
            month_shifts=h.month_shifts,
            extra_shifts=[
                {"date": s.shift_date.isoformat(), "shift": s.code} for s in h.extra_shifts
            ],
        ),
        summary=own.summary,
        p_reach=own.p_reach,
        expected_shortfall=own.expected_shortfall,
        histogram=Histogram(edges=[_r(e, 2) for e in edges], counts=[int(c) for c in counts]),
        fan=fan,
        rework_expected=rework,
        overrides=setup.overrides.normalized(),
        base=None if base is None else outcome(base, tg),
        delta=None if base is None else delta(base, paths, tg),
        calibration_id=calibration_id,
        state=StateDigest(
            as_of=state.as_of.astimezone(tz),
            mtd=state.mtd_output,
            buffers={k: _r(v) for k, v in state.buffers.items()},
            open_downs=[d.equipment for d in state.open_downs],
            filter_dp={k: _r(v) for k, v in state.filter_dp.items()},
            source=state.source,
            digest=state.digest(),
        ),
        warnings=[*state.warnings, *(warnings or [])],
    )
