"""ISO 22400-2 KPIs as pure functions (SPEC §5.4-§5.8, FR-KPI-01..05).

Units: time elements (POT, PDOT, ...) are minutes; planned run times per unit (PRI) and their sums
are seconds — exactly as stored in ``kpi_shift`` (``pot`` … in minutes, ``pri_good_s`` in seconds).
Parameter names carry the unit (``_min`` / ``_s`` / ``_h``).

Every ratio returns ``None`` when its denominator is zero (SPEC §5.4: "деление на 0 → null").
Full precision is kept; comparisons with thresholds use :func:`round_fraction` (4 digits) and
:func:`round_pp` (0.1 percentage point), see SPEC §5.4 "Округление".

Time model (one line, one interval)::

    POT  planned operating time (calendar shift length, 0 on non-working days)
    PDOT planned downtime inside POT (reasons with planned=true)
    PBT  = POT - PDOT                       planned busy time
    ADOT unplanned downtime >= microstop threshold
    ADET delays: STARVED + BLOCKED
    AUST changeover
    APT  = PBT - ADOT - ADET - AUST          includes DEGRADED running and microstops

    A = APT / PBT,  E = sum PRI(PQ) / APT,  QR = GQ / PQ,  OEE = A * E * QR = sum PRI(GQ) / PBT
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import overload

FRACTION_DIGITS = 4
"""Fractions (A, E, QR, OEE, defect rate) are compared and reported with 4 decimals."""
PP_DIGITS = 1
"""Percentages are compared with 0.1 percentage point resolution."""
SECONDS_PER_MINUTE = 60.0
MINUTES_PER_HOUR = 60.0
_EPS = 1e-9


# --------------------------------------------------------------------------- basics


def ratio(numerator: float, denominator: float) -> float | None:
    """``numerator / denominator`` or ``None`` for a zero denominator."""
    if denominator == 0:
        return None
    return numerator / denominator


@overload
def round_fraction(value: float) -> float: ...
@overload
def round_fraction(value: None) -> None: ...
@overload
def round_fraction(value: float | None) -> float | None: ...
def round_fraction(value: float | None) -> float | None:
    """Round a fraction to :data:`FRACTION_DIGITS` decimals (``None`` passes through)."""
    return None if value is None else round(value, FRACTION_DIGITS)


@overload
def round_pp(value: float) -> float: ...
@overload
def round_pp(value: None) -> None: ...
@overload
def round_pp(value: float | None) -> float | None: ...
def round_pp(value: float | None) -> float | None:
    """Round a percentage to :data:`PP_DIGITS` decimals (``None`` passes through)."""
    return None if value is None else round(value, PP_DIGITS)


@overload
def to_percent(value: float) -> float: ...
@overload
def to_percent(value: None) -> None: ...
@overload
def to_percent(value: float | None) -> float | None: ...
def to_percent(value: float | None) -> float | None:
    """Fraction -> percent (``0.975`` -> ``97.5``)."""
    return None if value is None else value * 100.0


def fraction_as_pp(value: float | None) -> float | None:
    """Fraction rounded the way thresholds see it, as percent with 0.1 pp: 0.97504 -> 97.5."""
    return round_pp(to_percent(round_fraction(value)))


def pri_seconds(ict_seconds: float, cycle_factor: float = 1.0) -> float:
    """Planned run time per unit: ``ict_seconds x cycle_factor(product)`` (SPEC §5.4)."""
    if ict_seconds <= 0 or cycle_factor <= 0:
        raise ValueError("ict_seconds and cycle_factor must be positive")
    return ict_seconds * cycle_factor


# --------------------------------------------------------------------------- ISO 22400 KPIs


def availability(apt_min: float, pbt_min: float) -> float | None:
    """A = APT / PBT."""
    return ratio(apt_min, pbt_min)


def effectiveness(pri_produced_s: float, apt_min: float) -> float | None:
    """E = sum PRI(PQ) / APT (performance)."""
    return ratio(pri_produced_s, apt_min * SECONDS_PER_MINUTE)


def quality_ratio(gq: int, pq: int) -> float | None:
    """QR = GQ / PQ; also the first-pass yield (FPY) of the line."""
    return ratio(gq, pq)


fpy = quality_ratio
"""First-pass yield of a line equals its quality ratio (SPEC §5.4)."""


def oee(a: float | None, e: float | None, qr: float | None) -> float | None:
    """OEE = A x E x QR (``None`` if any factor is undefined)."""
    if a is None or e is None or qr is None:
        return None
    return a * e * qr


def oee_from_good(pri_good_s: float, pbt_min: float) -> float | None:
    """OEE = sum PRI(GQ) / PBT — the identity form, equal to A x E x QR."""
    return ratio(pri_good_s, pbt_min * SECONDS_PER_MINUTE)


def own_availability(apt_min: float, adet_min: float, pbt_min: float) -> float | None:
    """Availability without flow delays (diagnostic): (APT + ADET) / PBT."""
    return ratio(apt_min + adet_min, pbt_min)


def defect_rate(pq: int, gq: int) -> float | None:
    """Defect share: (PQ - GQ) / PQ."""
    return ratio(pq - gq, pq)


def rty(fpys: Iterable[float | None]) -> float | None:
    """Rolled throughput yield = product of the FPYs along the flow (``None`` if any unknown)."""
    values = list(fpys)
    if not values or any(v is None for v in values):
        return None
    return math.prod(v for v in values if v is not None)


def mtbf_h(running_min: float, failures: int) -> float | None:
    """MTBF in hours: running time / number of unplanned failures >= microstop threshold."""
    return ratio(running_min / MINUTES_PER_HOUR, failures)


def mttr_min(repair_min: float, failures: int) -> float | None:
    """MTTR in minutes: total duration of those failures / their number."""
    return ratio(repair_min, failures)


def is_microstop(duration_s: float, threshold_s: float) -> bool:
    """An unplanned stop shorter than the threshold is a microstop (performance loss, in APT)."""
    return duration_s < threshold_s


@dataclass(frozen=True, slots=True)
class UnplannedStops:
    """Unplanned stops of a line split by the microstop threshold (minutes)."""

    adot_min: float
    """Stops >= threshold: availability loss (ADOT)."""
    microstop_min: float
    """Stops < threshold: performance loss, part of APT."""
    failures: int
    """Number of stops >= threshold (MTBF/MTTR denominator)."""


def split_unplanned_stops(durations_s: Iterable[float], threshold_s: float) -> UnplannedStops:
    """Classify unplanned stop durations into ADOT failures and microstops."""
    adot_s = micro_s = 0.0
    failures = 0
    for duration in durations_s:
        if duration < 0:
            raise ValueError(f"negative stop duration {duration}")
        if is_microstop(duration, threshold_s):
            micro_s += duration
        else:
            adot_s += duration
            failures += 1
    return UnplannedStops(
        adot_min=adot_s / SECONDS_PER_MINUTE,
        microstop_min=micro_s / SECONDS_PER_MINUTE,
        failures=failures,
    )


# --------------------------------------------------------------------------- time model


@dataclass(frozen=True, slots=True)
class TimeModel:
    """ISO 22400-2 time elements of one line over one interval, minutes.

    ``microstop`` is informative: microstops are inside APT (they cost performance, not
    availability). ``starved`` + ``blocked`` = ADET.
    """

    pot: float
    pdot: float = 0.0
    adot: float = 0.0
    starved: float = 0.0
    blocked: float = 0.0
    aust: float = 0.0
    microstop: float = 0.0

    def __post_init__(self) -> None:
        for name in ("pot", "pdot", "adot", "starved", "blocked", "aust", "microstop"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.pdot > self.pot + _EPS:
            raise ValueError(f"planned downtime {self.pdot} exceeds POT {self.pot}")
        if self.adot + self.adet + self.aust > self.pbt + _EPS:
            raise ValueError("ADOT + ADET + AUST exceed PBT")
        if self.microstop > self.apt + _EPS:
            raise ValueError("microstops exceed APT")

    @property
    def adet(self) -> float:
        return self.starved + self.blocked

    @property
    def pbt(self) -> float:
        return self.pot - self.pdot

    @property
    def apt(self) -> float:
        return self.pbt - self.adot - self.adet - self.aust


@dataclass(frozen=True, slots=True)
class ShiftKpi:
    """KPIs of one line for one shift (or any interval), as stored in ``kpi_shift``."""

    pot_min: float
    pdot_min: float
    pbt_min: float
    apt_min: float
    adot_min: float
    adet_min: float
    aust_min: float
    microstop_min: float
    pq: int
    gq: int
    pri_produced_s: float
    pri_good_s: float
    availability: float | None
    effectiveness: float | None
    quality_ratio: float | None
    oee: float | None
    own_availability: float | None
    defect_rate: float | None
    failures: int | None = None
    repair_min: float | None = None
    mtbf_h: float | None = None
    mttr_min: float | None = None

    @property
    def fpy(self) -> float | None:
        return self.quality_ratio

    @property
    def lost_min(self) -> float:
        """Time lost against the plan: PBT - APT."""
        return self.pbt_min - self.apt_min

    @property
    def defects(self) -> int:
        return self.pq - self.gq


def _check_counts(pq: int, gq: int) -> None:
    if pq < 0 or gq < 0:
        raise ValueError("produced and good quantities must not be negative")
    if gq > pq:
        raise ValueError(f"good quantity {gq} exceeds produced quantity {pq}")


def compute_shift_kpi(
    time: TimeModel,
    *,
    pq: int,
    gq: int,
    pri_produced_s: float,
    pri_good_s: float,
    failures: int | None = None,
    repair_min: float | None = None,
) -> ShiftKpi:
    """All KPIs of an interval from its time model and counts (event path)."""
    _check_counts(pq, gq)
    a = availability(time.apt, time.pbt)
    e = effectiveness(pri_produced_s, time.apt)
    qr = quality_ratio(gq, pq)
    running_min = time.apt
    return ShiftKpi(
        pot_min=time.pot,
        pdot_min=time.pdot,
        pbt_min=time.pbt,
        apt_min=time.apt,
        adot_min=time.adot,
        adet_min=time.adet,
        aust_min=time.aust,
        microstop_min=time.microstop,
        pq=pq,
        gq=gq,
        pri_produced_s=pri_produced_s,
        pri_good_s=pri_good_s,
        availability=a,
        effectiveness=e,
        quality_ratio=qr,
        oee=oee_from_good(pri_good_s, time.pbt),
        own_availability=own_availability(time.apt, time.adet, time.pbt),
        defect_rate=defect_rate(pq, gq),
        failures=failures,
        repair_min=repair_min,
        mtbf_h=None if failures is None else mtbf_h(running_min, failures),
        mttr_min=None if failures is None or repair_min is None else mttr_min(repair_min, failures),
    )


def aggregate_shift_kpi(
    *, pot_min: float, worked_min: float, produced: int, good: int, ict_seconds: float
) -> ShiftKpi:
    """Import path: KPIs from a shift aggregate without events (SPEC §5.4).

    POT = shift length, PDOT = 0 (planned downtime cannot be attributed to a shift, D1),
    ADET = 0, APT = reported working time, PQ = "Факт", GQ = "Факт" - "Брак"; the lost time
    PBT - APT counts as ADOT. The model is unknown, so PRI = ICT (cycle factor 1).
    """
    _check_counts(produced, good)
    if pot_min < 0 or worked_min < 0:
        raise ValueError("shift length and worked time must not be negative")
    pri = pri_seconds(ict_seconds)
    pbt = pot_min
    a = availability(worked_min, pbt)
    e = effectiveness(pri * produced, worked_min)
    qr = quality_ratio(good, produced)
    return ShiftKpi(
        pot_min=pot_min,
        pdot_min=0.0,
        pbt_min=pbt,
        apt_min=worked_min,
        adot_min=pbt - worked_min,
        adet_min=0.0,
        aust_min=0.0,
        microstop_min=0.0,
        pq=produced,
        gq=good,
        pri_produced_s=pri * produced,
        pri_good_s=pri * good,
        availability=a,
        effectiveness=e,
        quality_ratio=qr,
        oee=oee_from_good(pri * good, pbt),
        own_availability=own_availability(worked_min, 0.0, pbt),
        defect_rate=defect_rate(produced, good),
    )


# --------------------------------------------------------------------------- capacity (§5.7)


def capacity_loss_min(duration_min: float, degraded_capacity: float, *, planned: bool) -> float:
    """Effective line capacity lost by an equipment stop (SPEC §5.7).

    Unplanned: ``duration x (1 - degraded_capacity)`` (class A: all of it; class B with a manual
    bypass at 50%: half). Planned stops are excluded from PBT and are not a loss.
    """
    if duration_min < 0:
        raise ValueError("duration must not be negative")
    if not 0.0 <= degraded_capacity <= 1.0:
        raise ValueError("degraded_capacity must be within [0, 1]")
    if planned:
        return 0.0
    return duration_min * (1.0 - degraded_capacity)


def minutes_to_units(minutes: float, ict_seconds: float) -> float | None:
    """Lost minutes expressed in cars: ``minutes x 60 / ICT`` (FR-KPI-05)."""
    return ratio(minutes * SECONDS_PER_MINUTE, ict_seconds)


# --------------------------------------------------------------------------- flow (§5.6)


def buffer_change(upstream_produced: float, downstream_produced: float) -> float:
    """Change of the buffer between two areas over a period: upstream out - downstream out.

    Negative = the buffer was drained (downstream produced more than it received).
    """
    return upstream_produced - downstream_produced


# --------------------------------------------------------------------------- plan (§5.8)


def plan_gap(line_plans_total: float, plant_target: float) -> float:
    """Unallocated plan: sum of line plans - plant target (negative = target not covered)."""
    return line_plans_total - plant_target


def plan_to_date(plan_qty: float, shifts_total: float, shifts_elapsed: float) -> float | None:
    """Monthly plan distributed evenly over working shifts, up to ``shifts_elapsed``."""
    share = ratio(shifts_elapsed, shifts_total)
    return None if share is None else plan_qty * share


def plan_attainment(good_mtd: float, plan_to_date_qty: float | None) -> float | None:
    """Plan fulfilment MTD = good finished cars since month start / plan to date."""
    if plan_to_date_qty is None:
        return None
    return ratio(good_mtd, plan_to_date_qty)


def required_rate(plan_qty: float, produced_mtd: float, remaining_shifts: float) -> float | None:
    """Rate per shift needed to meet the plan: (plan - output MTD) / remaining working shifts."""
    if remaining_shifts <= 0:
        return None
    return (plan_qty - produced_mtd) / remaining_shifts


# --------------------------------------------------------------------------- loss tree (FR-KPI-05)


class LossCategory(StrEnum):
    """Loss tree categories (FR-KPI-05)."""

    PLANNED_DOWNTIME = "planned_downtime"
    UNPLANNED_DOWNTIME = "unplanned_downtime"
    STARVED = "starved"
    BLOCKED = "blocked"
    CHANGEOVER = "changeover"
    MICROSTOPS = "microstops"
    SPEED = "speed"
    QUALITY = "quality"


@dataclass(frozen=True, slots=True)
class DowntimeShare:
    """Part of the unplanned downtime (ADOT) of a line attributed to a reason and a unit."""

    minutes: float
    reason_code: str | None = None
    equipment: str | None = None


@dataclass(frozen=True, slots=True)
class LossItem:
    category: LossCategory
    minutes: float
    units: float | None
    """Minutes expressed in cars (``minutes x 60 / ICT``)."""
    line: str | None = None
    reason_code: str | None = None
    equipment: str | None = None


@dataclass(frozen=True, slots=True)
class LossTree:
    """Flat list of loss items with category totals."""

    items: tuple[LossItem, ...] = field(default_factory=tuple)

    def minutes(self, category: LossCategory | None = None) -> float:
        return sum(i.minutes for i in self.items if category is None or i.category == category)

    def units(self, category: LossCategory | None = None) -> float:
        return sum(i.units or 0.0 for i in self.items if category is None or i.category == category)

    def by_category(self) -> dict[LossCategory, tuple[float, float]]:
        """``category -> (minutes, units)`` for every category present, in enum order."""
        present = {i.category for i in self.items}
        return {c: (self.minutes(c), self.units(c)) for c in LossCategory if c in present}

    def __add__(self, other: LossTree) -> LossTree:
        return LossTree(self.items + other.items)


def line_loss_tree(
    *,
    line: str,
    ict_seconds: float,
    time: TimeModel,
    pri_produced_s: float,
    pri_good_s: float,
    unplanned: Sequence[DowntimeShare] = (),
) -> LossTree:
    """Loss tree of one line for one interval (minutes and cars).

    The categories partition POT exactly::

        POT = PDOT + ADOT + STARVED + BLOCKED + AUST + MICROSTOPS + SPEED + QUALITY + PRI(GQ)

    ``unplanned`` breaks ADOT down by reason and equipment; whatever it does not cover is reported
    as one unattributed item. Speed loss is ``APT - microstops - PRI(PQ)`` and may be negative
    when the ICT is set too slow (DQ-06).
    """

    def item(category: LossCategory, minutes: float, **keys: str | None) -> LossItem:
        units = minutes_to_units(minutes, ict_seconds)
        return LossItem(category, minutes, units, line=line, **keys)

    items = [item(LossCategory.PLANNED_DOWNTIME, time.pdot)]
    attributed = 0.0
    for share in unplanned:
        attributed += share.minutes
        items.append(
            item(
                LossCategory.UNPLANNED_DOWNTIME,
                share.minutes,
                reason_code=share.reason_code,
                equipment=share.equipment,
            )
        )
    if attributed > time.adot + _EPS:
        raise ValueError(f"attributed downtime {attributed} exceeds ADOT {time.adot}")
    rest = time.adot - attributed
    if rest > _EPS or not unplanned:
        items.append(item(LossCategory.UNPLANNED_DOWNTIME, rest))
    produced_min = pri_produced_s / SECONDS_PER_MINUTE
    good_min = pri_good_s / SECONDS_PER_MINUTE
    items += [
        item(LossCategory.STARVED, time.starved),
        item(LossCategory.BLOCKED, time.blocked),
        item(LossCategory.CHANGEOVER, time.aust),
        item(LossCategory.MICROSTOPS, time.microstop),
        item(LossCategory.SPEED, time.apt - time.microstop - produced_min),
        item(LossCategory.QUALITY, produced_min - good_min),
    ]
    return LossTree(tuple(items))
