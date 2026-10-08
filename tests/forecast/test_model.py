"""Fast Monte Carlo model (SPEC §10.2, T-FC): horizon, determinism, CRN, FR-FC-01, FR-FC-03."""

from __future__ import annotations

import dataclasses
import json
import os
import time
from datetime import date, datetime, timedelta
from itertools import pairwise

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from support import GOLDEN_JSON
from twin_core.config import TwinConfig
from twin_core.forecast.calibration import month_bounds, targets_from_config
from twin_core.forecast.horizon import Horizon, build_horizon
from twin_core.forecast.model import Paths, build_setup, flow_step, simulate
from twin_core.forecast.overrides import ExtraShift, Overrides
from twin_core.forecast.params import CalibrationParams, OpenDown, PlantState
from twin_core.forecast.runner import ForecastContext, forecast, run_paths
from twin_core.schedule import pm_due, schedule_anchor, working_day_index

SEED = 20261016
RUNS = 1000
PATH_TOL = 0.5
"""Failure-rate changes are monotone per run up to half a car: an extra failure delays later
repairs (serial), which can move downtime into another hour or shift (cumulative downtime
stays monotone; P50 and the mean are checked strictly). Paint booths are excluded from per-run
checks: filters wear with operating time, so fewer booth failures can bring a filter swap into
the month — physically right, and P50 / mean still rise."""
NO_FILTER_UNITS = ("JIG-01", "OVEN-01", "CONV-01", "CONV-02", "CONV-03", "TEST-01", "TEST-02")


def _paths(ctx: ForecastContext, ov: Overrides | None = None, runs: int = RUNS) -> Paths:
    return run_paths(ctx, ov, n_runs=runs, seed=SEED)[1]


def _p50(p: Paths) -> float:
    return float(np.percentile(p.total, 50))


# --------------------------------------------------------------------------- horizon


def test_horizon_from_demo_start(cfg: TwinConfig) -> None:
    m0, m1 = month_bounds(cfg, "2026-10")
    h = build_horizon(cfg, as_of=cfg.simulation.clock.demo_start, month_start=m0, month_end=m1)
    assert h.n_steps == 160  # 10 working days x 16 h (26.10 is a holiday)
    assert h.remaining_shifts == pytest.approx(20.0)
    assert h.month_shifts == 42
    assert h.days[0] == date(2026, 10, 16)
    assert h.days[-1] == date(2026, 10, 31)
    assert set(np.unique(h.gap_min).tolist()) == {0.0, 480.0, 3360.0, 4800.0}
    assert len(set(h.slot.tolist())) == h.n_steps  # one random-number slot per step
    anchor = schedule_anchor(cfg)
    for shift in h.shifts:
        if shift.code != "A":
            continue
        due = pm_due(
            cfg,
            shift_code="A",
            working_day=working_day_index(cfg.calendar, anchor, shift.shift_date),
        )
        got = [ev for ev in h.pm if h.shift_index[ev.step] == h.shifts.index(shift)]
        assert [(e.equipment, e.minutes) for e in got] == [
            (t.equipment, t.duration_min) for t in due
        ]


def test_horizon_partial_first_step_and_extra_shift(cfg: TwinConfig) -> None:
    m0, m1 = month_bounds(cfg, "2026-10")
    tz = cfg.timezone
    as_of = datetime(2026, 10, 16, 9, 31, tzinfo=tz)
    h = build_horizon(cfg, as_of=as_of, month_start=m0, month_end=m1)
    assert h.step_min[0] == pytest.approx(29.0)
    assert h.remaining_shifts == pytest.approx(20 - (2 * 60 + 31) / 480)
    sat = build_horizon(
        cfg,
        as_of=as_of,
        month_start=m0,
        month_end=m1,
        extra_shifts=[ExtraShift(date=date(2026, 10, 17), shifts=["A"])],
    )
    assert sat.n_steps == h.n_steps + 8
    assert [(s.shift_date, s.code) for s in sat.extra_shifts] == [(date(2026, 10, 17), "A")]

    def pm_keys(hz: Horizon) -> list[tuple[object, str, float]]:
        return [(hz.shifts[hz.shift_index[e.step]].key, e.equipment, e.minutes) for e in hz.pm]

    assert pm_keys(sat) == pm_keys(h)
    assert sat.lots == h.lots  # no maintenance or deliveries added
    assert sat.remaining_shifts == h.remaining_shifts  # the plan calendar is unchanged


