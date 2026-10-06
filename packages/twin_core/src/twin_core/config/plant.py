"""Models for ``config/plant.yaml`` — ISA-95 plant model (SPEC §5.1).

Shape and value ranges are validated here; references between entities (equipment -> type,
buffer -> lines, plan -> line/product, ...) are checked by :mod:`twin_core.config.crossref`
so that every broken reference is reported with its exact location.
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, StringConstraints, field_validator, model_validator

from twin_core.config.common import (
    Code,
    Fraction,
    Ident,
    Named,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StrictModel,
)
from twin_core.domain import AreaKind, Criticality, PlanLevel

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def _parse_hhmm(value: object) -> object:
    if isinstance(value, time):
        return value
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        # Unquoted 07:00 is parsed by YAML 1.1 as the sexagesimal integer 420.
        raise ValueError(f'expected a quoted time "HH:MM" such as "07:00", got {value!r}')
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


ClockTime = Annotated[time, BeforeValidator(_parse_hhmm)]
Month = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]


class Site(Named):
    code: Code
    timezone: NonEmptyStr
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown IANA time zone {value!r} (is the tzdata package installed?)"
            ) from exc
        return value


class ShiftDef(Named):
    """A shift template. ``end <= start`` means the shift ends on the next calendar day."""

    code: Code
    start: ClockTime
    end: ClockTime

    @model_validator(mode="after")
    def _non_empty(self) -> ShiftDef:
        if self.start == self.end:
            raise ValueError("shift start and end must differ")
        return self

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    @property
    def duration_min(self) -> int:
        start = self.start.hour * 60 + self.start.minute
        end = self.end.hour * 60 + self.end.minute
        return (end - start) % (24 * 60)


class Holiday(StrictModel):
    date: dt.date
    name_ru: NonEmptyStr
    name_kk: NonEmptyStr | None = None


class ExtraWorkingDay(StrictModel):
    """A normally non-working day that becomes working (e.g. a Saturday shift).

    ``shifts`` omitted = all calendar shifts.
    """

    date: dt.date
    shifts: list[Code] | None = None
    name_ru: NonEmptyStr | None = None


class Calendar(StrictModel):
    shifts: Annotated[list[ShiftDef], Field(min_length=1)]
    working_weekdays: Annotated[list[Annotated[int, Field(ge=1, le=7)]], Field(min_length=1)]
    """ISO-8601 weekdays: Monday = 1 … Sunday = 7."""
    holidays: list[Holiday] = []
    extra_working_days: list[ExtraWorkingDay] = []


class Product(StrictModel):
    code: Code
    name: NonEmptyStr
    aliases: list[NonEmptyStr] = []
    cycle_factor: PositiveFloat
    color_hex: HexColor


class PlanEntry(StrictModel):
    month: Month
    level: PlanLevel
    line: Code | None = None
    product: Code | None = None
    qty: NonNegativeInt

    @model_validator(mode="after")
    def _level_shape(self) -> PlanEntry:
        if self.level == "line_model" and (self.line is None or self.product is None):
            raise ValueError("a line_model plan entry needs both 'line' and 'product'")
        if self.level == "plant_target" and (self.line is not None or self.product is not None):
            raise ValueError("a plant_target plan entry must not have 'line' or 'product'")
        return self


class Signal(StrictModel):
    """Telemetry signal of an equipment type: physical range plus warning/limit thresholds."""

    code: Ident
    name_ru: NonEmptyStr
    name_kk: NonEmptyStr | None = None
    unit: str
    lo: float
    hi: float
    warn_lo: float | None = None
    warn_hi: float | None = None
    limit_lo: float | None = None
    limit_hi: float | None = None

    @model_validator(mode="after")
    def _thresholds(self) -> Signal:
        if self.lo >= self.hi:
            raise ValueError(f"lo ({self.lo}) must be below hi ({self.hi})")
        for name in ("warn_lo", "warn_hi", "limit_lo", "limit_hi"):
            value = getattr(self, name)
            if value is not None and not self.lo <= value <= self.hi:
                raise ValueError(f"{name} ({value}) is outside the range [{self.lo}, {self.hi}]")
        if self.warn_lo is not None and self.warn_hi is not None and self.warn_lo >= self.warn_hi:
            raise ValueError("warn_lo must be below warn_hi")
        if self.warn_hi is not None and self.limit_hi is not None and self.warn_hi > self.limit_hi:
            raise ValueError("warn_hi must not exceed limit_hi")
        if self.warn_lo is not None and self.limit_lo is not None and self.warn_lo < self.limit_lo:
            raise ValueError("warn_lo must not be below limit_lo")
        return self


class EquipmentType(StrictModel):
    name_ru: NonEmptyStr
    name_kk: NonEmptyStr | None = None
    signals: list[Signal] = []


class Point(StrictModel):
    x: float
    y: float


class Rect(StrictModel):
    x: float
    y: float
    w: PositiveFloat
    h: PositiveFloat


class Rework(StrictModel):
    stations: NonNegativeInt
    minutes_median: NonNegativeFloat
    repaint_share: Fraction = 0.0
    """Share of this line's defects that need a full repaint (second pass through the line)."""


class Equipment(Named):
    code: Code
    type: Ident
    criticality: Criticality
    degraded_capacity: Fraction
    """Line capacity while this unit is down (A: 0, B: e.g. 0.5 manual bypass, C: 1)."""
    aliases: list[NonEmptyStr] = []
    layout: Point


class Line(Named):
    code: Code
    aliases: list[NonEmptyStr] = []
    ict_seconds: PositiveFloat
    plan_rate_per_shift: PositiveInt
    rework: Rework
    equipment: list[Equipment] = []


class Area(Named):
    code: Code
    kind: AreaKind
    aliases: list[NonEmptyStr] = []
    layout: Rect
    lines: list[Line] = []

    @model_validator(mode="after")
    def _production_has_lines(self) -> Area:
        if self.kind == "production" and not self.lines:
            raise ValueError("a production area needs at least one line")
        return self


class Buffer(Named):
    code: Code
    from_line: Code
    to_line: Code
    capacity: PositiveInt
    aliases: list[NonEmptyStr] = []
    layout: Rect


class Layout(StrictModel):
    viewbox: tuple[float, float, PositiveFloat, PositiveFloat]
    """SVG viewBox: min-x, min-y, width, height."""
    flow_path: Annotated[list[tuple[float, float]], Field(min_length=2)]


class PlantConfig(StrictModel):
    """Root of ``plant.yaml``."""

    version: Literal[1]
    site: Site
    calendar: Calendar
    products: Annotated[list[Product], Field(min_length=1)]
    plan: list[PlanEntry] = []
    equipment_types: dict[Ident, EquipmentType]
    areas: Annotated[list[Area], Field(min_length=1)]
    buffers: list[Buffer] = []
    layout: Layout
