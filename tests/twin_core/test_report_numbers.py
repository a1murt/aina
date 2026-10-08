"""T-LLM: number check of report texts (SPEC §11.5) — extraction of Russian/Kazakh numbers,
tolerances, percent normalisation, dates and times, codes that are not numbers."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from twin_core.report.numbers import extract, input_values, verify

INPUT: dict[str, Any] = {
    "shift": {"date": "2026-10-15", "code": "A", "start_local": "07:00", "end_local": "15:00"},
    "output": 114,
    "plan": 120,
    "oee": 0.81234,
    "oee_pct": 79.4,
    "defect_rate_pct": 5.17,
    "lost_cars": 14.25,
    "target": 5500,
    "p10": 4757,
    "p90": 4807,
    "gap_pp": 1.5,
    "stop": {"start_local": "09:31", "minutes": 55},
    "alert": "Конвейер-03 (финальная): потеря ≈ 14,2 авто; отыгрывается за ≈ 0,5 смены",
}
NAMES = ["Конвейер-03 (финальная)", "Сборка-1", "Окраска-1", "CONV-03"]


def check(text: str) -> list[str]:
    return [m.token for m in verify(text, INPUT, mask=NAMES).mismatches]


def test_correct_text_passes() -> None:
    text = (
        "Итоги\nВыпуск 114 авто при плане 120. OEE Окраска-1 79,4%, брак 5,17%.\n"
        "Причины\n- Конвейер-03 (финальная), Сборка-1: обрыв цепи, 55 мин с 09:31.\n"
    )
    result = verify(text, INPUT, mask=NAMES)
    assert result.ok, result.mismatches
    assert result.checked == 6  # 114, 120, 79,4%, 5,17%, 55 and 09:31; nothing from the names


def test_wrong_number_is_reported() -> None:
    assert check("Выпуск 115 авто при плане 120.") == ["115"]
    assert check("Брак 5,3%.") == ["5,3%"]


def test_rounding_tolerance_for_decimals_only() -> None:
    assert check("потеря ≈ 14,2 авто") == []  # 14.25 -> 14.2 (|Δ| = 0.05)
    assert check("потеря ≈ 14,3 авто") == []  # 14.25 -> 14.3
    assert check("потеря ≈ 14,4 авто") == ["14,4"]
    assert check("OEE 79,45%") == []
    assert check("OEE 79,5%") == ["79,5%"]  # 0.1 off
    assert check("около 14 авто") == ["14"]  # integers are exact: 14.25 is not 14


def test_percent_normalisation_both_ways() -> None:
    assert check("OEE 81,2%") == []  # input fraction 0.81234 -> 81.234 %
    assert check("OEE 81,23 %") == []
    assert check("OEE 81,4%") == ["81,4%"]
    assert check("разрыв 1,5 п.п.") == []
    assert check("разрыв 1,5 п. п.") == []
    assert check("доля 0,8123") == []  # the fraction itself, within 0.05
    assert check("OEE 79,4") == []  # a percent value quoted without the sign


def test_thousands_separators_and_ranges() -> None:
    assert check("цель 5 500 авто") == []
    assert check("цель 5 500 авто") == []
    assert check("цель 5 500 авто") == []
    assert check("цель 5500 авто") == []
    assert check("P10–P90: 4 757–4 807") == []
    assert check("P10–P90: 4 757–4 808") == ["4 808"]
    assert check("потеря −14,2 авто") == []  # sign is ignored


def test_dates_and_times_must_come_from_the_input() -> None:
    assert check("Рапорт за 15.10.2026, смена A (07:00–15:00)") == []
    assert check("Рапорт за 15.10") == []
    assert check("Рапорт за 2026-10-15") == []
    assert check("на 17.10.2026") == ["17.10.2026"]
    assert check("остановка в 11:11") == ["11:11"]
    assert check("с 9:31") == []
    assert check("октябрь 2026") == []  # a year of an input date


def test_short_date_is_a_number_unless_the_day_is_known() -> None:
    found = extract("OEE 16.10 и 81.20", known_days={(15, 10)})
    assert [t.value for t in found.numbers] == [16.1, 81.2]
    found = extract("на 15.10 и 16.10", known_days={(15, 10)})
    assert [d[0] for d in found.dates] == ["15.10"]


def test_codes_ordinals_lists_and_standards_are_not_numbers() -> None:
    text = (
        "1) AL-S1 по CONV-03 и Сборка-1, модель J7, P50 и 1-ауысым, 2-я смена.\n"
        "2. OEE по ISO 22400, уровни ISA-95.\n"
        "- 3) пункт"
    )
    assert extract(text).numbers == ()
    assert check(text) == []


def test_names_with_digits_are_masked() -> None:
    # without the mask «Сборка-1 114» would not be a problem either: "1" follows a letter-hyphen
    assert check("Сборка-1 114 авто") == []
    assert check("Конвейер-03 (финальная) 55 мин") == []


def test_numbers_inside_input_strings_count() -> None:
    values = input_values(INPUT, mask=NAMES)
    assert 14.2 in values.numbers
    assert 0.5 in values.numbers
    assert "09:31" in values.times
    assert "07:00" in values.times
    assert check("отыгрывается за ≈ 0,5 смены") == []


def test_iso_timestamps_give_dates_but_not_utc_times() -> None:
    values = input_values({"start": "2026-10-15T02:00:00Z"})
    assert {d.isoformat() for d in values.dates} == {"2026-10-15"}
    assert values.times == frozenset()


@pytest.mark.parametrize("value", [0.0, 3.0, 99.9, 1234.5])
def test_formatted_input_number_passes(value: float) -> None:
    text = f"значение {value:.1f}".replace(".", ",")
    assert verify(text, {"v": value}).ok


@given(st.lists(st.floats(min_value=0, max_value=10_000, allow_nan=False), max_size=6))
def test_any_input_number_quoted_with_one_decimal_passes(values: list[float]) -> None:
    rounded = [round(v, 1) for v in values]
    text = "; ".join(f"{v:.1f}".replace(".", ",") for v in rounded)
    assert verify(text, {"values": rounded}).ok
