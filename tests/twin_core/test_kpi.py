"""T-KPI: ISO 22400 formulas (SPEC §5.4-§5.8, FR-KPI-01..05), with hypothesis properties.

Properties: OEE identity A x E x QR = sum PRI(GQ) / PBT; ranges; microstops count against
performance, not availability; planned downtime is excluded from PBT; the loss tree partitions POT.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from twin_core.kpi import (
    DowntimeShare,
    LossCategory,
    LossTree,
    TimeModel,
    aggregate_shift_kpi,
    availability,
    buffer_change,
    capacity_loss_min,
    compute_shift_kpi,
    defect_rate,
    effectiveness,
    fpy,
    fraction_as_pp,
    is_microstop,
    line_loss_tree,
    minutes_to_units,
    mtbf_h,
    mttr_min,
    oee,
    oee_from_good,
    own_availability,
    plan_attainment,
    plan_gap,
    plan_to_date,
    pri_seconds,
    quality_ratio,
    ratio,
    required_rate,
    round_fraction,
    round_pp,
    rty,
    split_unplanned_stops,
    to_percent,
)

REL = 1e-9

shares = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def time_models(draw: st.DrawFn) -> TimeModel:
    """Valid time models: PDOT <= POT, losses <= PBT, microstops <= APT."""
    pot = draw(st.floats(min_value=1.0, max_value=1440.0, allow_nan=False))
    pdot = pot * draw(shares) * 0.5
    pbt = pot - pdot
    weights = [draw(shares) for _ in range(4)]
    used = draw(shares) * 0.9
    total = sum(weights) or 1.0
    adot, starved, blocked, aust = (pbt * used * w / total for w in weights)
    apt = pbt - adot - starved - blocked - aust
    micro = apt * draw(shares) * 0.5
    return TimeModel(
        pot=pot, pdot=pdot, adot=adot, starved=starved, blocked=blocked, aust=aust, microstop=micro
    )


@st.composite
def shifts(draw: st.DrawFn) -> tuple[TimeModel, int, int, float]:
    """Time model, PQ <= capacity at PRI, GQ <= PQ, PRI (s)."""
    time = draw(time_models())
    pri = draw(st.floats(min_value=30.0, max_value=600.0, allow_nan=False))
    capacity = math.floor(time.apt * 60 / pri)
    pq = draw(st.integers(min_value=0, max_value=max(capacity, 0)))
    gq = draw(st.integers(min_value=0, max_value=pq))
    return time, pq, gq, pri


# --------------------------------------------------------------------------- properties


@given(shifts())
def test_oee_identity(case: tuple[TimeModel, int, int, float]) -> None:
    time, pq, gq, pri = case
    assume(pq > 0 and time.apt > 0)
    k = compute_shift_kpi(time, pq=pq, gq=gq, pri_produced_s=pri * pq, pri_good_s=pri * gq)
    assert k.availability is not None
    assert k.effectiveness is not None
    assert k.quality_ratio is not None
    assert k.oee is not None
    product = k.availability * k.effectiveness * k.quality_ratio
    assert math.isclose(product, k.oee, rel_tol=REL, abs_tol=1e-12)
    assert math.isclose(
        oee(k.availability, k.effectiveness, k.quality_ratio) or 0.0,
        k.oee,
        rel_tol=REL,
        abs_tol=1e-12,
    )


@given(shifts())
def test_ranges(case: tuple[TimeModel, int, int, float]) -> None:
    time, pq, gq, pri = case
    k = compute_shift_kpi(time, pq=pq, gq=gq, pri_produced_s=pri * pq, pri_good_s=pri * gq)
    assert k.availability is not None
    assert k.own_availability is not None
    assert 0.0 <= k.availability <= 1.0 + REL
    assert k.availability - REL <= k.own_availability <= 1.0 + REL
    assert k.oee is not None
    assert 0.0 <= k.oee <= k.availability + REL
    if k.effectiveness is not None:
        assert 0.0 <= k.effectiveness <= 1.0 + REL
    if pq:
        assert k.quality_ratio is not None
        assert k.defect_rate is not None
        assert 0.0 <= k.quality_ratio <= 1.0
        assert math.isclose(k.quality_ratio + k.defect_rate, 1.0)
        assert k.fpy == k.quality_ratio
    else:
        assert k.quality_ratio is None
        assert k.defect_rate is None
    assert math.isclose(k.pbt_min - k.apt_min, k.lost_min)
    assert k.defects == pq - gq


@given(time_models(), st.floats(min_value=0.0, max_value=200.0, allow_nan=False))
def test_planned_downtime_is_excluded_from_pbt(time: TimeModel, extra_pdot: float) -> None:
    """Adding planned downtime to POT changes no KPI (it is not a loss)."""
    longer = TimeModel(
        pot=time.pot + extra_pdot,
        pdot=time.pdot + extra_pdot,
        adot=time.adot,
        starved=time.starved,
        blocked=time.blocked,
        aust=time.aust,
        microstop=time.microstop,
    )
    pq = int(time.apt // 4)
    base = compute_shift_kpi(time, pq=pq, gq=pq, pri_produced_s=pq * 60.0, pri_good_s=pq * 60.0)
    other = compute_shift_kpi(longer, pq=pq, gq=pq, pri_produced_s=pq * 60.0, pri_good_s=pq * 60.0)
    assert math.isclose(longer.pbt, time.pbt, rel_tol=REL, abs_tol=1e-9)
    for field in ("availability", "effectiveness", "oee", "own_availability"):
        a, b = getattr(base, field), getattr(other, field)
        assert (a is None and b is None) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    assert capacity_loss_min(extra_pdot, 0.0, planned=True) == 0.0


@given(
    st.lists(st.floats(min_value=0.0, max_value=3600.0, allow_nan=False), max_size=30),
    st.floats(min_value=1.0, max_value=900.0, allow_nan=False),
)
def test_microstops_split(durations: list[float], threshold: float) -> None:
    stops = split_unplanned_stops(durations, threshold)
    assert math.isclose(
        (stops.adot_min + stops.microstop_min) * 60, sum(durations), rel_tol=1e-9, abs_tol=1e-6
    )
    assert stops.failures == sum(1 for d in durations if d >= threshold)
    assert all(is_microstop(d, threshold) == (d < threshold) for d in durations)


@given(st.floats(min_value=0.0, max_value=299.0, allow_nan=False))
def test_microstop_costs_performance_not_availability(micro_s: float) -> None:
    """A stop below the threshold leaves ADOT and A alone; the lost output shows up in E."""
    threshold = 300.0
    stops = split_unplanned_stops([micro_s], threshold)
    assert stops.adot_min == 0.0
    assert stops.failures == 0
    time = TimeModel(pot=480.0, adot=stops.adot_min, microstop=stops.microstop_min)
    pri = 233.0
    full = math.floor(time.apt * 60 / pri)
    lost_units = math.ceil(micro_s / pri)
    with_micro = compute_shift_kpi(
        time,
        pq=full - lost_units,
        gq=full - lost_units,
        pri_produced_s=pri * (full - lost_units),
        pri_good_s=pri * (full - lost_units),
    )
    assert with_micro.availability == 1.0
    long_stop = split_unplanned_stops([threshold + micro_s], threshold)
    assert long_stop.failures == 1
    assert availability(480.0 - long_stop.adot_min, 480.0) == pytest.approx(
        1 - (threshold + micro_s) / 60 / 480
    )


@given(shifts(), st.lists(shares, max_size=3))
def test_loss_tree_partitions_pot(
    case: tuple[TimeModel, int, int, float], parts: list[float]
) -> None:
    time, pq, gq, pri = case
    total = sum(parts) or 1.0
    shares_ = [
        DowntimeShare(time.adot * p / total * 0.99, reason_code=f"R{i}", equipment="EQ")
        for i, p in enumerate(parts)
    ]
    tree = line_loss_tree(
        line="L",
        ict_seconds=pri,
        time=time,
        pri_produced_s=pri * pq,
        pri_good_s=pri * gq,
        unplanned=shares_,
    )
    assert math.isclose(tree.minutes() + pri * gq / 60, time.pot, rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(
        tree.minutes(LossCategory.UNPLANNED_DOWNTIME), time.adot, rel_tol=1e-9, abs_tol=1e-6
    )
    assert math.isclose(tree.minutes(LossCategory.PLANNED_DOWNTIME), time.pdot)
    assert math.isclose(tree.units(), tree.minutes() * 60 / pri, rel_tol=1e-9, abs_tol=1e-6)


@given(
    st.floats(min_value=1.0, max_value=480.0, allow_nan=False),
    st.integers(min_value=0, max_value=150),
    shares,
)
def test_aggregate_path_equals_event_path(worked: float, pq: int, good_share: float) -> None:
    """Import path (§5.4): POT=480, PDOT=ADET=0, APT=worked — same as a time model with ADOT."""
    gq = int(pq * good_share)
    agg = aggregate_shift_kpi(
        pot_min=480.0, worked_min=worked, produced=pq, good=gq, ict_seconds=233
    )
    ev = compute_shift_kpi(
        TimeModel(pot=480.0, adot=480.0 - worked),
        pq=pq,
        gq=gq,
        pri_produced_s=233.0 * pq,
        pri_good_s=233.0 * gq,
    )
    for field in ("availability", "effectiveness", "quality_ratio", "oee", "defect_rate"):
        a, b = getattr(agg, field), getattr(ev, field)
        assert (a is None and b is None) or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


# --------------------------------------------------------------------------- examples


def test_case_shift_values() -> None:
    """Appendix A: PAINT-1 on 02.10 — 116 produced, 6 defects, 7.7 h worked."""
    k = aggregate_shift_kpi(pot_min=480, worked_min=462, produced=116, good=110, ict_seconds=233)
    assert round_fraction(k.availability) == 0.9625
    assert round_fraction(k.effectiveness) == 0.975
    assert round_fraction(k.quality_ratio) == 0.9483
    assert round_fraction(k.oee) == 0.8899
    assert round_fraction(k.defect_rate) == 0.0517
    assert k.adot_min == 18
    assert k.pdot_min == 0
    assert k.adet_min == 0


def test_division_by_zero_is_none() -> None:
    assert ratio(1, 0) is None
    assert availability(0, 0) is None
    assert effectiveness(10, 0) is None
    assert quality_ratio(0, 0) is None
    assert defect_rate(0, 0) is None
    assert oee_from_good(1, 0) is None
    assert own_availability(1, 1, 0) is None
    assert mtbf_h(60, 0) is None
    assert mttr_min(10, 0) is None
    assert oee(0.9, None, 1.0) is None
    k = aggregate_shift_kpi(pot_min=480, worked_min=0, produced=0, good=0, ict_seconds=233)
    assert k.effectiveness is None
    assert k.quality_ratio is None
    assert k.oee == 0.0


def test_rounding_helpers() -> None:
    assert round_fraction(0.97504) == 0.975
    assert round_fraction(None) is None
    assert round_pp(97.54) == 97.5
    assert round_pp(None) is None
    assert to_percent(0.5) == 50.0
    assert to_percent(None) is None
    assert fraction_as_pp(0.89996) == 90.0
    assert fraction_as_pp(None) is None


def test_yields_and_reliability() -> None:
    assert fpy(98, 100) == 0.98
    assert rty([0.9831, 0.9652, 0.9917]) == pytest.approx(0.9831 * 0.9652 * 0.9917)
    assert rty([]) is None
    assert rty([0.9, None]) is None
    assert mtbf_h(600, 2) == 5.0
    assert mttr_min(50, 2) == 25.0
    k = compute_shift_kpi(
        TimeModel(pot=480, adot=50),
        pq=100,
        gq=99,
        pri_produced_s=23300,
        pri_good_s=23067,
        failures=2,
        repair_min=50,
    )
    assert k.mtbf_h == pytest.approx(430 / 60 / 2)
    assert k.mttr_min == 25.0
    no_failures = compute_shift_kpi(
        TimeModel(pot=480), pq=1, gq=1, pri_produced_s=233, pri_good_s=233, failures=0
    )
    assert no_failures.mtbf_h is None
    assert no_failures.mttr_min is None


def test_capacity_and_units_golden() -> None:
    """SPEC §5.7: ABB-01 25 min at 50% -> 12.5 min -> 3.22 cars; CONV-03 55 min -> 14.16."""
    assert capacity_loss_min(25, 0.5, planned=False) == 12.5
    assert round_fraction(minutes_to_units(12.5, 233)) == 3.2189
    assert (
        round_fraction(minutes_to_units(capacity_loss_min(55, 0.0, planned=False), 233)) == 14.1631
    )
    assert minutes_to_units(5, 0) is None
    with pytest.raises(ValueError, match="negative"):
        capacity_loss_min(-1, 0.5, planned=False)
    with pytest.raises(ValueError, match="degraded_capacity"):
        capacity_loss_min(1, 1.5, planned=False)


def test_plan_golden() -> None:
    """SPEC §5.8: 4 800 / 42 = 114.29, 5 500 / 42 = 130.95, gap -700."""
    assert round_fraction(required_rate(4800, 0, 42)) == 114.2857
    assert round_fraction(required_rate(5500, 0, 42)) == 130.9524
    assert required_rate(5500, 100, 0) is None
    assert plan_gap(4800, 5500) == -700
    to_date = plan_to_date(4800, 42, 21)
    assert to_date == 2400
    assert plan_attainment(2280, to_date) == 0.95
    assert plan_attainment(10, None) is None
    assert plan_to_date(4800, 0, 0) is None
    assert buffer_change(229, 231) == -2


def test_pri_and_validation() -> None:
    assert pri_seconds(233, 1.05) == pytest.approx(244.65)
    with pytest.raises(ValueError, match="positive"):
        pri_seconds(0)
    with pytest.raises(ValueError, match="negative"):
        TimeModel(pot=-1)
    with pytest.raises(ValueError, match="exceeds POT"):
        TimeModel(pot=10, pdot=11)
    with pytest.raises(ValueError, match="exceed PBT"):
        TimeModel(pot=10, adot=6, aust=5)
    with pytest.raises(ValueError, match="microstops exceed APT"):
        TimeModel(pot=10, adot=5, microstop=6)
    with pytest.raises(ValueError, match="exceeds produced"):
        aggregate_shift_kpi(pot_min=480, worked_min=400, produced=1, good=2, ict_seconds=233)
    with pytest.raises(ValueError, match="must not be negative"):
        aggregate_shift_kpi(pot_min=480, worked_min=-1, produced=1, good=1, ict_seconds=233)
    with pytest.raises(ValueError, match="must not be negative"):
        compute_shift_kpi(TimeModel(pot=1), pq=-1, gq=0, pri_produced_s=0, pri_good_s=0)
    with pytest.raises(ValueError, match="negative stop"):
        split_unplanned_stops([-1.0], 300)


def test_loss_tree_breakdown() -> None:
    time = TimeModel(pot=480, pdot=30, adot=40, starved=10, blocked=5, aust=0, microstop=8)
    tree = line_loss_tree(
        line="ASSY-1",
        ict_seconds=233,
        time=time,
        pri_produced_s=233 * 90,
        pri_good_s=233 * 88,
        unplanned=[DowntimeShare(30, "ME-CHAIN", "CONV-03")],
    )
    by_cat = tree.by_category()
    assert by_cat[LossCategory.PLANNED_DOWNTIME][0] == 30
    assert by_cat[LossCategory.UNPLANNED_DOWNTIME][0] == 40
    unattributed = [
        i for i in tree.items if i.category == LossCategory.UNPLANNED_DOWNTIME and not i.reason_code
    ]
    assert len(unattributed) == 1
    assert unattributed[0].minutes == 10
    assert by_cat[LossCategory.QUALITY][0] == pytest.approx(2 * 233 / 60)
    assert by_cat[LossCategory.QUALITY][1] == pytest.approx(2.0)
    merged = tree + LossTree()
    assert merged.minutes() == tree.minutes()
    with pytest.raises(ValueError, match="exceeds ADOT"):
        line_loss_tree(
            line="L",
            ict_seconds=233,
            time=time,
            pri_produced_s=0,
            pri_good_s=0,
            unplanned=[DowntimeShare(41)],
        )
