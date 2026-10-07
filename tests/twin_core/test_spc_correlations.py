"""SPC p-chart with Western Electric rules (SPEC §11.3, AL-Q2) and defect correlations (§11.4)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from twin_core.config import TwinConfig
from twin_core.correlations import (
    HourStat,
    band_center,
    defect_correlations,
    deviation,
)
from twin_core.spc import Subgroup, p_chart, western_electric

# --------------------------------------------------------------------------- Western Electric


def _rules_at_end(z: list[float]) -> set[int]:
    last = len(z) - 1
    return {v.rule for v in western_electric(z) if v.end == last}


def test_rule1_beyond_three_sigma() -> None:
    violations = western_electric([0.1, -0.2, 3.2])
    assert [(v.rule, v.side, v.start, v.end) for v in violations] == [(1, 1, 2, 2)]
    assert _rules_at_end([0.0, -3.5]) == {1}
    assert _rules_at_end([0.0, 3.0]) == set()  # exactly at the limit is inside


def test_rule2_two_of_three_beyond_two_sigma_same_side() -> None:
    assert 2 in _rules_at_end([2.5, 0.0, 2.1])
    assert 2 not in _rules_at_end([2.5, 0.0, -2.1])  # opposite sides
    assert 2 not in _rules_at_end([2.5, 2.1, 0.0])  # the completing point must be in the zone
    v = next(v for v in western_electric([0.0, -2.2, -2.4]) if v.rule == 2)
    assert v.side == -1


def test_rule3_four_of_five_beyond_one_sigma() -> None:
    assert 3 in _rules_at_end([1.2, 1.5, 0.3, 1.1, 1.8])
    assert 3 not in _rules_at_end([1.2, 0.5, 0.3, 1.1, 1.8])


def test_rule4_eight_on_one_side() -> None:
    assert _rules_at_end([0.2] * 8) == {4}
    assert _rules_at_end([0.2] * 7) == set()
    assert 4 not in _rules_at_end([0.2] * 4 + [0.0] + [0.2] * 3)  # a point on the centre breaks it
    below = western_electric([-0.5] * 9)
    assert [(v.rule, v.side, v.start, v.end) for v in below] == [(4, -1, 0, 7), (4, -1, 1, 8)]


def test_rules_subset() -> None:
    only_rule1 = western_electric([3.5] * 8, rules=[1])
    assert len(only_rule1) == 8
    assert all(v.rule == 1 for v in only_rule1)


# --------------------------------------------------------------------------- p-chart


def _shifts(rates: list[tuple[int, int]], special: set[int] | None = None) -> list[Subgroup]:
    special = special or set()
    return [
        Subgroup(
            key=f"2026-10-{1 + i // 2:02d}/{'AB'[i % 2]}",
            defects=d,
            n=n,
            special_cause=i in special,
        )
        for i, (d, n) in enumerate(rates)
    ]


def test_p_chart_limits_follow_subgroup_size(cfg: TwinConfig) -> None:
    norm = cfg.rules.thresholds.defect_rate_limit
    subgroups = _shifts([(5, 115)] * 19 + [(10, 230)])
    chart = p_chart(subgroups, norm=norm)
    assert chart.p_bar == pytest.approx(5 / 115)
    assert chart.norm == 0.02
    small, big = chart.points[0], chart.points[-1]
    p = 5 / 115
    assert small.ucl == pytest.approx(p + 3 * math.sqrt(p * (1 - p) / 115))
    assert small.lcl == max(0.0, p - 3 * math.sqrt(p * (1 - p) / 115))
    assert big.ucl < small.ucl  # bigger subgroup → tighter limits
    assert all(pt.lcl >= 0 for pt in chart.points)
    assert chart.in_control
    assert chart.latest_violations == ()


def test_p_chart_baseline_last_20_without_special_causes() -> None:
    history = [(30, 100)] * 5 + [(3, 100)] * 20  # old bad period falls out of the window
    flagged = len(history) - 3
    history[flagged] = (40, 100)
    chart = p_chart(_shifts(history, special={flagged}))
    assert len(chart.baseline_keys) == 19
    assert chart.p_bar == pytest.approx(0.03)
    flagged_point = chart.points[flagged]
    assert flagged_point.special_cause
    assert not flagged_point.in_baseline
    assert 1 in flagged_point.rules  # still plotted and judged
    assert chart.points[0].rules  # the old bad shifts are far above the current centre


def test_p_chart_raises_al_q2_for_the_newest_shift() -> None:
    chart = p_chart(_shifts([(3, 115)] * 20 + [(14, 115)]))
    assert chart.latest_violations
    assert {v.rule for v in chart.latest_violations} == {1}
    assert chart.points[-1].rules == (1,)
    assert chart.latest_violations[0].keys == (chart.points[-1].key,)


def test_p_chart_skips_empty_shifts_and_handles_zero_p_bar() -> None:
    chart = p_chart(_shifts([(0, 0), (0, 100), (0, 120)]))
    assert [pt.n for pt in chart.points] == [100, 120]
    assert chart.p_bar == 0.0
    assert all(pt.z == 0.0 and pt.ucl == 0.0 for pt in chart.points)
    spike = p_chart(_shifts([(0, 100), (0, 100), (1, 100)], special={2}))  # p̄ = 0
    assert spike.points[-1].z == math.inf
    assert 1 in spike.points[-1].rules
    empty = p_chart(_shifts([(0, 0)]))
    assert empty.p_bar is None
    assert empty.points == ()
    assert empty.latest_violations == ()


def test_p_chart_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="defects <= n"):
        p_chart([Subgroup("x", 5, 3)])
    with pytest.raises(ValueError, match="baseline_size"):
        p_chart([], baseline_size=0)


# --------------------------------------------------------------------------- correlations


def _hours(n: int, rng: np.random.Generator, coupling: float) -> list[HourStat]:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    out: list[HourStat] = []
    for i in range(n):
        dp = float(rng.uniform(150, 450))
        humidity = float(55 + 10 * math.sin(i / 24 * 2 * math.pi) + rng.normal(0, 1))
        pq = 15
        p = 0.02 + coupling * (dp - 150) / 300
        defects = int(rng.binomial(pq, min(0.9, p)))
        out.append(
            HourStat(
                hour=start + timedelta(hours=i),
                pq=pq,
                defects=defects,
                factors={"filter_dp_pa": dp, "humidity_dev": abs(humidity - 55)},
            )
        )
    return out


def test_paint_defects_follow_filter_dp(cfg: TwinConfig) -> None:
    humidity = next(s for s in cfg.equipment_types["booth"].signals if s.code == "humidity_pct")
    assert band_center(humidity) == 55.0
    assert deviation([45.0, 60.0], 55.0) == [10.0, 5.0]
    rng = np.random.default_rng(11)
    hours = _hours(14 * 16, rng, coupling=0.15)  # 14 days x 16 working hours
    hours.append(HourStat(hour=hours[-1].hour, pq=0, defects=0, factors={"filter_dp_pa": 1.0}))
    results = {r.factor: r for r in defect_correlations(hours, ["filter_dp_pa", "humidity_dev"])}
    dp = results["filter_dp_pa"]
    assert dp.insight
    assert dp.direction == 1
    assert dp.rho >= 0.3
    assert dp.p_value < 0.05
    assert dp.n == 14 * 16  # the hour without production is skipped
    assert len(dp.points) == dp.n
    assert not results["humidity_dev"].insight


def test_correlations_skip_short_or_constant_series() -> None:
    rng = np.random.default_rng(2)
    hours = _hours(5, rng, coupling=0.1)
    assert defect_correlations(hours, ["filter_dp_pa"]) == []
    flat = [
        HourStat(h.hour, h.pq, h.defects, {"x": 1.0, "y": math.nan}) for h in _hours(30, rng, 0.1)
    ]
    assert defect_correlations(flat, ["x", "y", "missing"]) == []


def test_band_center_needs_a_band(cfg: TwinConfig) -> None:
    dp = next(s for s in cfg.equipment_types["booth"].signals if s.code == "filter_dp_pa")
    with pytest.raises(ValueError, match="warn_lo"):
        band_center(dp)
