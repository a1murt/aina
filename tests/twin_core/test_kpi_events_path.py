"""Event-path KPI helpers (SPEC §5.4, FR-ENG-06): time model from state and stop records,
area aggregation, stop impact."""

from __future__ import annotations

import pytest

from twin_core.kpi import (
    StateSpan,
    StopSpan,
    TimeModel,
    aggregate_kpi,
    compute_shift_kpi,
    oee_from_good,
    shift_time_model,
    stop_impact,
)

H = 3600.0


def model(states: list[StateSpan], stops: list[StopSpan], *, hi: float = 8 * H) -> TimeModel:
    return shift_time_model(
        window=(0.0, hi),
        pot_min=hi / 60.0,
        states=states,
        stops=stops,
        microstop_threshold_s=300,
    )


def test_states_and_stops_map_to_iso_22400_elements() -> None:
    states = [
        StateSpan(0, 3600, "RUNNING"),
        StateSpan(3600, 4200, "DOWN_UNPLANNED"),  # 10 min failure
        StateSpan(4200, 4320, "DOWN_UNPLANNED"),  # 2 min microstop
        StateSpan(4320, 6000, "STARVED"),  # 28 min
        StateSpan(6000, 6600, "BLOCKED"),  # 10 min
        StateSpan(6600, 7800, "DOWN_PLANNED"),  # 20 min PM
        StateSpan(7800, 8400, "CHANGEOVER"),  # 10 min
        StateSpan(8400, 9000, "DEGRADED"),
        StateSpan(9000, 8 * H, "RUNNING"),
    ]
    stops = [
        StopSpan(3600, 4200, planned=False, duration_s=600),
        StopSpan(4200, 4320, planned=False, duration_s=120),
        StopSpan(6600, 7800, planned=True, duration_s=1200),
    ]
    tm = model(states, stops)
    assert tm.pot == 480
    assert tm.pdot == pytest.approx(20)
    assert tm.adot == pytest.approx(10)
    assert tm.microstop == pytest.approx(2)
    assert tm.adet == pytest.approx(38)
    assert tm.aust == pytest.approx(10)
    assert tm.apt == pytest.approx(480 - 20 - 10 - 38 - 10)


def test_reclassification_moves_time_between_pdot_and_adot() -> None:
    states = [StateSpan(0, 600, "DOWN_UNPLANNED"), StateSpan(600, 8 * H, "RUNNING")]
    unplanned = model(states, [StopSpan(0, 600, planned=False, duration_s=600)])
    planned = model(states, [StopSpan(0, 600, planned=True, duration_s=600)])
    assert unplanned.adot == pytest.approx(10)
    assert unplanned.pdot == 0
    assert planned.adot == 0
    assert planned.pdot == pytest.approx(10)


def test_down_without_record_falls_back_to_state_and_idle_is_planned() -> None:
    states = [
        StateSpan(0, 600, "DOWN_PLANNED"),
        StateSpan(600, 1200, "DOWN_UNPLANNED"),
        StateSpan(1200, 1800, "IDLE_NO_PLAN"),
        StateSpan(1800, 8 * H, "RUNNING"),
    ]
    tm = model(states, [])
    assert tm.pdot == pytest.approx(20)
    assert tm.adot == pytest.approx(10)


def test_open_stop_counts_as_microstop_until_threshold_and_window_clips() -> None:
    states = [StateSpan(0, 3000, "RUNNING"), StateSpan(3000, 8 * H, "DOWN_UNPLANNED")]
    # live window: now = 3120 s, the stop has lasted 2 min so far
    live = shift_time_model(
        window=(0, 3120),
        pot_min=52,
        states=states,
        stops=[StopSpan(3000, 3120, planned=False, duration_s=120)],
        microstop_threshold_s=300,
    )
    assert live.microstop == pytest.approx(2)
    assert live.adot == 0
    assert live.pot == 52
    assert shift_time_model(
        window=(0, 100), pot_min=0, states=states, stops=[], microstop_threshold_s=300
    ) == TimeModel(pot=0)


def test_oee_identity_and_aggregation() -> None:
    a = compute_shift_kpi(
        TimeModel(pot=480, pdot=20, adot=10),
        pq=110,
        gq=108,
        pri_produced_s=110 * 233,
        pri_good_s=108 * 233,
        failures=1,
        repair_min=10,
    )
    b = compute_shift_kpi(
        TimeModel(pot=480, starved=30),
        pq=100,
        gq=99,
        pri_produced_s=100 * 233,
        pri_good_s=99 * 233,
        failures=0,
        repair_min=0,
    )
    assert a.oee == pytest.approx(a.availability * a.effectiveness * a.quality_ratio)  # type: ignore[operator]
    agg = aggregate_kpi([a, b])
    assert agg is not None
    assert agg.pq == 210
    assert agg.gq == 207
    assert agg.pbt_min == pytest.approx(940)
    assert agg.oee == pytest.approx(oee_from_good(207 * 233, 940))
    assert agg.failures == 1
    assert aggregate_kpi([]) is None


def test_stop_impact_fr_eng_06() -> None:
    # golden: Конвейер-03 55 min -> 14.16 cars; bottleneck stop: irrecoverable
    bn = stop_impact(
        elapsed_min=55,
        degraded_capacity=0.0,
        ict_seconds=233,
        is_bottleneck=True,
        line_capacity_per_shift=123.6,
        bottleneck_rate_per_shift=114.0,
        upstream_free_units=30,
    )
    assert bn.lost_units == pytest.approx(14.16, abs=0.01)
    assert bn.irrecoverable_units == pytest.approx(bn.lost_units)
    assert bn.recover_shifts is None
    ok = stop_impact(
        elapsed_min=55,
        degraded_capacity=0.0,
        ict_seconds=233,
        is_bottleneck=False,
        line_capacity_per_shift=123.6,
        bottleneck_rate_per_shift=114.0,
        upstream_free_units=30,
    )
    assert ok.irrecoverable_units == 0
    assert ok.recover_shifts == pytest.approx(14.163 / 9.6, abs=0.01)
    full = stop_impact(
        elapsed_min=55,
        degraded_capacity=0.0,
        ict_seconds=233,
        is_bottleneck=False,
        line_capacity_per_shift=123.6,
        bottleneck_rate_per_shift=114.0,
        upstream_free_units=4,
    )
    assert full.irrecoverable_units == pytest.approx(full.lost_units - 4)
    # class B with a manual bypass loses half; no spare capacity -> not recoverable
    robot = stop_impact(
        elapsed_min=25,
        degraded_capacity=0.5,
        ict_seconds=233,
        is_bottleneck=False,
        line_capacity_per_shift=120,
        bottleneck_rate_per_shift=125,
        upstream_free_units=None,
    )
    assert robot.lost_min == pytest.approx(12.5)
    assert robot.lost_units == pytest.approx(3.22, abs=0.01)
    assert robot.recover_shifts is None
    none = stop_impact(
        elapsed_min=0,
        degraded_capacity=0.0,
        ict_seconds=233,
        is_bottleneck=False,
        line_capacity_per_shift=120,
        bottleneck_rate_per_shift=None,
        upstream_free_units=None,
    )
    assert none.lost_units == 0
    assert none.recover_shifts == 0
