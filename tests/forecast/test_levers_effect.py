"""Levers (SPEC §10.4), extra shifts needed for a target, economic effect (§10.5), AL-P1."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from twin_core.config import TwinConfig
from twin_core.forecast.calibration import targets_from_config
from twin_core.forecast.effect import (
    EffectResult,
    economics,
    effect_per_run,
    improvement_overrides,
    paint_area,
)
from twin_core.forecast.levers import LeversResult, lever_specs
from twin_core.forecast.model import Paths
from twin_core.forecast.overrides import Overrides
from twin_core.forecast.params import CalibrationParams
from twin_core.forecast.runner import ForecastContext, effect, forecast, levers
from twin_core.rules import AL_PLAN_RISK, AlertEvaluator

SEED = 20261016


@pytest.fixture(scope="module")
def lever_result(ctx: ForecastContext) -> LeversResult:
    return levers(ctx, n_runs=100, seed=SEED)


def test_lever_specs(cfg: TwinConfig, ctx: ForecastContext) -> None:
    _, m1 = ctx.bounds
    specs = lever_specs(cfg, ctx.params, as_of=ctx.state.as_of, month_end=m1)
    ids = [s.id for s in specs]
    a_b = [c for c, e in cfg.equipment.items() if e.criticality in ("A", "B")]
    assert ids[: len(a_b)] == [f"eliminate_failures:{c}" for c in a_b]
    assert "eliminate_failures:WATER-01" not in ids  # class C does not limit the line
    assert "defect_norm:PAINT" in ids
    assert "defect_norm:ASSY" not in ids
    assert {"filter_policy", "extra_shift:2026-10-17", "buffer_capacity:PBS"} <= set(ids)
    by_id = {s.id: s for s in specs}
    limit = cfg.rules.thresholds.defect_rate_limit
    assert by_id["defect_norm:PAINT"].overrides.defect_rate == {"PAINT": limit}
    assert by_id["buffer_capacity:PBS"].params == {"buffer": "PBS", "from": 30, "to": 40}
    mult = cfg.simulation.forecast.levers.failure_free_multiplier
    assert by_id["eliminate_failures:CONV-03"].overrides.mtbf_multiplier == {"CONV-03": mult}


def test_levers_ranked_and_applicable(cfg: TwinConfig, lever_result: LeversResult) -> None:
    rows = lever_result.levers
    effects = [r.effect_kzt.mean for r in rows]
    assert effects == sorted(effects, reverse=True)
    assert [r.rank for r in rows] == list(range(1, len(rows) + 1))
    assert sum(r.top for r in rows) == cfg.simulation.forecast.levers.top
    # «Применить в сценарии»: the lever's overrides are a valid FR-FC-02 payload
    for row in rows:
        Overrides.model_validate(row.overrides)
    shift = next(r for r in rows if r.kind == "extra_shift")
    assert shift.delta_p50 > 60
    assert shift.delta_p_reach["plant_target"] == 0.0
    assert shift.rank == 1


def test_shifts_needed_for_target(lever_result: LeversResult) -> None:
    needed = lever_result.shifts_needed_for_target
    plant = needed["plant_target"]
    assert plant["saturdays"].shifts is None  # 6 Saturday shifts are not enough for 5 500
    assert plant["saturdays"].p_max <= 0.5
    assert plant["saturdays"].candidates == 6
    wide = plant["weekends_and_holidays"]
    assert wide.shifts is not None
    assert 4 <= wide.shifts <= 10
    assert wide.p_reach is not None
    assert wide.p_reach > 0.5
    assert len(wide.dates) == wide.shifts
    assert wide.dates[0] == {"date": "2026-10-17", "shift": "A"}
    line = needed["line_plan"]["saturdays"]
    assert line.shifts is not None
    assert line.shifts <= 2


def test_effect_formula_on_a_hand_case(cfg: TwinConfig) -> None:
    econ, _ = economics(cfg)
    base = Paths(
        total=np.array([100.0, 100.0]),
        daily=np.zeros((1, 2)),
        rework=np.array([[10.0, 20.0], [10.0, 20.0]]),
    )
    scenario = Paths(
        total=np.array([110.0, 95.0]),
        daily=np.zeros((1, 2)),
        rework=np.array([[10.0, 15.0], [12.0, 20.0]]),
    )
    value = effect_per_run(base, scenario, areas=("WELD", "PAINT"), econ=econ, extra_shifts=1)
    margin = econ.price_kzt * econ.margin_rate
    cost = econ.rework_cost_kzt
    expected = [
        10 * margin + 5 * cost["PAINT"] - econ.shift_cost_kzt,
        -5 * margin - 2 * cost["WELD"] - econ.shift_cost_kzt,
    ]
    assert value.tolist() == pytest.approx(expected)


def test_economics_assumptions(cfg: TwinConfig) -> None:
    econ, listed = economics(cfg, {"margin_rate": 0.12, "rework_cost_kzt": {"PAINT": 1.0}})
    assert econ.margin_rate == 0.12
    assert econ.rework_cost_kzt["PAINT"] == 1.0
    assert econ.rework_cost_kzt["WELD"] == cfg.business.params.rework_cost_kzt.value["WELD"]
    flags = {a.key: (a.assumption, a.overridden) for a in listed}
    assert flags["margin_rate"] == (True, True)
    assert flags["avg_price_kzt"] == (False, False)
    with pytest.raises(ValueError, match="unknown assumption"):
        economics(cfg, {"revenue": 1})


def test_improvement_overrides(cfg: TwinConfig, params: CalibrationParams) -> None:
    ov = improvement_overrides(cfg, params)
    imp = cfg.business.improvement_defaults
    assert paint_area(cfg) == "PAINT"
    assert ov.mttr_multiplier == dict.fromkeys(
        ("A", "B", "C"), pytest.approx(1 - imp.mttr_reduction)
    )
    assert ov.mtbf_multiplier["A"] == pytest.approx(1 / (1 - imp.unplanned_failure_reduction))
    assert ov.filter_policy == imp.filter_policy
    assert ov.defect_rate == {"PAINT": imp.paint_defect_rate_target}


def test_effect_with_the_system_full_month(cfg: TwinConfig, params: CalibrationParams) -> None:
    out = effect(
        cfg,
        params,
        month="2026-10",
        targets=targets_from_config(cfg, "2026-10"),
        scenario=None,
        assumptions=None,
        n_runs=300,
        seed=SEED,
    )
    assert out.horizon == "full_month"
    assert out.currency == "KZT"
    assert out.delta_cars.mean > 0
    assert out.month_kzt.mean > 0
    assert out.year_kzt.mean == pytest.approx(out.month_kzt.mean * 12, rel=1e-6)
    assert out.month_kzt.p10 <= out.month_kzt.p50 <= out.month_kzt.p90
    assert out.rework_saved["PAINT"].units > 0
    assert out.show_revenue is False
    assert [n for n in EffectResult.model_fields if "revenue" in n] == ["show_revenue"]
    assert {a.key for a in out.assumptions} == {
        "avg_price_kzt",
        "margin_rate",
        "rework_cost_kzt",
        "saturday_shift_cost_kzt",
    }
    sat = effect(
        cfg,
        params,
        month="2026-10",
        targets=targets_from_config(cfg, "2026-10"),
        scenario=Overrides.model_validate({"extra_shifts": [{"date": "2026-10-17"}]}),
        assumptions=None,
        n_runs=300,
        seed=SEED,
    )
    assert sat.extra_shifts == 2
    assert sat.extra_shift_cost_kzt == 2 * cfg.business.params.saturday_shift_cost_kzt.value


def test_al_p1_plan_risk_from_forecast(cfg: TwinConfig, ctx: ForecastContext) -> None:
    result, _, _ = forecast(ctx, n_runs=500, seed=SEED)
    data = result.model_dump(mode="json")
    t = cfg.rules.thresholds
    assert t.plan_risk_target == "line_plan"
    ev = AlertEvaluator(cfg.rules)
    alert = ev.plan_risk(period_date=date(2026, 10, 16), forecast=data)
    p = data["p_reach"]["line_plan"]
    if p >= t.plan_risk_warn_p:
        assert alert is None
    else:
        assert alert is not None
        assert alert.rule_id == AL_PLAN_RISK
        assert alert.entity == "PLANT"
        assert isinstance(alert.value, dict)
        assert alert.value["target_qty"] == 4800
    # the plant target would keep the alert critical (P(5 500) = 0)
    strict = AlertEvaluator(cfg.rules, t.model_copy(update={"plan_risk_target": "plant_target"}))
    crit = strict.plan_risk(period_date=date(2026, 10, 16), forecast=data)
    assert crit is not None
    assert crit.severity == "critical"
    for p_value, expected in ((0.1, "critical"), (0.3, "warning"), (0.6, None)):
        fake = {"p_reach": {"line_plan": p_value}, "targets": {"line_plan": 4800}}
        got = ev.plan_risk(period_date=date(2026, 10, 16), forecast=fake)
        assert (got.severity if got else None) == expected
    assert ev.plan_risk(period_date=date(2026, 10, 16), forecast={}) is None
