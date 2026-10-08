"""T-BN: active-period bottleneck detection (SPEC §9.5, Roser et al. 2002)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from twin_core.bottleneck import (
    Period,
    ShiftingBottleneck,
    active_periods,
    is_active,
    shifting_bottleneck,
)

ORDER = ["L1", "L2", "L3", "L4", "L5"]


def test_t_bn_1() -> None:
    """L1 active 0-60; L2 active 0-20 and 30-60 -> L1 sole 1.0, L2 nothing."""
    r = shifting_bottleneck(
        {"L1": [Period(0, 60)], "L2": [Period(0, 20), Period(30, 60)]}, (0, 60), ORDER
    )
    assert r.sole_share == {"L1": pytest.approx(1.0)}
    assert r.shifting_share == {}
    assert r.overall == "L1"
    assert r.transitions == ()


def test_t_bn_2() -> None:
    """L1 0-40, L2 10-60: b = L1 on 0-10, L2 on 10-60; shifting 10-40 for both."""
    r = shifting_bottleneck({"L1": [Period(0, 40)], "L2": [Period(10, 60)]}, (0, 60), ORDER)
    assert r.sole_share["L1"] == pytest.approx(10 / 60)
    assert r.sole_share["L2"] == pytest.approx(20 / 60)
    assert r.shifting_share["L1"] == pytest.approx(0.5)
    assert r.shifting_share["L2"] == pytest.approx(0.5)
    assert r.share(r.sole["L1"] + r.shifting["L1"]) == pytest.approx(0.6667, abs=1e-4)
    assert r.share(r.sole["L2"] + r.shifting["L2"]) == pytest.approx(0.8333, abs=1e-4)
    assert r.overall == "L2"
    assert [(s.start, s.end, s.line) for s in r.segments] == [(0, 10, "L1"), (10, 60, "L2")]
    assert r.current == "L2"
    assert r.current_since == 10


def test_t_bn_2_from_state_timelines() -> None:
    """The same case built from state timelines (STARVED breaks a period, DOWN does not)."""
    l1 = active_periods(
        [(0, 30, "RUNNING"), (30, 40, "DOWN_UNPLANNED"), (40, 60, "STARVED")], (0, 60)
    )
    l2 = active_periods([(0, 10, "STARVED"), (10, 60, "RUNNING")], (0, 60))
    r = shifting_bottleneck({"L1": l1, "L2": l2}, (0, 60), ORDER)
    assert r.overall == "L2"
    assert r.shifting_share["L1"] == pytest.approx(0.5)


def test_activity_and_clipping() -> None:
    assert is_active("RUNNING")
    assert is_active("DEGRADED")
    assert is_active("DOWN_PLANNED")
    assert not is_active("STARVED")
    assert not is_active("BLOCKED")
    assert not is_active("IDLE_NO_PLAN")
    periods = active_periods(
        [(0, 10, "RUNNING"), (10, 20, "DOWN_UNPLANNED"), (20, 30, "STARVED"), (30, 50, "RUNNING")],
        (5, 45),
    )
    assert periods == [Period(5, 20, 20), Period(30, 45, 20)]
    # a gap in the data ends a period
    assert len(active_periods([(0, 10, "RUNNING"), (12, 20, "RUNNING")], (0, 20))) == 2


def test_ties_use_unclipped_length_then_flow_order() -> None:
    # both lines active over the whole window; L2's period started earlier (longer in reality)
    r = shifting_bottleneck({"L1": [Period(0, 60, 60)], "L2": [Period(0, 60, 90)]}, (0, 60), ORDER)
    assert r.sole_share == {"L2": pytest.approx(1.0)}
    r = shifting_bottleneck({"L1": [Period(0, 60)], "L2": [Period(0, 60)]}, (0, 60), ORDER)
    assert r.sole_share == {"L1": pytest.approx(1.0)}


def test_no_active_line_means_no_bottleneck() -> None:
    r = shifting_bottleneck({"L1": [Period(10, 20)], "L2": []}, (0, 60), ORDER)
    assert r.sole_share == {"L1": pytest.approx(10 / 60)}
    assert r.segments[0].line is None
    assert r.current is None
    assert r.current_since is None
    empty = shifting_bottleneck({"L1": []}, (0, 0), ORDER)
    assert empty.overall is None
    assert empty.shares() == {}


# --------------------------------------------------------------------------- invariants


@st.composite
def timelines(draw: st.DrawFn) -> tuple[dict[str, list[Period]], tuple[float, float]]:
    n = draw(st.integers(2, 5))
    window = (0.0, 120.0)
    out: dict[str, list[Period]] = {}
    for line in ORDER[:n]:
        cuts = sorted(set(draw(st.lists(st.integers(1, 119), max_size=10))))
        bounds = [0, *cuts, 120]
        states = draw(
            st.lists(
                st.sampled_from(["RUNNING", "STARVED", "BLOCKED", "DOWN_UNPLANNED"]),
                min_size=len(bounds) - 1,
                max_size=len(bounds) - 1,
            )
        )
        spans = [
            (float(a), float(b), s) for a, b, s in zip(bounds, bounds[1:], states, strict=False)
        ]
        out[line] = active_periods(spans, window)
    return out, window


def _check_invariants(r: ShiftingBottleneck, periods: dict[str, list[Period]]) -> None:
    total = r.length
    for line in periods:
        assert r.sole.get(line, 0.0) + r.shifting.get(line, 0.0) <= total + 1e-9
    covered = 0.0
    for piece in r.pieces:
        assert piece.end > piece.start
        assert not (piece.sole and piece.shifting)
        if piece.shifting is not None:
            assert len(set(piece.shifting)) == 2
        if piece.sole is not None:
            mid = (piece.start + piece.end) / 2
            assert any(p.start <= mid < p.end for p in periods[piece.sole]), "b(t) must be active"
        if piece.sole or piece.shifting:
            covered += piece.end - piece.start
    any_active = 0.0
    for piece in r.pieces:
        mid = (piece.start + piece.end) / 2
        if any(p.start <= mid < p.end for ps in periods.values() for p in ps):
            any_active += piece.end - piece.start
    assert sum(r.sole.values()) + sum(r.shifting.values()) / 2 == pytest.approx(covered)
    assert covered == pytest.approx(any_active)


@settings(max_examples=150, deadline=None)
@given(timelines())
def test_invariants_on_random_timelines(
    case: tuple[dict[str, list[Period]], tuple[float, float]],
) -> None:
    periods, window = case
    r = shifting_bottleneck(periods, window, ORDER)
    _check_invariants(r, periods)
    if r.overall is not None:
        best = max(r.sole.get(x, 0.0) + r.shifting.get(x, 0.0) for x in periods)
        assert r.sole.get(r.overall, 0.0) + r.shifting.get(r.overall, 0.0) == pytest.approx(best)


@settings(max_examples=60, deadline=None)
@given(timelines(), st.integers(10, 110))
def test_live_equals_closed_window_on_truncated_periods(
    case: tuple[dict[str, list[Period]], tuple[float, float]], now: int
) -> None:
    periods, _window = case
    truncated = {
        line: [Period(p.start, min(p.end, now)) for p in ps if p.start < now]
        for line, ps in periods.items()
    }
    live = shifting_bottleneck(truncated, (0, now), ORDER)
    _check_invariants(live, truncated)
    assert live.length == now
