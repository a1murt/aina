"""T-LLM: shift report input assembly and the template report (SPEC §11.5): ru and kk, ≤ 250
words, the four sections, every number verified against the input."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from report_support import DAY, kpi, sample_facts, sample_forecast
from twin_core.config import TwinConfig
from twin_core.report import (
    KK_DRAFT,
    MAX_WORDS,
    SECTIONS,
    DefectFact,
    ShiftFacts,
    StopFact,
    build_shift_input,
    has_sections,
    next_working_shift,
    render_template,
    verify_report,
    word_count,
)


@pytest.fixture
def facts(cfg: TwinConfig) -> ShiftFacts:
    shift = cfg.calendar.shift(DAY, "A")
    return replace(sample_facts(cfg, shift), forecast=sample_forecast(shift.end))


def test_assembly_picks_latest_kpis_and_clips_stops(cfg: TwinConfig, facts: ShiftFacts) -> None:
    data = build_shift_input(cfg, facts)
    assert [ln.code for ln in data.lines] == ["WELD-1", "PAINT-1", "ASSY-1", "QC-1"]
    paint = next(ln for ln in data.lines if ln.code == "PAINT-1")
    assert paint.oee_pct == 79.0
    assert paint.gq == 101  # version 1, not the older 0
    assert data.totals.output == 103
    assert data.totals.plan == 120
    assert data.totals.shortfall == 17
    assert data.totals.attainment_pct == 85.8
    assert data.totals.unplanned_downtime_min == 75  # 55 + 20
    assert data.totals.planned_downtime_min == 12  # 42-min PM, 12 of them inside the shift
    assert data.totals.microstops == 1
    assert data.totals.microstop_min == 2
    assert data.totals.open_alerts == 1
    assert [s.equipment for s in data.stops] == ["CONV-03", "BOOTH-02", "ABB-04"]
    conv = data.stops[0]
    assert (conv.start_local, conv.end_local, conv.minutes) == ("09:31", "10:26", 55)
    assert conv.reason == "Обрыв цепи"
    assert conv.line_name == "Сборка-1"
    assert data.defects[0].code == "P-RUN"
    assert data.defects[0].qty == 3
    assert data.bottleneck[0].line == "PAINT-1"
    assert data.bottleneck[0].sole_pct == 58.0
    assert {(d.kind, d.entity) for d in data.deviations} == {
        ("oee", "WELD-1"),
        ("oee", "PAINT-1"),
        ("defect_rate", "PAINT"),
        ("plan", "QC-1"),
        ("forecast", "PLANT"),
    }
    assert data.forecast is not None
    assert (data.forecast.p50, data.forecast.p_plan_pct, data.forecast.plan) == (4787, 22.0, 4800)
    assert data.next_shift is not None
    assert data.next_shift.code == "B"
    assert data.source == "events"
    assert data.closed


def test_losses_partition_the_line_time(cfg: TwinConfig, facts: ShiftFacts) -> None:
    data = build_shift_input(cfg, facts)
    weld = [x for x in data.losses if x.line == "WELD-1"]
    names = {x.category: x.minutes for x in weld}
    assert names["unplanned_downtime"] == 40
    assert names["planned_downtime"] == 10
    assert all(x.cars >= 0 for x in weld)
    assert len(weld) <= 4


@pytest.mark.parametrize("lang", ["ru", "kk"])
def test_template_report_is_short_complete_and_verified(
    cfg: TwinConfig, facts: ShiftFacts, lang: str
) -> None:
    data = build_shift_input(cfg, facts, lang=lang)  # type: ignore[arg-type]
    text = render_template(data)
    assert word_count(text) <= MAX_WORDS
    assert has_sections(text, data.lang)
    for head in SECTIONS[data.lang]:
        assert head in text
    check = verify_report(text, data)
    assert check.ok, check.mismatches
    assert check.checked > 20
    assert "15.10.2026" in text
    assert "07:00–15:00" in text


def test_ru_template_wording(cfg: TwinConfig, facts: ShiftFacts) -> None:
    text = render_template(build_shift_input(cfg, facts))
    assert "Выпуск 103 авто при плане 120 (85,8%)." in text
    assert "Конвейер-03 (финальная), Сборка-1: обрыв цепи, 55 мин (09:31–10:26)." in text
    assert "Прогноз месяца: 4 787 авто (P10–P90: 4 757–4 807)" in text
    assert "Задачи на следующую смену (смена B, 15:00)" in text
    assert "- Выпуск ниже плана на 17 авто." in text


def test_kazakh_is_a_marked_draft(cfg: TwinConfig, facts: ShiftFacts) -> None:
    assert KK_DRAFT is True
    text = render_template(build_shift_input(cfg, facts, lang="kk"))
    assert text.startswith("Ауысым есебі:")
    assert "Келесі ауысымға міндеттер (B ауысымы, 15:00)" in text


def test_word_limit_holds_for_a_bad_shift(cfg: TwinConfig, facts: ShiftFacts) -> None:
    shift = facts.shift
    many_stops = [
        StopFact(
            eq,
            cfg.line_of_equipment(eq).code,
            shift.start + timedelta(minutes=10 * n),
            shift.start + timedelta(minutes=10 * n + 7),
            420.0,
            False,
            False,
            "EL-SENSOR",
        )
        for n, eq in enumerate(cfg.equipment)
    ]
    defects = [DefectFact(d.area, d.code, 4) for d in cfg.defects.values() if d.area in cfg.areas]
    bad = replace(
        facts,
        kpis=[kpi(line, 0.5, 100, 80) for line in cfg.flow_lines],
        stops=many_stops,
        defects=defects,
        alerts=list(facts.alerts) * 6,
    )
    for lang in ("ru", "kk"):
        data = build_shift_input(cfg, bad, lang=lang)
        text = render_template(data)
        assert word_count(text) <= MAX_WORDS, word_count(text)
        assert has_sections(text, data.lang)
        assert verify_report(text, data).ok


def test_without_kpis_the_template_says_so(cfg: TwinConfig, facts: ShiftFacts) -> None:
    empty = ShiftFacts(shift=facts.shift, now=facts.now)
    data = build_shift_input(cfg, empty)
    text = render_template(data)
    assert "Данных KPI за смену нет." in text
    assert data.source == "none"
    assert not data.closed
    assert has_sections(text, "ru")
    assert verify_report(text, data).ok


def test_import_rows_are_used_when_events_are_missing(cfg: TwinConfig, facts: ShiftFacts) -> None:
    imported = replace(facts, kpis=[kpi(line, 0.8, 100, 98, source="import") for line in cfg.lines])
    assert build_shift_input(cfg, imported).source == "import"


def test_next_working_shift_skips_the_weekend(cfg: TwinConfig) -> None:
    friday_b = cfg.calendar.shift(date(2026, 10, 16), "B")
    nxt = next_working_shift(cfg, friday_b)
    assert nxt is not None
    assert (nxt.shift_date, nxt.code) == (date(2026, 10, 19), "A")


def test_has_sections_requires_order(cfg: TwinConfig) -> None:
    ru = SECTIONS["ru"]
    assert has_sections("\n".join(ru), "ru")
    assert has_sections("\n".join(f"**{h}:**" for h in ru), "ru")
    assert not has_sections("\n".join(reversed(ru)), "ru")
    assert not has_sections("\n".join(ru[:3]), "ru")
