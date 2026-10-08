"""Forecast horizon: working steps from "now" to the end of the month (SPEC §10.2).

Steps are at most ``forecast.step_min`` minutes of working-shift time, aligned to shift starts;
the first one may be partial. Each step knows the non-working time before it (repairs go on
off-shift), its random-number slot (calendar position → common random numbers), its shift and
day. Planned maintenance and CKD dispatches follow :mod:`twin_core.schedule` on the configured
calendar; extra shifts of a what-if add working time only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import numpy.typing as npt

from twin_core.calendar import PlantCalendar, ShiftInstance
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.config.plant import ExtraWorkingDay
from twin_core.forecast.overrides import ExtraShift
from twin_core.schedule import (
    ckd_lot_sizes,
    equipment_positions,
    is_delivery_day,
    pm_due,
    schedule_anchor,
    working_day_index,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class PmEvent:
    step: int
    equipment: str
    minutes: float


@dataclass(frozen=True, slots=True)
class ScheduledLot:
    product: str
    qty: int
    dispatched: datetime
    key: int
    """Random-number slot of the lot's delay (day of month x 16 + product position)."""


@dataclass(frozen=True)
class Horizon:
    as_of: datetime
    month_start: datetime
    month_end: datetime
    step_start: FloatArray
    """Epoch seconds."""
    step_min: FloatArray
    gap_min: FloatArray
    """Non-working minutes between the previous step (or ``as_of``) and this step."""
    slot: IntArray
    shift_index: IntArray
    shift_last: BoolArray
    next_shift_h: FloatArray
    """Hours of the following working shift (for steps that end a shift; 0 at the horizon end)."""
    shifts: tuple[ShiftInstance, ...]
    shift_keys: IntArray
    days: tuple[date, ...]
    day_last_step: IntArray
    """Index of the last step that ends on or before the end of each day (-1: none yet)."""
    pm: tuple[PmEvent, ...]
    lots: tuple[ScheduledLot, ...]
    extra_shifts: tuple[ShiftInstance, ...]
    remaining_shifts: float
    """Working shifts of the configured calendar still ahead (fractional for the current one)."""
    month_shifts: int

    @property
    def n_steps(self) -> int:
        return int(self.step_min.shape[0])

    @property
    def hours(self) -> float:
        return float(self.step_min.sum() / 60.0)


def calendar_with_extra(
    cfg: TwinConfig, extra: list[ExtraShift] | tuple[ExtraShift, ...]
) -> PlantCalendar:
    """The plant calendar with extra working shifts added (union with already-worked shifts)."""
    if not extra:
        return cfg.calendar
    base = cfg.calendar
    codes = list(base.shift_codes)
    entries = {d.date: d for d in cfg.plant.calendar.extra_working_days}
    for item in extra:
        worked = set(base.working_shift_codes(item.date))
        wanted = set(item.shifts if item.shifts is not None else codes)
        union = [c for c in codes if c in worked | wanted]
        entries[item.date] = ExtraWorkingDay(date=item.date, shifts=union, name_ru="what-if")
    calendar = cfg.plant.calendar.model_copy(
        update={"extra_working_days": sorted(entries.values(), key=lambda d: d.date)}
    )
    return PlantCalendar(calendar, cfg.timezone)


