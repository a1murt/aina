"""Plant shift calendar (SPEC §5.2): shifts, working days, holidays, extra working days.

Every rule comes from ``plant.yaml: calendar`` and ``site.timezone``; nothing is hard-coded.
Shift instances carry UTC boundaries; their ``shift_date`` is the plant-local date on which the
shift starts (a shift crossing midnight belongs to the day it started).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from twin_core.clock import ensure_utc
from twin_core.config.plant import Calendar, PlantConfig, ShiftDef

_ONE_DAY = timedelta(days=1)
_SEARCH_DAYS = 400
"""Upper bound when searching forward for the next working shift (> a year of holidays)."""


@dataclass(frozen=True, order=True)
class ShiftInstance:
    """One concrete shift: e.g. shift A of 2026-10-16, 02:00–10:00 UTC."""

    start: datetime
    end: datetime
    shift_date: date
    code: str
    working: bool

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def key(self) -> tuple[date, str]:
        """Primary key of the ``shift`` table: (shift_date, shift_code)."""
        return self.shift_date, self.code

    def contains(self, instant: datetime) -> bool:
        return self.start <= ensure_utc(instant) < self.end


class PlantCalendar:
    """Shift calendar of the plant in its local time zone."""

    def __init__(self, calendar: Calendar, tz: ZoneInfo | str) -> None:
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self._calendar = calendar
        self._shifts: tuple[ShiftDef, ...] = tuple(sorted(calendar.shifts, key=lambda s: s.start))
        self._shift_codes = tuple(s.code for s in self._shifts)
        self._weekdays = frozenset(calendar.working_weekdays)
        self._holidays = {h.date: h for h in calendar.holidays}
        self._extra = {
            d.date: tuple(d.shifts) if d.shifts else self._shift_codes
            for d in calendar.extra_working_days
        }

    @classmethod
    def from_plant(cls, plant: PlantConfig) -> PlantCalendar:
        return cls(plant.calendar, plant.site.timezone)

    # ------------------------------------------------------------------ days

    @property
    def shift_codes(self) -> tuple[str, ...]:
        """Shift codes ordered by start time."""
        return self._shift_codes

    def local_date(self, instant: datetime) -> date:
        return ensure_utc(instant).astimezone(self.tz).date()

    def is_holiday(self, day: date) -> bool:
        return day in self._holidays

    def is_working_day(self, day: date) -> bool:
        if day in self._extra:
            return True
        return day.isoweekday() in self._weekdays and day not in self._holidays

    def working_shift_codes(self, day: date) -> tuple[str, ...]:
        """Shifts worked on ``day`` (an extra working day may list only some shifts)."""
        if day in self._extra:
            return self._extra[day]
        return self._shift_codes if self.is_working_day(day) else ()

    def working_days_in_month(self, year: int, month: int) -> list[date]:
        day = date(year, month, 1)
        days: list[date] = []
        while day.month == month:
            if self.is_working_day(day):
                days.append(day)
            day += _ONE_DAY
        return days

    # ------------------------------------------------------------------ shifts

    def _instance(
        self, day: date, shift: ShiftDef, working_codes: tuple[str, ...]
    ) -> ShiftInstance:
        start_local = datetime.combine(day, shift.start, tzinfo=self.tz)
        end_day = day + _ONE_DAY if shift.crosses_midnight else day
        end_local = datetime.combine(end_day, shift.end, tzinfo=self.tz)
        return ShiftInstance(
            start=start_local.astimezone(UTC),
            end=end_local.astimezone(UTC),
            shift_date=day,
            code=shift.code,
            working=shift.code in working_codes,
        )

    def shifts_on(self, day: date, *, working_only: bool = False) -> list[ShiftInstance]:
        """Shifts starting on plant-local ``day``, ordered by start."""
        codes = self.working_shift_codes(day)
        instances = [self._instance(day, shift, codes) for shift in self._shifts]
        return [s for s in instances if s.working] if working_only else instances

    def shift(self, day: date, code: str) -> ShiftInstance:
        for shift in self._shifts:
            if shift.code == code:
                return self._instance(day, shift, self.working_shift_codes(day))
        raise KeyError(f"unknown shift code {code!r} (known: {', '.join(self._shift_codes)})")

    def shift_at(self, instant: datetime, *, working_only: bool = False) -> ShiftInstance | None:
        """The shift containing ``instant``, or None between shifts (e.g. at night)."""
        moment = ensure_utc(instant)
        local_day = self.local_date(moment)
        for day in (local_day - _ONE_DAY, local_day):
            for shift in self.shifts_on(day, working_only=working_only):
                if shift.contains(moment):
                    return shift
        return None

    def materialize(self, first: date, last: date) -> list[ShiftInstance]:
        """Every shift (working or not) for plant-local dates ``first..last`` inclusive."""
        out: list[ShiftInstance] = []
        day = first
        while day <= last:
            out.extend(self.shifts_on(day))
            day += _ONE_DAY
        return out

    def shifts_between(
        self, start: datetime, end: datetime, *, working_only: bool = True
    ) -> list[ShiftInstance]:
        """Shifts overlapping the half-open interval ``[start, end)``."""
        lo, hi = ensure_utc(start), ensure_utc(end)
        if hi <= lo:
            return []
        candidates = self.materialize(self.local_date(lo) - _ONE_DAY, self.local_date(hi))
        return [
            s for s in candidates if s.start < hi and s.end > lo and (s.working or not working_only)
        ]

    def working_shifts_in_month(self, year: int, month: int) -> list[ShiftInstance]:
        first = date(year, month, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - _ONE_DAY
        return [s for s in self.materialize(first, last) if s.working]

    def next_shift_change(self, instant: datetime) -> datetime | None:
        """Earliest working-shift boundary (start or end) strictly after ``instant``."""
        moment = ensure_utc(instant)
        day = self.local_date(moment) - _ONE_DAY
        for _ in range(_SEARCH_DAYS):
            for shift in self.shifts_on(day, working_only=True):
                for boundary in (shift.start, shift.end):
                    if boundary > moment:
                        return boundary
            day += _ONE_DAY
        return None

    def local_time(self, instant: datetime) -> time:
        return ensure_utc(instant).astimezone(self.tz).time()
