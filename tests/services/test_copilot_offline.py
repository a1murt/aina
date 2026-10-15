"""Offline copilot: ~40 Russian phrasings of a plant manager and a customer are answered, not
refused; every answer starts with a verdict, shows no raw API keys or ISO dates and groups
thousands; the three prompts the UI suggests are answered by the expected tools."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

import qost_api.app  # noqa: F401  (imports the routes before the copilot: no import cycle)
from qost_api.auth import Principal
from qost_api.copilot.offline import (
    REFUSAL,
    OfflineCopilot,
    find_entities,
    qty_rx,
    target_quantities,
)
from qost_api.copilot.tools import Toolbox
from qost_api.queries.losses import bottleneck_loss_view
from twin_core.config import TwinConfig, load_config

TODAY = date(2026, 10, 16)

GUARANTEED = {
    # the three prompts of the copilot panel (web/messages ru.json copilot.ex1–ex3); ex1 names
    # a date: on the stand data «вчера» (15.10) is above the month average for every area
    "Почему 08.10 просела сварка?": (
        "why_drop",
        {"get_kpi", "get_losses", "get_downtime"},
    ),
    "Какое оборудование в зоне риска?": ("risk", {"get_alerts", "get_equipment_health"}),
    "Выполним ли план месяца и что мешает?": (
        "plan",
        {"get_forecast", "get_bottleneck", "get_losses"},
    ),
}

QUESTIONS = [
    # the 12 questions of the copilot evaluation set
    "Почему вчера просела сварка?",
    "Какое оборудование в зоне риска?",
    "Какие потери за смену и где узкое место?",
    "Выполним ли мы план в октябре?",
    "Сколько машин мы потеряли из-за простоев на этой неделе?",
    "Что больше всего мешает выпуску?",
    "Какой брак на окраске сегодня?",
    "Почему стоит конвейер 3?",
    "Что посоветуешь сделать, чтобы выполнить 5 500?",
    "Сравни OEE сварки и окраски за неделю",
    "Какое оборудование чаще всего ломается?",
    "Сколько стоит час простоя окраски?",
    # the prompts of the panel and other phrasings of the same questions
    "Почему 08.10 просела сварка?",
    "Выполним ли план месяца и что мешает?",
    "Какое оборудование больше всего простаивало?",
    "Почему мы не выполняем план 5 500?",
    "Как дела на заводе?",
    "Что сломалось сегодня?",
    "Что с роботом ABB-03?",
    "Почему упала сварка?",
    "Риск отказа робота ABB-04?",
    # more ways to ask the same things
    "Какой OEE у Окраски-1 за месяц?",
    "Сколько машин выпустили вчера?",
    "Какие оповещения открыты?",
    "Где сейчас узкое место?",
    "Какая вероятность выполнить план линии 4 800?",
    "Когда менять фильтры окрасочной камеры?",
    "Сколько простоев было у Камеры-02 на этой неделе?",
    "Какие потери на окраске с начала месяца?",
    "Покажи брак по сварке за неделю",
    "Как выполнить план месяца?",
    "Что нужно сделать, чтобы догнать план?",
    "Какой прогноз выпуска на октябрь?",
    "Почему вчера упал OEE окраски?",
    "Какое оборудование чаще всего отказывает?",
    "Какая доступность конвейера 2?",
    "Что сейчас стоит?",
    "Сколько стоит минута простоя сборки?",
    "Сравни сварку, окраску и сборку по OEE",
    "Что происходит с ОТК?",
    "Есть ли критичные оповещения?",
    "Почему ухудшилось качество окраски?",
]

RAW_KEY = re.compile(r"(?<![\w-])[a-z][a-z_]{2,}(?![\w-])")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
UNGROUPED = re.compile(r"(?<![\d,.])\d{4,}(?![\d,])")


# --------------------------------------------------------------------------- fake data


def _kpi_row(level: str, code: str, a: dict[str, Any], i: int) -> dict[str, Any]:
    period = (
        {"date": a.get("to")}
        if a.get("granularity", "day") == "day"
        else {"month": str(a.get("to", "2026-10-16"))[:7]}
    )
    if level == "equipment":
        return {
            "level": level,
            "code": code,
            **period,
            "availability": 0.99 - i * 0.003,
            "failures": 5 - i % 5,
            "adot": 200.0 - i * 9,
            "mtbf_h": 34.5 + i,
            "oee": None,
        }
    yesterday = a.get("granularity") == "day"
    return {
        "level": level,
        "code": code,
        **period,
        "oee": (0.84 if yesterday else 0.89) - i * 0.01,
        "availability": 0.965,
        "effectiveness": 0.94 if yesterday else 0.964,
        "quality_ratio": 0.955,
        "pq": 2518 - i,
        "gq": 2404 - i,
        "defect_rate": 0.045,
    }


def _kpi(cfg: TwinConfig, a: dict[str, Any]) -> dict[str, Any]:
    level = a.get("level", "line")
    table = {
        "line": list(cfg.lines),
        "area": [x for x in cfg.areas if cfg.areas[x].lines],
        "equipment": list(cfg.equipment),
        "plant": [cfg.plant.site.code],
    }[level]
    codes = [a["code"]] if a.get("code") else table
    return {
        "level": level,
        "from": a.get("from", "2026-10-01"),
        "to": a.get("to", "2026-10-16"),
        "items": [_kpi_row(level, c, a, i) for i, c in enumerate(codes)],
    }


LOSSES = {
    "from": "2026-10-01",
    "to": "2026-10-16",
    "kzt_per_car": 700000.0,
    "totals": {"minutes": 1179.0, "units": 304.0, "kzt": 212600000},
    "categories": [
        {"category": "planned_downtime", "minutes": 60.0, "units": 15.0, "loss": False},
        {"category": "quality", "minutes": 444.0, "units": 114.0, "kzt": 79800000, "loss": True},
        {"category": "speed", "minutes": 371.0, "units": 96.0, "kzt": 67200000, "loss": True},
        {
            "category": "unplanned_downtime",
            "minutes": 364.0,
            "units": 94.0,
            "kzt": 65800000,
            "loss": True,
        },
        {"category": "starved", "minutes": 1.0, "units": 0.2, "kzt": 140000, "loss": True},
        {"category": "microstops", "minutes": 0.3, "units": 0.1, "kzt": 50000, "loss": True},
    ],
}


def _item(category: str, line: str, minutes: float, units: float, kzt: float) -> dict[str, Any]:
    loss = category != "planned_downtime"
    return {
        "category": category,
        "line": line,
        "minutes": minutes,
        "units": units,
        "kzt": kzt,
        "loss": loss,
    }


# Plant tree items: each stop/speed category on both bottleneck lines (weights 0.68 + 0.32 add
# up to the category of LOSSES), quality on the output line (weight 1), plus items that the
# bottleneck view leaves out: starvation of a non-bottleneck line and planned downtime.
PLANT_ITEMS: list[dict[str, Any]] = [
    *(
        _item(c["category"], line, c["minutes"], c["units"], c["kzt"])
        for c in LOSSES["categories"]  # type: ignore[attr-defined]
        if c["loss"] and c["category"] != "quality"
        for line in ("PAINT-1", "WELD-1")
    ),
    _item("quality", "QC-1", 444.0, 114.0, 79800000),
    _item("quality", "ASSY-1", 50.0, 13.0, 9100000),
    _item("starved", "ASSY-1", 300.0, 80.0, 56000000),
    _item("planned_downtime", "PAINT-1", 60.0, 15.0, 0),
]
STOPS: list[dict[str, Any]] = [
    {
        "entity": "CONV-03",
        "line": "ASSY-1",
        "start_ts": "2026-10-16T02:05:00+00:00",
        "open": True,
        "duration_s": 840.0,
        "reason_code": "ME-CHAIN",
        "reason_name_ru": "Обрыв цепи",
    },
    {
        "entity": "BOOTH-02",
        "line": "PAINT-1",
        "start_ts": "2026-10-15T08:44:00+00:00",
        "open": False,
        "duration_s": 2400.0,
        "reason_code": "PM-FILTER",
        "reason_name_ru": "Замена фильтра",
    },
    {
        "entity": "ABB-01",
        "line": "WELD-1",
        "start_ts": None,
        "open": False,
        "duration_s": 1500.0,
        "reason_code": "EL-SENSOR",
        "reason_name_ru": None,
    },
]
PARETO = {
    "pareto": {
        "from": "2026-10-16",
        "to": "2026-10-16",
        "area": "PAINT",
        "line": None,
        "total": 4,
        "items": [
            {"defect_code": "P-DUST", "name_ru": "Включения / сор", "qty": 2, "share": 0.5},
            {"defect_code": "P-COLOR", "name_ru": "Отклонение цвета", "qty": 1, "share": 0.25},
            {"defect_code": "P-RUN", "name_ru": "Потёки", "qty": 1, "share": 0.25},
        ],
        "vital_few": ["P-DUST", "P-COLOR"],
    },
    "recent": [],
}
ALERT: dict[str, Any] = {
    "id": 1,
    "ts": "2026-10-16T02:10:00+00:00",
    "status": "open",
}
ALERTS_BY_RULE: dict[str | None, list[dict[str, Any]]] = {
    "AL-M1": [
        {
            **ALERT,
            "rule_id": "AL-M1",
            "severity": "critical",
            "entity": "ABB-04",
            "message_ru": "Робот ABB-04: вероятность отказа в ближайшие 8 ч — 84,4%",
        }
    ],
    "AL-M2": [
        {
            **ALERT,
            "rule_id": "AL-M2",
            "severity": "warning",
            "entity": "BOOTH-02",
            "message_ru": "Камера базы и лака: перепад давления достигнет предела через 0,4 ч",
        }
    ],
    None: [
        {
            **ALERT,
            "rule_id": "AL-P1",
            "severity": "critical",
            "entity": "PLANT",
            "message_ru": "Вероятность выполнить план месяца 18,4%",
        },
        {
            **ALERT,
            "rule_id": "AL-Q1",
            "severity": "warning",
            "entity": "PAINT",
            "message_ru": "Окраска: брак 5,17%, норма 2%",
        },
        {
            **ALERT,
            "rule_id": "AL-O2",
            "severity": "info",
            "entity": "WELD-1",
            "message_ru": "Сварка-1: OEE 87,38%, цель 85%",
        },
    ],
}
FORECAST = {
    "month": "2026-10",
    "as_of": "2026-10-16T02:00:00+00:00",
    "mtd": 2511,
    "targets": {"plant_target": 5500, "line_plan": 4800},
    "p_reach": {"plant_target": 0.0, "line_plan": 0.177},
    "summary": {"p10": 4759.7, "p50": 4786.1, "p90": 4803.9, "mean": 4783.1, "sd": 18.8},
    "expected_shortfall": {"plant_target": 716.9, "line_plan": 17.9},
    "required_rate": {"plant_target": 149.45, "line_plan": 114.45},
    "remaining_shifts": 20.0,
}
LEVERS = {
    "month": "2026-10",
    "as_of": "2026-10-16T02:00:00+00:00",
    "base": {
        "summary": FORECAST["summary"],
        "p_reach": FORECAST["p_reach"],
        "expected_shortfall": FORECAST["expected_shortfall"],
    },
    "levers": [
        {
            "id": "extra_shift:2026-10-17",
            "kind": "extra_shift",
            "params": {"date": "2026-10-17", "shifts": ["A"]},
            "delta_p50": 115.7,
            "delta_mean": 115.9,
            "delta_p_reach": {"plant_target": 0.0, "line_plan": 0.823},
            "effect_kzt": {"p50": 69691673.9},
            "rank": 1,
            "top": True,
        },
        {
            "id": "filter_policy",
            "kind": "filter_policy",
            "params": {"policy": "predictive_shift_change"},
            "delta_p50": 36.3,
            "delta_mean": 35.0,
            "delta_p_reach": {"plant_target": 0.0, "line_plan": 0.625},
            "effect_kzt": {"p50": 24549787.9},
            "rank": 2,
            "top": True,
        },
        {
            "id": "defect_norm:PAINT",
            "kind": "defect_norm",
            "params": {"area": "PAINT", "from": 0.0477, "to": 0.02},
            "delta_p50": 19.2,
            "delta_mean": 18.5,
            "delta_p_reach": {"plant_target": 0.0, "line_plan": 0.422},
            "effect_kzt": {"p50": 22259657.0},
            "rank": 3,
            "top": True,
        },
        {
            "id": "eliminate_failures:BOOTH-02",
            "kind": "eliminate_failures",
            "params": {"equipment": "BOOTH-02", "criticality": "A"},
            "delta_p50": 6.4,
            "delta_mean": 6.0,
            "delta_p_reach": {"plant_target": 0.0, "line_plan": 0.117},
            "effect_kzt": {"p50": 1925004.7},
            "rank": 4,
            "top": True,
        },
        {
            "id": "buffer_capacity:PBS",
            "kind": "buffer_capacity",
            "params": {"buffer": "PBS", "from": 30, "to": 40},
            "delta_p50": 0.4,
            "delta_mean": 0.6,
            "delta_p_reach": {"plant_target": 0.0, "line_plan": 0.004},
            "effect_kzt": {"p50": -11185.6},
            "rank": 5,
            "top": False,
        },
    ],
    "shifts_needed_for_target": {
        "plant_target": {
            "saturdays": {"qty": 5500, "shifts": None, "dates": [], "p_reach": None, "p_max": 0.04},
            "weekends_and_holidays": {
                "qty": 5500,
                "shifts": 7,
                "dates": [
                    {"date": "2026-10-17", "shift": "A"},
                    {"date": "2026-10-17", "shift": "B"},
                    {"date": "2026-10-18", "shift": "A"},
                ],
                "p_reach": 0.9975,
                "p_max": 1.0,
            },
        },
        "line_plan": {
            "saturdays": {
                "qty": 4800,
                "shifts": 1,
                "dates": [{"date": "2026-10-17", "shift": "A"}],
                "p_reach": 0.9995,
                "p_max": 1.0,
            }
        },
    },
}


def _health(code: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "name_ru": code,
        "state": "RUNNING",
        "since": "2026-10-16T02:00:00Z",
        "reason_code": None,
        "prediction": None,
        "limits": [],
        "signals": [],
        "reliability_30d": {"failures": 10, "mtbf_h": 34.65, "mttr_min": 18.8},
    }
    if code == "ABB-04":
        body["prediction"] = {
            "horizon_h": 8.0,
            "p_failure": 0.844,
            "health_index": 16.0,
            "factors": ["ошибки датчиков: в среднем 1,25 1/ч за 1 ч"],
        }
    if code.startswith("BOOTH"):
        body["limits"] = [
            {
                "signal": "filter_dp_pa",
                "signal_name_ru": "Перепад давления на фильтрах",
                "hours_to_limit": 0.4 if code == "BOOTH-02" else 7.4,
                "limit_at": "2026-10-16T02:23:05+00:00",
                "alert": True,
                "text_ru": f"{code}: перепад давления достигнет предела 450 Па через ≈ 0,4 ч",
            }
        ]
    return body


BOTTLENECK = {
    "from": "2026-10-01",
    "to": "2026-10-16",
    "shifts": 22,
    "shares": {
        "WELD-1": {"sole": 0.3182, "shifting": 0.0},
        "PAINT-1": {"sole": 0.6818, "shifting": 0.0},
        "ASSY-1": {"sole": 0.0, "shifting": 0.0},
        "QC-1": {"sole": 0.0, "shifting": 0.0},
    },
    "overall": "PAINT-1",
    "live": {"current": "WELD-1"},
    "chronology": [],
}


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch, cfg: TwinConfig) -> list[tuple[str, dict[str, Any]]]:
    """Fake data side of every tool; records which tool ran with which arguments."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake(name: str) -> Any:
        async def tool(self: Toolbox, a: dict[str, Any]) -> Any:
            calls.append((name, dict(a)))
            if name == "get_kpi":
                return _kpi(cfg, a)
            if name == "get_losses":
                items = PLANT_ITEMS if a.get("level") == "plant" else []
                return {**LOSSES, "items": items, "level": a.get("level"), "code": a.get("code")}
            if name == "get_downtime":
                items = [s for s in STOPS if s["open"]] if a.get("open_only") else STOPS
                return {"items": items, "count_shown": len(items), "more": False}
            if name == "get_defects":
                return PARETO
            if name == "get_alerts":
                items = ALERTS_BY_RULE.get(a.get("rule_id"), [])
                return {
                    "items": items,
                    "open_counts": {"critical": 5, "warning": 6, "info": 1},
                    "more": False,
                }
            if name == "get_forecast":
                return FORECAST
            if name == "get_levers":
                return LEVERS
            if name == "get_equipment_health":
                return _health(a["code"])
            if name == "get_bottleneck":
                return BOTTLENECK
            raise AssertionError(name)

        return tool

    for name in (
        "get_kpi",
        "get_losses",
        "get_downtime",
        "get_defects",
        "get_alerts",
        "get_forecast",
        "get_levers",
        "get_equipment_health",
        "get_bottleneck",
    ):
        monkeypatch.setattr(Toolbox, f"_{name}", fake(name))
    return calls