def build_horizon(
    cfg: TwinConfig,
    *,
    as_of: datetime,
    month_start: datetime,
    month_end: datetime,
    extra_shifts: list[ExtraShift] | tuple[ExtraShift, ...] = (),
    step_min: int | None = None,
) -> Horizon:
    step = step_min or cfg.simulation.forecast.step_min
    step_s = step * 60.0
    moment = max(ensure_utc(as_of), ensure_utc(month_start))
    m0, m1 = ensure_utc(month_start), ensure_utc(month_end)
    base_cal = cfg.calendar
    cal = calendar_with_extra(cfg, extra_shifts)
    tz = cfg.timezone

    shifts = tuple(s for s in cal.shifts_between(moment, m1, working_only=True) if s.start < m1)
    base_keys = {s.key for s in base_cal.shifts_between(moment, m1, working_only=True)}
    extra = tuple(s for s in shifts if s.key not in base_keys)

    starts: list[float] = []
    mins: list[float] = []
    gaps: list[float] = []
    slots: list[int] = []
    shift_idx: list[int] = []
    last_flag: list[bool] = []
    first_step_of_shift: dict[int, int] = {}
    prev_end = moment
    for j, shift in enumerate(shifts):
        t = shift.start
        while t < shift.end:
            t_next = min(t + timedelta(seconds=step_s), shift.end, m1)
            if t_next > moment:
                begin = max(t, moment)
                if t == shift.start and shift.start >= moment:
                    first_step_of_shift[j] = len(starts)
                starts.append(begin.timestamp())
                mins.append((t_next - begin).total_seconds() / 60.0)
                gaps.append(max(0.0, (begin - prev_end).total_seconds() / 60.0))
                slots.append(int((t - m0).total_seconds() // step_s))
                shift_idx.append(j)
                last_flag.append(t_next >= shift.end)
                prev_end = t_next
            if t_next >= m1:
                break
            t = t_next

    n = len(starts)
    next_h = np.zeros(n)
    for i in range(n):
        if last_flag[i]:
            j = shift_idx[i] + 1
            if j < len(shifts):
                next_h[i] = shifts[j].duration.total_seconds() / 3600.0

    # days from as_of's local date to the month end
    first_day = moment.astimezone(tz).date()
    last_day = (m1 - timedelta(seconds=1)).astimezone(tz).date()
    days: list[date] = []
    day = first_day
    while day <= last_day:
        days.append(day)
        day += _ONE_DAY
    step_end_day = [
        (
            datetime.fromtimestamp(starts[i], tz)
            + timedelta(minutes=mins[i])
            - timedelta(seconds=1)
        ).date()
        for i in range(n)
    ]
    day_last = np.full(len(days), -1, dtype=np.int64)
    k = -1
    for d_i, d in enumerate(days):
        while k + 1 < n and step_end_day[k + 1] <= d:
            k += 1
        day_last[d_i] = k

    # planned maintenance and CKD dispatches on the configured calendar
    anchor = schedule_anchor(cfg)
    positions = equipment_positions(cfg)
    pm: list[PmEvent] = []
    lots: list[ScheduledLot] = []
    product_pos = {p: i for i, p in enumerate(cfg.products)}
    wd_cache: dict[date, int] = {}
    for j, shift in enumerate(shifts):
        if shift.key not in base_keys or j not in first_step_of_shift:
            continue
        d = shift.shift_date
        if d not in wd_cache:
            wd_cache[d] = working_day_index(base_cal, anchor, d)
        wd = wd_cache[d]
        step_i = first_step_of_shift[j]
        for task in pm_due(cfg, shift_code=shift.code, working_day=wd, positions=positions):
            pm.append(PmEvent(step_i, task.equipment, task.duration_min))
        first_codes = base_cal.working_shift_codes(d)
        if first_codes and first_codes[0] == shift.code and is_delivery_day(cfg, wd):
            for product, qty in ckd_lot_sizes(cfg, d).items():
                if qty > 0:
                    lots.append(
                        ScheduledLot(product, qty, shift.start, d.day * 16 + product_pos[product])
                    )

    # working shifts of the configured calendar still ahead (for the required rate)
    month_shifts = [s for s in base_cal.shifts_between(m0, m1, working_only=True) if s.start >= m0]
    remaining = 0.0
    for s in month_shifts:
        if s.end <= moment:
            continue
        span = (s.end - s.start).total_seconds()
        remaining += (s.end - max(s.start, moment)).total_seconds() / span

    return Horizon(
        as_of=moment,
        month_start=m0,
        month_end=m1,
        step_start=np.asarray(starts, dtype=np.float64),
        step_min=np.asarray(mins, dtype=np.float64),
        gap_min=np.asarray(gaps, dtype=np.float64),
        slot=np.asarray(slots, dtype=np.int64),
        shift_index=np.asarray(shift_idx, dtype=np.int64),
        shift_last=np.asarray(last_flag, dtype=np.bool_),
        next_shift_h=next_h,
        shifts=shifts,
        shift_keys=np.asarray(
            [s.shift_date.day * 16 + cal.shift_codes.index(s.code) for s in shifts],
            dtype=np.int64,
        ),
        days=tuple(days),
        day_last_step=day_last,
        pm=tuple(pm),
        lots=tuple(lots),
        extra_shifts=extra,
        remaining_shifts=remaining,
        month_shifts=len(month_shifts),
    )