# --------------------------------------------------------------------------- determinism, CRN


def test_deterministic_by_seed(ctx: ForecastContext) -> None:
    a = _paths(ctx, runs=500)
    b = _paths(ctx, runs=500)
    assert np.array_equal(a.total, b.total)
    assert np.array_equal(a.daily, b.daily)
    c = run_paths(ctx, None, n_runs=500, seed=SEED + 1)[1]
    assert not np.array_equal(a.total, c.total)
    r1, _, _ = forecast(ctx, n_runs=500, seed=SEED)
    r2, _, _ = forecast(ctx, n_runs=500, seed=SEED)
    assert json.dumps(r1.model_dump(mode="json")) == json.dumps(r2.model_dump(mode="json"))


def test_common_random_numbers_untouched_override(ctx: ForecastContext) -> None:
    base = _paths(ctx, runs=500)
    # a class C unit never limits its line: the same draws give bit-identical results
    same = _paths(ctx, Overrides(mtbf_multiplier={"WATER-01": 2.0}), runs=500)
    assert np.array_equal(base.total, same.total)


# --------------------------------------------------------------------------- FR-FC-03


def test_more_defects_never_raise_output(ctx: ForecastContext) -> None:
    low = _paths(ctx, Overrides(defect_rate={"PAINT": 0.03}))
    high = _paths(ctx, Overrides(defect_rate={"PAINT": 0.06}))
    assert _p50(high) <= _p50(low)
    assert np.all(high.total <= low.total + 1e-6)
    assert _p50(high) < _p50(low)  # and the repaint loss is visible


def test_extra_shift_never_lowers_p50(ctx: ForecastContext) -> None:
    base = _paths(ctx)
    sat = _paths(ctx, Overrides(extra_shifts=[ExtraShift(date=date(2026, 10, 17), shifts=["A"])]))
    assert _p50(sat) >= _p50(base)
    assert float((sat.total - base.total).mean()) > 50  # about a shift of the bottleneck


def test_higher_mtbf_never_lowers_output(ctx: ForecastContext) -> None:
    base = _paths(ctx)
    better = _paths(ctx, Overrides(mtbf_multiplier={"A": 1.5}))
    assert _p50(better) >= _p50(base)
    assert better.total.mean() > base.total.mean()
    no_filters = _paths(ctx, Overrides(mtbf_multiplier=dict.fromkeys(NO_FILTER_UNITS, 1.5)))
    assert np.all(no_filters.total >= base.total - PATH_TOL)


def test_larger_buffer_and_faster_cycle_never_lower_output(ctx: ForecastContext) -> None:
    base = _paths(ctx)
    bigger = _paths(ctx, Overrides(buffer_capacity={"PBS": 40}))
    faster = _paths(ctx, Overrides(ict_seconds={"PAINT-1": 225}))
    assert np.all(bigger.total >= base.total - 1e-6)
    assert np.all(faster.total >= base.total - 1e-6)


@settings(max_examples=6, deadline=None)
@given(
    area=st.sampled_from(["WELD", "PAINT", "ASSY", "QC"]),
    rates=st.tuples(st.floats(0.0, 0.3), st.floats(0.0, 0.3)).map(sorted),
    unit=st.sampled_from(["JIG-01", "ABB-02", "OVEN-01", "CONV-02", "TEST-01", "B"]),
    mults=st.tuples(st.floats(0.2, 5.0), st.floats(0.2, 5.0)).map(sorted),
)
def test_monotone_pairs(
    ctx: ForecastContext, area: str, rates: list[float], unit: str, mults: list[float]
) -> None:
    lo = _paths(ctx, Overrides(defect_rate={area: rates[0]}), runs=200)
    hi = _paths(ctx, Overrides(defect_rate={area: rates[1]}), runs=200)
    assert np.all(hi.total <= lo.total + 1e-6)
    m_lo = _paths(ctx, Overrides(mtbf_multiplier={unit: mults[0]}), runs=200)
    m_hi = _paths(ctx, Overrides(mtbf_multiplier={unit: mults[1]}), runs=200)
    assert np.all(m_hi.total >= m_lo.total - PATH_TOL)
    assert np.percentile(m_hi.total, 50) >= np.percentile(m_lo.total, 50)


