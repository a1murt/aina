"""Shift calendar (SPEC §5.2, assumption D8: October 2026 = 21 working days / 42 shifts)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from twin_core.calendar import PlantCalendar
from twin_core.config import Calendar, TwinConfig

TZ = "Asia/Qostanay"


@pytest.fixture(scope="module")
def cal(cfg: TwinConfig) -> PlantCalendar:
    return cfg.calendar


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Plant-local wall time (UTC+5) as an aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=5)


def test_october_2026_d8(cal: PlantCalendar) -> None:
    days = cal.working_days_in_month(2026, 10)
    assert len(days) == 21
    assert len(cal.working_shifts_in_month(2026, 10)) == 42
    assert date(2026, 10, 26) not in days
    assert cal.is_holiday(date(2026, 10, 26))
    assert not cal.is_working_day(date(2026, 10, 26))  # Monday, transferred day off
    assert not cal.is_working_day(date(2026, 10, 17))  # Saturday
    assert cal.is_working_day(date(2026, 10, 16))  # Demo Day, Friday


def test_shift_boundaries_in_utc(cal: PlantCalendar) -> None:
    a, b = cal.shifts_on(date(2026, 10, 16))
    assert (a.code, b.code) == ("A", "B")
    assert a.start == datetime(2026, 10, 16, 2, 0, tzinfo=UTC)  # 07:00 +05
    assert a.end == b.start == datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
    assert b.end == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert a.duration == timedelta(hours=8)
    assert a.key == (date(2026, 10, 16), "A")
    assert cal.shift(date(2026, 10, 16), "B") == b
    with pytest.raises(KeyError):
        cal.shift(date(2026, 10, 16), "C")


def test_shift_at(cal: PlantCalendar) -> None:
    shift = cal.shift_at(datetime(2026, 10, 16, 4, 31, 12, tzinfo=UTC))  # 09:31 local
    assert shift is not None
    assert (shift.shift_date, shift.code, shift.working) == (date(2026, 10, 16), "A", True)
    at_change = cal.shift_at(local(2026, 10, 16, 15, 0))
    assert at_change is not None
    assert at_change.code == "B"  # half-open intervals
    assert cal.shift_at(local(2026, 10, 16, 23, 30)) is None  # night: no shift
    saturday = cal.shift_at(local(2026, 10, 17, 9, 0))
    assert saturday is not None
    assert not saturday.working
    assert cal.shift_at(local(2026, 10, 17, 9, 0), working_only=True) is None


def test_next_shift_change(cal: PlantCalendar) -> None:
    assert cal.next_shift_change(local(2026, 10, 16, 9, 31)) == local(2026, 10, 16, 15, 0)
    assert cal.next_shift_change(local(2026, 10, 16, 15, 0)) == local(2026, 10, 16, 23, 0)
    # Friday night -> weekend -> holiday Monday 26.10 -> Tuesday 07:00.
    assert cal.next_shift_change(local(2026, 10, 23, 23, 30)) == local(2026, 10, 27, 7, 0)


def test_shifts_between(cal: PlantCalendar) -> None:
    shifts = cal.shifts_between(local(2026, 10, 16, 14, 0), local(2026, 10, 19, 8, 0))
    assert [(s.shift_date.day, s.code) for s in shifts] == [(16, "A"), (16, "B"), (19, "A")]
    every = cal.shifts_between(
        local(2026, 10, 16, 14, 0), local(2026, 10, 19, 8, 0), working_only=False
    )
    assert len(every) == 7
    assert cal.shifts_between(local(2026, 10, 16, 8, 0), local(2026, 10, 16, 8, 0)) == []


def test_materialize_covers_every_day(cal: PlantCalendar) -> None:
    rows = cal.materialize(date(2026, 9, 1), date(2026, 12, 31))
    assert len(rows) == 122 * 2
    assert len({r.key for r in rows}) == len(rows)
    assert sum(r.working for r in rows if r.shift_date.month == 10) == 42


def test_extra_working_day_with_selected_shifts(cfg: TwinConfig) -> None:
    data = cfg.plant.calendar.model_dump()
    data["extra_working_days"] = [{"date": date(2026, 10, 17), "shifts": ["A"]}]
    cal = PlantCalendar(Calendar.model_validate(data), TZ)
    assert cal.is_working_day(date(2026, 10, 17))
    assert cal.working_shift_codes(date(2026, 10, 17)) == ("A",)
    assert len(cal.working_days_in_month(2026, 10)) == 22
    assert len(cal.working_shifts_in_month(2026, 10)) == 43


def test_night_shift_crossing_midnight() -> None:
    calendar = Calendar.model_validate(
        {
            "shifts": [
                {"code": "A", "name_ru": "день", "start": "07:00", "end": "19:00"},
                {"code": "N", "name_ru": "ночь", "start": "19:00", "end": "07:00"},
            ],
            "working_weekdays": [1, 2, 3, 4, 5],
        }
    )
    cal = PlantCalendar(calendar, TZ)
    night = cal.shift_at(local(2026, 10, 17, 3, 0))  # Saturday 03:00 belongs to Friday's night
    assert night is not None
    assert (night.shift_date, night.code, night.working) == (date(2026, 10, 16), "N", True)
    assert night.duration == timedelta(hours=12)


@given(st.integers(min_value=0, max_value=122 * 24 * 3600))
def test_shift_at_is_consistent(cfg: TwinConfig, offset_s: int) -> None:
    cal = cfg.calendar
    instant = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=offset_s)
    shift = cal.shift_at(instant)
    if shift is None:
        assert not any(
            s.contains(instant)
            for s in cal.shifts_between(
                instant - timedelta(days=1), instant + timedelta(days=1), working_only=False
            )
        )
        return
    assert shift.contains(instant)
    assert shift.working == (shift.code in cal.working_shift_codes(shift.shift_date))
    assert shift.working == cal.is_working_day(shift.shift_date)
