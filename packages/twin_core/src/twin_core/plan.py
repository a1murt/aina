"""Monthly plan progress (SPEC §5.8): target, line plan, output MTD, fulfilment, remaining shifts
and the required rate — one pure function for ``GET /plan/progress`` and the director panel.

* Output MTD = finished cars of the month so far: units leaving the last flow line with a
  :data:`twin_core.events.FINISHED_RESULTS` result (``pass`` + ``rework_pass``; imports carry no
  last-line data). The SQL counterpart is ``qost_api.queries.plan.mtd_output`` (shared with the
  forecast, M6).
* Working shifts are counted with the fraction of the running shift still ahead, exactly like
  the forecast horizon (M6), so ``required_rate`` agrees with ``POST /forecast``.
* Plan to date distributes the monthly quantity evenly over working shifts (§5.4
  "план, распределённый по рабочим сменам на текущий момент").

Golden (§5.8): at the month start with no output the required rate is 4 800 / 42 = 114.2857 for
the line plan and 5 500 / 42 = 130.9524 for the plant target.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from twin_core.calendar import PlantCalendar
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.forecast.calibration import month_bounds
from twin_core.kpi import plan_attainment, plan_gap, plan_to_date, required_rate


@dataclass(frozen=True, slots=True)
class ShiftCount:
    """Working shifts of a month at a moment: total, elapsed and remaining (fractional)."""

    total: int
    elapsed: float
    remaining: float


def month_shift_count(
    cal: PlantCalendar, cfg: TwinConfig, month: str, as_of: datetime
) -> ShiftCount:
    """Working shifts of ``month`` (plant-local) before/after ``as_of``; the running shift is split
    by the elapsed fraction (same rule as the forecast horizon)."""
    m0, m1 = month_bounds(cfg, month)
    moment = ensure_utc(as_of)
    shifts = [s for s in cal.shifts_between(m0, m1, working_only=True) if s.start >= m0]
    remaining = 0.0
    for s in shifts:
        if s.end <= moment:
            continue
        span = (s.end - s.start).total_seconds()
        remaining += (s.end - max(s.start, moment)).total_seconds() / span
    return ShiftCount(total=len(shifts), elapsed=len(shifts) - remaining, remaining=remaining)


@dataclass(frozen=True, slots=True)
class TargetProgress:
    """Progress against one monthly quantity (plant target or line plan)."""

    qty: int
    plan_to_date: float | None
    fulfilment: float | None
    """Output MTD / plan to date (``None`` before the first working shift)."""
    remaining_qty: int
    required_rate: float | None
    """Cars per remaining working shift to reach ``qty`` (``None``: no shifts left)."""


@dataclass(frozen=True, slots=True)
class DayOutput:
    day: date
    output: int
    cum_output: int
    cum_plan: dict[str, float]
    """Cumulative plan to the end of the day per target key (even distribution over shifts)."""


@dataclass(frozen=True, slots=True)
class PlanProgress:
    month: str
    as_of: datetime
    mtd_output: int
    shifts: ShiftCount
    targets: dict[str, TargetProgress]
    """``plant_target`` and/or ``line_plan`` (whichever is known)."""
    unallocated: int | None
    """Σ line plans − plant target (DQ-05; negative = the target is not covered)."""
    line_plan_by_product: dict[str, int] = field(default_factory=dict)
    daily: tuple[DayOutput, ...] = ()


def plan_progress(
    cfg: TwinConfig,
    month: str,
    *,
    as_of: datetime,
    mtd_output: int,
    plant_target: int | None,
    line_plan: int | None,
    line_plan_by_product: Mapping[str, int] | None = None,
    daily_output: Mapping[date, int] | None = None,
) -> PlanProgress:
    """Plan progress of ``month`` at ``as_of`` (pure; see the module docstring)."""
    cal = cfg.calendar
    count = month_shift_count(cal, cfg, month, as_of)
    targets: dict[str, TargetProgress] = {}
    for key, qty in (("plant_target", plant_target), ("line_plan", line_plan)):
        if qty is None:
            continue
        to_date = plan_to_date(qty, count.total, count.elapsed)
        targets[key] = TargetProgress(
            qty=qty,
            plan_to_date=to_date,
            fulfilment=plan_attainment(mtd_output, to_date) if count.elapsed > 0 else None,
            remaining_qty=qty - mtd_output,
            required_rate=required_rate(qty, mtd_output, count.remaining),
        )
    unallocated = (
        int(plan_gap(line_plan, plant_target))
        if plant_target is not None and line_plan is not None
        else None
    )
    return PlanProgress(
        month=month,
        as_of=ensure_utc(as_of),
        mtd_output=mtd_output,
        shifts=count,
        targets=targets,
        unallocated=unallocated,
        line_plan_by_product=dict(line_plan_by_product or {}),
        daily=_daily(cfg, month, targets, count.total, daily_output or {}),
    )


def _daily(
    cfg: TwinConfig,
    month: str,
    targets: Mapping[str, TargetProgress],
    total_shifts: int,
    output: Mapping[date, int],
) -> tuple[DayOutput, ...]:
    """Cumulative fact and plan per calendar day of the month (director chart)."""
    cal = cfg.calendar
    m0, m1 = month_bounds(cfg, month)
    first = m0.astimezone(cfg.timezone).date()
    last = (m1.astimezone(cfg.timezone)).date()
    days: list[DayOutput] = []
    cum = 0
    shifts_done = 0
    day = first
    while day < last:
        shifts_done += len(cal.shifts_on(day, working_only=True))
        cum += output.get(day, 0)
        plan = {
            key: (t.qty * shifts_done / total_shifts if total_shifts else 0.0)
            for key, t in targets.items()
        }
        days.append(DayOutput(day, output.get(day, 0), cum, plan))
        day = date.fromordinal(day.toordinal() + 1)
    return tuple(days)


def plan_progress_json(progress: PlanProgress, *, digits: int = 4) -> dict[str, object]:
    """JSON body of ``GET /plan/progress`` (numbers rounded to ``digits`` for display)."""

    def r(value: float | None) -> float | None:
        return None if value is None else round(value, digits)

    return {
        "month": progress.month,
        "as_of": progress.as_of.isoformat().replace("+00:00", "Z"),
        "mtd_output": progress.mtd_output,
        "shifts": {
            "total": progress.shifts.total,
            "elapsed": r(progress.shifts.elapsed),
            "remaining": r(progress.shifts.remaining),
        },
        "targets": {
            key: {
                "qty": t.qty,
                "plan_to_date": r(t.plan_to_date),
                "fulfilment": r(t.fulfilment),
                "remaining_qty": t.remaining_qty,
                "required_rate": r(t.required_rate),
            }
            for key, t in progress.targets.items()
        },
        "unallocated": progress.unallocated,
        "line_plan_by_product": progress.line_plan_by_product,
        "daily": [
            {
                "date": d.day.isoformat(),
                "output": d.output,
                "cum_output": d.cum_output,
                "cum_plan": {k: round(v, 2) for k, v in d.cum_plan.items()},
            }
            for d in progress.daily
        ],
    }


def line_plan_by_product(
    cfg: TwinConfig, month: str, rows: Sequence[tuple[str, str | None, str | None, int]] = ()
) -> dict[str, int]:
    """Line plan per product of ``month`` from ``production_plan`` rows ``(level, line, product,
    qty)``; falls back to ``plant.yaml: plan`` when no line rows are given."""
    out: dict[str, int] = {}
    source = rows or [
        (e.level, e.line, e.product, e.qty) for e in cfg.plant.plan if e.month == month
    ]
    for level, _line, product, qty in source:
        if level == "line_model" and product is not None:
            out[product] = out.get(product, 0) + qty
    return out


__all__ = [
    "DayOutput",
    "PlanProgress",
    "ShiftCount",
    "TargetProgress",
    "line_plan_by_product",
    "month_shift_count",
    "plan_progress",
    "plan_progress_json",
]