# --------------------------------------------------------------------------- fluid flow


@settings(max_examples=60, deadline=None)
@given(st.data())
def test_flow_step_invariants(data: st.DataObject) -> None:
    runs, lines = 8, 4

    def arr(shape: tuple[int, ...], hi: float = 20.0) -> np.ndarray:
        n = int(np.prod(shape))
        values = data.draw(st.lists(st.floats(0.0, hi), min_size=n, max_size=n))
        return np.array(values, dtype=float).reshape(shape)

    cap = arr((runs, lines))
    capacity = np.array(data.draw(st.lists(st.floats(1.0, 30.0), min_size=3, max_size=3)))
    held = arr((runs, lines), 3.0)
    held[:, :-1] = np.minimum(held[:, :-1], capacity)  # a valid state: occupancy <= capacity
    buffers = np.minimum(arr((runs, lines - 1)), capacity - held[:, :-1])
    ret = np.minimum(held, arr((runs, lines), 2.0))
    p = arr((runs, lines), 0.3)
    pass_f, queue_f = 1.0 - p, p * 0.99
    limit = arr((runs,))
    x, after, finished = flow_step(cap, buffers, held, capacity, pass_f, queue_f, ret, limit)
    eps = 1e-9
    assert np.all(x >= -eps)
    assert np.all(x <= cap + eps)
    assert np.all(x[:, 0] <= limit + eps)
    held_after = held - ret + x * queue_f
    assert np.all(after >= -eps)
    assert np.all(after + held_after[:, :-1] <= capacity + eps)
    inflow = x * pass_f + ret
    # material balance of every buffer
    assert np.allclose(after, buffers + inflow[:, :-1] - x[:, 1:], atol=1e-9)
    assert np.allclose(finished, inflow[:, -1])


# --------------------------------------------------------------------------- golden-ish


def _degenerate(cfg: TwinConfig, params: CalibrationParams, per_shift: float) -> CalibrationParams:
    """No failures, no defects, no filters; every line makes exactly ``per_shift`` per shift."""
    big = 1e9
    equipment = {
        c: e.model_copy(
            update={"failures": e.failures.model_copy(update={"shape": 0.0, "per_h": 0.0})}
        )
        for c, e in params.equipment.items()
    }
    lines = {}
    for code, line in params.lines.items():
        m = per_shift * line.ict_seconds * line.cycle_factor_mix / (480 * 60)
        eff = line.efficiency.model_copy(update={"alpha": m * big, "beta": (1 - m) * big})
        lines[code] = line.model_copy(update={"efficiency": eff})
    areas = {
        a: d.model_copy(update={"rate": d.rate.model_copy(update={"alpha": 1e-9, "beta": 1.0})})
        for a, d in params.areas.items()
    }
    return params.model_copy(
        update={"equipment": equipment, "lines": lines, "areas": areas, "filters": None}
    )


def test_golden_naive_projection_and_required_rate(
    cfg: TwinConfig, params: CalibrationParams
) -> None:
    golden = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))["plan"]
    m0, m1 = month_bounds(cfg, "2026-10")
    state = PlantState(
        as_of=m0,
        mtd_output=0,
        buffers={c: float(b.initial) for c, b in params.buffers.items()},
        kits={p: float(k) for p, k in cfg.simulation.ckd_supply.initial_kits.items()},
    )
    flat = _degenerate(cfg, params, golden["mean_sustainable_rate_per_shift"])
    setup = build_setup(cfg, flat, state, month_start=m0, month_end=m1)
    setup = dataclasses.replace(setup, pm_steps=())  # planned maintenance off
    paths = simulate(setup, 200, SEED)
    assert np.allclose(paths.total, golden["naive_month_projection"], atol=1.0)
    ctx = ForecastContext(cfg, flat, state, targets_from_config(cfg, "2026-10"), "2026-10")
    result, _, _ = forecast(ctx, n_runs=200, seed=SEED)
    assert result.required_rate["line_plan"] == pytest.approx(
        golden["required_rate_per_shift_line_plan"], abs=1e-4
    )
    assert result.required_rate["plant_target"] == pytest.approx(
        golden["required_rate_per_shift_target"], abs=1e-4
    )


