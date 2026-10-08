"""What-if overrides of the fast model (FR-FC-02).

Semantics:

* ``defect_rate[area]`` — new mean defect share; each run's posterior draw is rescaled by
  ``value / posterior mean`` (keeps the spread; monotone in the value);
* ``mtbf_multiplier[equipment | A | B | C]`` — failure rate divided by the multiplier
  (an equipment key beats its class key);
* ``mttr_multiplier[...]`` — duration of new failures multiplied (open repairs and filter swaps
  unchanged);
* ``ict_seconds[line]`` — ideal cycle time of the line;
* ``buffer_capacity[buffer]`` — capacity;
* ``extra_shifts[{date, shifts}]`` — additional working shifts (``shifts`` omitted = every
  calendar shift); no planned maintenance and no CKD deliveries are added on them;
* ``filter_policy`` — ``on_limit`` | ``predictive_shift_change``;
* ``ckd_delay_days[product]`` — the next lot of the product (in transit or scheduled) arrives
  that many days later (as scenario S5).

:func:`check_overrides` validates codes and dates against the configuration and returns issues
in the RFC 7807 ``validation`` shape; unknown codes come with the closest valid code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from twin_core.aliases import closest_code
from twin_core.calendar import PlantCalendar
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.domain import FilterPolicy

CLASSES = ("A", "B", "C")

DefectShare = Annotated[float, Field(ge=0.0, le=0.5)]
MtbfFactor = Annotated[float, Field(gt=0.0, le=1000.0)]
MttrFactor = Annotated[float, Field(gt=0.0, le=10.0)]
IctSeconds = Annotated[float, Field(ge=30.0, le=3600.0)]
Capacity = Annotated[int, Field(ge=0, le=500)]
DelayDays = Annotated[float, Field(ge=0.0, le=31.0)]


class ExtraShift(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    shifts: list[str] | None = None
    """Shift codes; ``None`` = every calendar shift."""


class Overrides(BaseModel):
    """FR-FC-02 overrides; an empty object is the baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    defect_rate: dict[str, DefectShare] = Field(default_factory=dict)
    mtbf_multiplier: dict[str, MtbfFactor] = Field(default_factory=dict)
    mttr_multiplier: dict[str, MttrFactor] = Field(default_factory=dict)
    ict_seconds: dict[str, IctSeconds] = Field(default_factory=dict)
    buffer_capacity: dict[str, Capacity] = Field(default_factory=dict)
    extra_shifts: Annotated[list[ExtraShift], Field(max_length=62)] = Field(default_factory=list)
    filter_policy: FilterPolicy | None = None
    ckd_delay_days: dict[str, DelayDays] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.normalized()

    def normalized(self) -> dict[str, Any]:
        """JSON form without empty fields (stored in ``forecast_run.overrides``)."""
        return self.model_dump(mode="json", exclude_defaults=True)

    def merged(self, other: Overrides) -> Overrides:
        """``self`` with every non-empty field of ``other`` applied on top (dicts merge)."""
        data = self.model_dump()
        for key, value in other.model_dump(exclude_defaults=True).items():
            if isinstance(value, dict):
                data[key] = {**data.get(key, {}), **value}
            elif isinstance(value, list):
                data[key] = [*data.get(key, []), *value]
            else:
                data[key] = value
        return Overrides.model_validate(data)


@dataclass(frozen=True, slots=True)
class OverrideIssue:
    loc: tuple[str | int, ...]
    msg: str
    type: str = "value_error"

    def as_problem(self, prefix: tuple[str, ...] = ("body", "overrides")) -> dict[str, Any]:
        return {"loc": [*prefix, *self.loc], "msg": self.msg, "type": self.type}


def _unknown(kind: str, value: str, valid: list[str]) -> str:
    hint = closest_code(value, valid)
    tail = f"; did you mean '{hint}'?" if hint else f" (known: {', '.join(valid)})"
    return f"unknown {kind} '{value}'{tail}"


def check_overrides(
    cfg: TwinConfig,
    overrides: Overrides,
    *,
    month_start: datetime,
    month_end: datetime,
    as_of: datetime,
    calendar: PlantCalendar | None = None,
) -> list[OverrideIssue]:
    """Codes and dates of ``overrides`` against the configuration and the forecast month."""
    issues: list[OverrideIssue] = []
    defect_areas = [a for a in cfg.simulation.defects.per_area if a in cfg.areas]
    eq_keys = [*cfg.equipment, *CLASSES]
    checks: list[tuple[str, dict[str, Any], list[str], str]] = [
        ("defect_rate", overrides.defect_rate, defect_areas, "area"),
        ("mtbf_multiplier", overrides.mtbf_multiplier, eq_keys, "equipment or class"),
        ("mttr_multiplier", overrides.mttr_multiplier, eq_keys, "equipment or class"),
        ("ict_seconds", overrides.ict_seconds, list(cfg.flow_lines), "line"),
        ("buffer_capacity", overrides.buffer_capacity, list(cfg.buffers), "buffer"),
        ("ckd_delay_days", overrides.ckd_delay_days, list(cfg.products), "product"),
    ]
    for field, mapping, valid, kind in checks:
        for key in mapping:
            if key not in valid:
                issues.append(OverrideIssue((field, key), _unknown(kind, key, valid)))

    cal = calendar or cfg.calendar
    tz = cfg.timezone
    first = ensure_utc(month_start).astimezone(tz).date()
    last_excl = ensure_utc(month_end).astimezone(tz).date()
    moment = ensure_utc(as_of)
    codes = list(cal.shift_codes)
    seen: set[date] = set()
    for i, extra in enumerate(overrides.extra_shifts):
        loc: tuple[str | int, ...] = ("extra_shifts", i)
        day = extra.date
        if not first <= day < last_excl:
            issues.append(OverrideIssue((*loc, "date"), f"{day} is outside the forecast month"))
            continue
        if day in seen:
            issues.append(OverrideIssue((*loc, "date"), f"{day} is listed twice"))
            continue
        seen.add(day)
        requested = extra.shifts if extra.shifts is not None else codes
        if not requested:
            issues.append(OverrideIssue((*loc, "shifts"), "no shifts given"))
            continue
        bad = False
        for j, code in enumerate(requested):
            if code not in codes:
                issues.append(OverrideIssue((*loc, "shifts", j), _unknown("shift", code, codes)))
                bad = True
        if bad:
            continue
        worked = set(cal.working_shift_codes(day))
        already = [c for c in requested if c in worked]
        if already:
            issues.append(
                OverrideIssue(
                    (*loc, "shifts"),
                    f"{day}: shift(s) {', '.join(already)} are already worked by the calendar",
                )
            )
            continue
        for code in requested:
            if cal.shift(day, code).start < moment:
                issues.append(
                    OverrideIssue((*loc, "date"), f"{day} shift {code} starts before the forecast")
                )
    return issues