def toolbox(cfg: TwinConfig, role: str = "director") -> Toolbox:
    return Toolbox(
        principal=Principal(username=role, role=role, user_id=1),
        request=cast(Any, None),
        cfg=cfg,
        clock=cast(Any, None),
        session=cast(Any, None),
        forecast_service=object(),
    )


# --------------------------------------------------------------------------- tests


def test_there_are_about_forty_phrasings() -> None:
    assert len(QUESTIONS) >= 40
    assert set(GUARANTEED) <= set(QUESTIONS)


@pytest.mark.parametrize("question", QUESTIONS)
async def test_every_phrasing_is_answered_in_russian(
    question: str, cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    bot = OfflineCopilot(cfg)
    assert bot.intents(question), question
    res = await bot.answer(question, toolbox(cfg), TODAY)
    assert not res.refused, res.text
    assert res.text != REFUSAL
    assert ran, "no tool was called"
    assert len(res.calls) <= 5
    verdict = res.text.split("\n")[0]
    assert verdict, res.text
    assert not verdict.startswith("•"), res.text
    assert "Не удалось" not in res.text, res.text
    assert not RAW_KEY.findall(res.text), (RAW_KEY.findall(res.text), res.text)
    assert not ISO_DATE.search(res.text), res.text
    assert not [m for m in UNGROUPED.findall(res.text) if not m.startswith("20")], res.text
    assert "None" not in res.text
    assert "nan" not in res.text


@pytest.mark.parametrize("question", list(GUARANTEED))
async def test_the_three_suggested_prompts(
    question: str, cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    block, tools = GUARANTEED[question]
    bot = OfflineCopilot(cfg)
    assert bot.intents(question)[0] == block
    res = await bot.answer(question, toolbox(cfg), TODAY)
    assert not res.refused
    assert tools <= {name for name, _ in ran}, ran


async def test_why_did_welding_drop_compares_yesterday_with_the_month(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer("Почему вчера просела сварка?", toolbox(cfg), TODAY)
    kpi = [a for name, a in ran if name == "get_kpi"]
    assert kpi[0] == {
        "level": "area",
        "code": "WELD",
        "from": "2026-10-15",
        "to": "2026-10-15",
        "granularity": "day",
    }
    assert kpi[1]["from"] == "2026-10-01"
    assert kpi[1]["granularity"] == "month"
    assert res.text.startswith("OEE участка «Сварка» (вчера, 15.10) — 84 %: на 5 п.п.")
    assert "Сильнее всего просела составляющая «эффективность»" in res.text
    assert "Главная потеря — брак" in res.text
    assert "«Обрыв цепи»" in res.text  # the stops of the day, in words


async def test_suggested_drop_prompt_names_its_date(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer("Почему 08.10 просела сварка?", toolbox(cfg), TODAY)
    kpi = [a for name, a in ran if name == "get_kpi"]
    assert (kpi[0]["code"], kpi[0]["from"], kpi[0]["to"]) == ("WELD", "2026-10-08", "2026-10-08")
    assert res.text.startswith("OEE участка «Сварка» (08.10)")


async def test_a_stop_listed_for_the_line_and_the_unit_is_shown_once(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    filter_change = {
        "start_ts": "2026-10-13T03:44:00+00:00",
        "open": False,
        "duration_s": 2400.0,
        "reason_code": "PM-FILTER",
        "reason_name_ru": "Замена фильтра",
    }
    stops = [
        {**filter_change, "entity": "PAINT-1", "line": "PAINT-1"},
        {**filter_change, "entity": "BOOTH-02", "line": "PAINT-1"},
        {**filter_change, "entity": "BOOTH-01", "start_ts": "2026-10-13T13:07:00+00:00"},
    ]
    monkeypatch.setitem(globals(), "STOPS", stops)
    res = await OfflineCopilot(cfg).answer("Почему 13.10 просела окраска?", toolbox(cfg), TODAY)
    line = next(x for x in res.text.splitlines() if "Простои:" in x)
    assert line.count("«Замена фильтра», 40") == 2, line  # BOOTH-02 and BOOTH-01, not PAINT-1
    assert "BOOTH-02" in line
    assert "BOOTH-01" in line
    assert "Окраска-1 —" not in line


def _config_with_target(config_copy: Path, qty: int) -> TwinConfig:
    rules = config_copy / "rules.yaml"
    rules.write_text(
        rules.read_text(encoding="utf-8").replace(
            "plant_target_per_month: 5500", f"plant_target_per_month: {qty}"
        ),
        encoding="utf-8",
    )
    plant = config_copy / "plant.yaml"
    plant.write_text(
        plant.read_text(encoding="utf-8").replace(
            "level: plant_target, qty: 5500", f"level: plant_target, qty: {qty}"
        ),
        encoding="utf-8",
    )
    return load_config(config_copy, tag_map=False)


def test_target_quantities_come_from_the_config(cfg: TwinConfig, config_copy: Path) -> None:
    assert target_quantities(cfg) == {"plant_target": {5500}, "line_plan": {4800}}
    rx = qty_rx([5500, 4800])
    assert rx is not None
    for hit in ("выполним 5 500", "выполним 5500", "4 800 авто"):
        assert rx.search(hit), hit
    for miss in ("15 500", "5 5001"):
        assert not rx.search(miss), miss
    assert qty_rx([]) is None
    other = _config_with_target(config_copy, 6000)
    assert target_quantities(other)["plant_target"] == {6000}
    assert OfflineCopilot(other).intents("Будет 6 000 машин?") == ["plan"]
    assert OfflineCopilot(other).intents("Будет 5 500 машин?") != ["plan"]
    assert OfflineCopilot(cfg).intents("Будет 5 500 машин?") == ["plan"]
    assert OfflineCopilot(cfg).intents("Будет 4800 машин?") == ["plan"]


async def test_advice_target_also_comes_from_the_levers_payload(
    config_copy: Path, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    other = _config_with_target(config_copy, 6000)  # the database plan (payload) says 5 500
    res = await OfflineCopilot(other).answer(
        "Что посоветуешь сделать, чтобы выполнить 5 500?", toolbox(other), TODAY
    )
    assert res.text.startswith("Цель завода 5\u00a0500 при текущем темпе недостижима")
    assert "План линии" not in res.text


async def test_plan_answer_names_the_ceiling_and_the_bottleneck(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer(
        "Выполним ли план месяца и что мешает?", toolbox(cfg), TODAY
    )
    assert res.text.startswith("Прогноз на октябрь 2026: ≈ 4 786 авто")
    assert "цель завода 5 500 — вероятность 0 %" in res.text
    assert "потолка линии ≈ 5 191 авто" in res.text
    assert "узкое место — Окраска-1 (68 % смен)" in res.text
    # plant losses are the losses at the bottleneck (the director tile's figure), from the
    # plant tree weighted by the bottleneck shares, not the sum over all lines
    assert ("get_losses", {"from": "2026-10-01", "to": "2026-10-16", "level": "plant"}) in ran


async def test_advice_uses_the_levers(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer(
        "Что посоветуешь сделать, чтобы выполнить 5 500?", toolbox(cfg), TODAY
    )
    assert [n for n, _ in ran] == ["get_levers"]
    assert res.text.startswith("Цель завода 5 500 при текущем темпе недостижима")
    assert "нужно 7 доп. смен (17.10, 18.10) — тогда 100\u00a0%" in res.text
    assert "План линии" not in res.text  # 5 500 was asked: only the plant target
    assert "Доп. смена 17.10 (1 смена): +116 авто" in res.text
    assert "≈ 69,7 млн ₸" in res.text


async def test_advice_for_a_master_falls_back_to_the_bottleneck(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer(
        "Что посоветуешь, чтобы выполнить план?", toolbox(cfg, "master"), TODAY
    )
    assert not res.refused
    assert "get_levers" not in [n for n, _ in ran]
    assert res.text.startswith("Больше всего выпуск ограничивает линия Окраска-1")


async def test_advice_on_the_plan_names_both_targets(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer(
        "Что посоветуешь, чтобы выполнить план?", toolbox(cfg), TODAY
    )
    assert res.text.startswith(
        "План линии 4\u00a0800 при текущем темпе под угрозой: вероятность 18\u00a0%"
    )
    assert "хватит 1 доп. смены (17.10) — тогда 100\u00a0%" in res.text
    assert "Цель завода 5\u00a0500 при текущем темпе недостижима" in res.text


async def test_compare_two_areas_in_one_answer(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer(
        "Сравни OEE сварки и окраски за неделю", toolbox(cfg), TODAY
    )
    assert ran == [
        (
            "get_kpi",
            {"level": "area", "from": "2026-10-10", "to": "2026-10-16", "granularity": "month"},
        )
    ]
    assert "участка «Сварка»" in res.text
    assert "участка «Окраска»" in res.text
    assert "разница 1 п.п." in res.text


async def test_stopped_conveyor_three(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer("Почему стоит конвейер 3?", toolbox(cfg), TODAY)
    assert ran[0] == ("get_equipment_health", {"code": "CONV-03"})
    assert res.text.startswith("Конвейер-03 (финальная, CONV-03) стоит с 07:05")
    assert "«Обрыв цепи»" in res.text


async def test_units_failing_most_often(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer(
        "Какое оборудование чаще всего ломается?", toolbox(cfg), TODAY
    )
    assert ran[0][1]["level"] == "equipment"
    assert res.text.startswith("Чаще всего (с начала месяца) отказывает Робот ABB-01: 5 отказов")


async def test_off_topic_and_roles(cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]) -> None:
    bot = OfflineCopilot(cfg)
    for question in ("Расскажи анекдот", "Кто выиграл чемпионат мира?", "Напиши код на Python"):
        res = await bot.answer(question, toolbox(cfg), TODAY)
        assert res.refused, question
        assert res.text == REFUSAL
    res = await bot.answer("Выполним ли план месяца?", toolbox(cfg, "quality"), TODAY)
    assert not res.refused
    assert "недоступен" in res.text
    assert ran == []


def test_entities_by_short_names_and_numbers(cfg: TwinConfig) -> None:
    assert find_entities(cfg, "конвейер 3").equipment == ["CONV-03"]
    assert find_entities(cfg, "Конвейера №2 и камеры 1").equipment == ["CONV-02", "BOOTH-01"]
    assert find_entities(cfg, "отказ робота").areas == []
    assert find_entities(cfg, "брак на ОТК").areas == ["QC"]
    e = find_entities(cfg, "Сравни окраску со сваркой")
    assert e.ordered == [("area", "PAINT"), ("area", "WELD")]


def test_bottleneck_loss_view_matches_the_director_tile() -> None:
    """Same definition as web/src/components/director/losses.ts (bottleneckLosses)."""
    view = bottleneck_loss_view({"items": PLANT_ITEMS}, BOTTLENECK, "QC-1")
    assert view is not None
    assert [(x["line"], round(x["share"], 4)) for x in view["lines"]] == [
        ("PAINT-1", 0.6818),
        ("WELD-1", 0.3182),
    ]
    weights = {(i["category"], i["line"]): round(i["weight"], 4) for i in view["items"]}
    assert weights[("speed", "PAINT-1")] == 0.6818
    assert weights[("quality", "QC-1")] == 1.0  # quality of the output line in full
    # non-bottleneck lines and planned downtime are left out
    assert all(i["line"] in ("PAINT-1", "WELD-1", "QC-1") and i["loss"] for i in view["items"])
    cats = {c["category"]: c["units"] for c in view["categories"]}
    assert cats == {
        "quality": 114.0,
        "speed": 96.0,
        "unplanned_downtime": 94.0,
        "starved": 0.2,
        "microstops": 0.1,
    }
    assert view["totals"] == {"minutes": 1180.3, "units": 304.3, "kzt": 212990000}
    # no shares known: the overall line at weight 1; no bottleneck at all: no view
    only = bottleneck_loss_view({"items": PLANT_ITEMS}, {"overall": "WELD-1"}, "QC-1")
    assert only is not None
    assert only["lines"] == [{"line": "WELD-1", "share": 1.0}]
    assert bottleneck_loss_view({"items": PLANT_ITEMS}, {"shares": {}}, "QC-1") is None


async def test_losses_answer_is_the_tile_figure_and_names_the_weights(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]]
) -> None:
    res = await OfflineCopilot(cfg).answer("Сколько потеряли с начала месяца?", toolbox(cfg), TODAY)
    text = res.text.replace("\xa0", " ")
    assert text.startswith("Потери на узком месте (с начала месяца): ≈ 304 авто, ≈ 213 млн ₸")
    assert "по доле смен, когда линия была узким местом (Окраска-1 — 68 %, Сварка-1 — 32 %)" in (
        text
    )
    assert ("get_losses", {"from": "2026-10-01", "to": "2026-10-16", "level": "plant"}) in ran


async def test_risk_headline_gives_every_unit_its_probability_in_a_fixed_order(
    cfg: TwinConfig, ran: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stand run r1 vs r2: «…CONV-01 — 92 %, Робот ABB-01 — 71 %, Робот ABB-04.» and «…ABB-04 —
    100 %, Робот ABB-01.» — the third unit had no percentage and the order followed the alert
    list (newest first, a re-raised AL-M1 moves up). Now: by p desc, ties by code, all with p."""
    p = {"CONV-01": 0.92, "ABB-01": 0.71, "ABB-04": 0.995, "ABB-02": 0.71}
    sev = {"CONV-01": "critical", "ABB-01": "warning", "ABB-04": "critical", "ABB-02": "warning"}

    def alerts(units: list[str]) -> list[dict[str, Any]]:
        return [
            {**ALERT, "rule_id": "AL-M1", "severity": sev[u], "entity": u, "message_ru": u}
            for u in units
        ]

    order: list[str] = []

    async def get_alerts(self: Toolbox, a: dict[str, Any]) -> Any:
        items = alerts(order) if a.get("rule_id") == "AL-M1" else []
        return {"items": items, "open_counts": {}, "more": False}

    async def get_health(self: Toolbox, a: dict[str, Any]) -> Any:
        body = _health(a["code"])
        body["prediction"] = {
            "horizon_h": 8.0,
            "p_failure": p[a["code"]],
            "health_index": 40.0,
            "factors": [],
        }
        return body

    monkeypatch.setattr(Toolbox, "_get_alerts", get_alerts)
    monkeypatch.setattr(Toolbox, "_get_equipment_health", get_health)
    bot = OfflineCopilot(cfg)
    q = "Какое оборудование в зоне риска?"

    heads = set()
    for order[:] in (
        ["CONV-01", "ABB-01", "ABB-04"],
        ["ABB-04", "CONV-01", "ABB-01"],
        ["ABB-01", "ABB-04", "CONV-01", "ABB-04"],  # a unit twice (two open AL-M1)
    ):
        res = await bot.answer(q, toolbox(cfg), TODAY)
        assert len(res.calls) <= 5
        heads.add(res.text.split("\n")[0])
    assert len(heads) == 1, heads
    head = heads.pop()
    assert head.startswith("В зоне риска 3 единицы оборудования: "), head
    listed = re.findall(r"([A-Z]+-\d+)\)? — (\d+)\s%", head)
    assert listed == [("ABB-04", "100"), ("CONV-01", "92"), ("ABB-01", "71")], head

    # four units: the three with a health call (critical first, then by code) are listed with
    # their probability, the fourth is counted; equal p are ordered by code
    order[:] = ["ABB-02", "ABB-01", "CONV-01", "ABB-04"]
    res = await bot.answer(q, toolbox(cfg), TODAY)
    head = res.text.split("\n")[0]
    assert len(res.calls) <= 5
    assert head.startswith("В зоне риска 4 единицы оборудования: "), head
    assert re.findall(r"([A-Z]+-\d+)\)? — (\d+)\s%", head) == [
        ("ABB-04", "100"),
        ("CONV-01", "92"),
        ("ABB-01", "71"),
    ], head
    assert "и ещё 1 единица" in head

    order[:] = ["ABB-02", "CONV-01", "ABB-01"]  # equal p: by code
    res = await bot.answer(q, toolbox(cfg), TODAY)
    assert re.findall(r"([A-Z]+-\d+)\)? — (\d+)\s%", res.text.split("\n")[0]) == [
        ("CONV-01", "92"),
        ("ABB-01", "71"),
        ("ABB-02", "71"),
    ]