def test_plant_target_is_out_of_reach_without_extra_shifts(ctx: ForecastContext) -> None:
    result, _, _ = forecast(ctx, n_runs=RUNS, seed=SEED)
    ceiling = 480 * 60 / 233 * 42
    assert ceiling < 5500
    assert result.p_reach["plant_target"] == 0.0
    assert 0.0 <= result.p_reach["line_plan"] <= 1.0
    assert result.summary.p10 <= result.summary.p50 <= result.summary.p90
    fan = result.fan
    assert [p.date for p in fan] == [date(2026, 10, 16) + timedelta(days=i) for i in range(16)]
    assert all(a.p50 <= b.p50 for a, b in pairwise(fan))
    assert fan[-1].p50 == pytest.approx(result.summary.p50, abs=1.0)
    assert sum(result.histogram.counts) == RUNS


# --------------------------------------------------------------------------- scenarios


def test_open_repair_lowers_output(ctx: ForecastContext, state: PlantState) -> None:
    down = OpenDown(
        equipment="CONV-03", state="DOWN_UNPLANNED", reason="ME-CHAIN", since=state.as_of
    )
    stopped = dataclasses.replace(ctx, state=state.model_copy(update={"open_downs": [down]}))
    base, hit = _paths(ctx), _paths(stopped)
    assert np.all(hit.total <= base.total + PATH_TOL)
    assert 0 < float((base.total - hit.total).mean()) < 30


def test_filter_near_limit_and_predictive_policy(ctx: ForecastContext, state: PlantState) -> None:
    fresh = dataclasses.replace(
        ctx, state=state.model_copy(update={"filter_dp": {"BOOTH-01": 150.0, "BOOTH-02": 150.0}})
    )
    near = dataclasses.replace(
        ctx, state=state.model_copy(update={"filter_dp": {"BOOTH-01": 150.0, "BOOTH-02": 440.0}})
    )
    assert _paths(near).total.mean() < _paths(fresh).total.mean()
    on_limit = _paths(ctx)
    predictive = _paths(ctx, Overrides(filter_policy="predictive_shift_change"))
    assert predictive.total.mean() > on_limit.total.mean()


# --------------------------------------------------------------------------- FR-FC-01


def test_fr_fc_01_timing_5000_runs_15_working_days(
    ctx: ForecastContext, state: PlantState, cfg: TwinConfig
) -> None:
    as_of = datetime(2026, 10, 9, 7, 0, tzinfo=cfg.timezone)
    early = dataclasses.replace(ctx, state=state.model_copy(update={"as_of": as_of}))
    m0, m1 = month_bounds(cfg, "2026-10")
    h = build_horizon(cfg, as_of=as_of, month_start=m0, month_end=m1)
    assert h.n_steps == 15 * 16
    scenario = Overrides(
        defect_rate={"PAINT": 0.03}, extra_shifts=[ExtraShift(date=date(2026, 10, 17))]
    )
    forecast(early, scenario, n_runs=200, seed=SEED)  # warm-up (imports, caches)
    started = time.perf_counter()
    result, _, base = forecast(early, scenario, n_runs=5000, seed=SEED)
    elapsed = time.perf_counter() - started
    assert base is not None
    assert result.delta is not None
    budget = float(os.environ.get("FORECAST_TIMING_BUDGET_S", "1.5"))
    print(f"\nFR-FC-01: base + scenario, 5000 runs, 240 steps: {elapsed:.3f} s")
    assert elapsed <= budget
