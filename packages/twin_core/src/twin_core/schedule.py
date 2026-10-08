"""Plant schedules shared by the virtual plant and the forecast (SPEC §6.3, §6.6, §10.2).

* Working-day index: working days counted from the local date of ``clock.backfill_from`` (the
  anchor). Planned maintenance and CKD deliveries cycle on it, so a schedule does not depend on
  where a model run starts.
* Planned maintenance (``simulation.yaml: planned_maintenance``): at the start of the configured
  shift, a unit is due when (working-day index + its global position in ``plant.yaml``, if
  staggered) is divisible by ``every_working_days``.
* CKD lots (``ckd_supply``): on every ``delivery_every_working_days``-th working day, one lot per
  product of ``lot_days_of_plan`` days of the month plan.

Pure functions over :class:`~twin_core.config.TwinConfig`; ``qost_sim`` and
``twin_core.forecast`` both call them, so the virtual plant and its fast model agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from twin_core.calendar import PlantCalendar
from twin_core.config import TwinConfig

_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class PmTask:
    """One planned-maintenance stop due at a shift start."""

    equipment: str
    duration_min: float
    reason: str


def schedule_anchor(cfg: TwinConfig) -> date:
    """Local date from which working days are counted (``clock.backfill_from``)."""
    return cfg.simulation.clock.backfill_from.astimezone(cfg.timezone).date()


def equipment_positions(cfg: TwinConfig) -> dict[str, int]:
    """Global position of every unit in ``plant.yaml`` (areas → lines → equipment)."""
    return {code: index for index, code in enumerate(cfg.equipment)}


def working_day_index(calendar: PlantCalendar, anchor: date, day: date) -> int:
    """Working days in ``[anchor, day)``; negative (minus those in ``[day, anchor)``) before it."""
    index = 0
    if day >= anchor:
        current = anchor
        while current < day:
            if calendar.is_working_day(current):
                index += 1
            current += _ONE_DAY
        return index
    current = day
    while current < anchor:
        if calendar.is_working_day(current):
            index -= 1
        current += _ONE_DAY
    return index


def pm_due(
    cfg: TwinConfig,
    *,
    shift_code: str,
    working_day: int,
    positions: dict[str, int] | None = None,
) -> list[PmTask]:
    """Planned maintenance due at the start of shift ``shift_code`` of working day ``working_day``.

    Order: entries of ``planned_maintenance``, then units in ``plant.yaml`` order.
    """
    pos = positions if positions is not None else equipment_positions(cfg)
    tasks: list[PmTask] = []
    for pm in cfg.simulation.planned_maintenance:
        if pm.shift != shift_code:
            continue
        for code, eq in cfg.equipment.items():
            if eq.type != pm.equipment_type:
                continue
            offset = pos[code] if pm.stagger else 0
            if (working_day + offset) % pm.every_working_days == 0:
                tasks.append(PmTask(code, pm.duration_min, pm.reason))
    return tasks


def is_delivery_day(cfg: TwinConfig, working_day: int) -> bool:
    """CKD lots are dispatched on this working day (at its first shift start)."""
    return working_day % cfg.simulation.ckd_supply.delivery_every_working_days == 0


def ckd_daily_plan(cfg: TwinConfig, day: date) -> dict[str, float]:
    """Planned kits per working day for ``day``'s month, by product (``plant.yaml`` order).

    From the month's ``line_model`` plan over its working days; without a plan for the month —
    the first line's ``plan_rate_per_shift`` × shifts per day × ``product_mix``.
    """
    products = list(cfg.products)
    month = f"{day.year:04d}-{day.month:02d}"
    per_month: dict[str, float] = {}
    for entry in cfg.plant.plan:
        if entry.month == month and entry.level == "line_model" and entry.product:
            per_month[entry.product] = per_month.get(entry.product, 0.0) + entry.qty
    if per_month:
        days = len(cfg.calendar.working_days_in_month(day.year, day.month)) or 1
        return {p: per_month.get(p, 0.0) / days for p in products}
    first_line = cfg.lines[cfg.flow_lines[0]]
    per_day = first_line.plan_rate_per_shift * len(cfg.calendar.shift_codes)
    mix = cfg.simulation.process.product_mix
    return {p: mix.get(p, 0.0) * per_day for p in products}


def ckd_lot_sizes(cfg: TwinConfig, day: date) -> dict[str, int]:
    """Kits per product in the lots dispatched on ``day`` (``lot_days_of_plan`` × daily plan)."""
    lot_days = cfg.simulation.ckd_supply.lot_days_of_plan
    return {p: round(lot_days * qty) for p, qty in ckd_daily_plan(cfg, day).items()}
