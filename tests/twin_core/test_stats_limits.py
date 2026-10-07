"""Theil–Sen, Spearman (twin_core.stats) and time-to-limit with the AL-M2 window (SPEC §11.2)."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.stats import spearmanr

from twin_core.config import TwinConfig
from twin_core.limits import (
    forecast_limit,
    limit_advice,
    limited_signals,
    maintenance_saving,
    recommend_window,
)
from twin_core.stats import spearman, theil_sen

# --------------------------------------------------------------------------- Theil–Sen


@given(
    slope=st.floats(-50, 50),
    intercept=st.floats(-1000, 1000),
    n=st.integers(2, 40),
)
def test_theil_sen_recovers_an_exact_line(slope: float, intercept: float, n: int) -> None:
    xs = [float(i) for i in range(n)]
    line = theil_sen(xs, [intercept + slope * x for x in xs])
    assert line is not None
    assert line.slope == pytest.approx(slope, abs=1e-6)
    assert line.at(0.0) == pytest.approx(intercept, abs=1e-6)
    assert line.n == n


def test_theil_sen_ignores_outliers_and_nan() -> None:
    rng = random.Random(1)
    xs = [i / 12 for i in range(72)]
    ys = [100 + 7 * x + rng.gauss(0, 1) for x in xs]
    for i in range(0, 72, 5):  # 20% gross outliers
        ys[i] += 500
    ys[3] = math.nan
    line = theil_sen(xs, ys)
    assert line is not None
    assert line.slope == pytest.approx(7, abs=0.5)
    assert line.n == 71


def test_theil_sen_degenerate_inputs() -> None:
    assert theil_sen([1.0], [2.0]) is None
    assert theil_sen([1.0, 1.0], [2.0, 3.0]) is None
    assert theil_sen([math.nan, 1.0], [1.0, 2.0]) is None
    with pytest.raises(ValueError, match="same length"):
        theil_sen([1.0, 2.0], [1.0])


# --------------------------------------------------------------------------- Spearman


def test_spearman_matches_scipy_with_ties() -> None:
    rng = np.random.default_rng(3)
    x = rng.integers(0, 8, 60).astype(float)  # many ties
    y = x * 0.5 + rng.normal(0, 2, 60)
    ours = spearman(x, y)
    ref = spearmanr(x, y)
    assert ours is not None
    assert ours.rho == pytest.approx(float(ref.statistic), abs=1e-12)
    assert ours.p_value == pytest.approx(float(ref.pvalue), rel=1e-9)
    assert ours.n == 60


def test_spearman_edges() -> None:
    assert spearman([1.0, 2.0], [1.0, 2.0]) is None
    assert spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    perfect = spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0])
    assert perfect is not None
    assert perfect.rho == pytest.approx(1.0)
    assert perfect.p_value < 1e-12


# --------------------------------------------------------------------------- time to limit


def _ramp(
    now: datetime, level: float, slope: float, hours: float, step_min: int = 5, noise: float = 4.0
) -> tuple[list[datetime], list[float]]:
    rng = random.Random(7)
    ts: list[datetime] = []
    values: list[float] = []
    n = int(hours * 60 / step_min)
    for k in range(n, -1, -1):
        t = now - timedelta(minutes=k * step_min)
        x = (t - now).total_seconds() / 3600
        ts.append(t)
        values.append(level + slope * x + rng.gauss(0, noise))
    return ts, values


def test_s2_filter_forecast_and_shift_change_window(cfg: TwinConfig) -> None:
    """S2: ΔP 370 Pa rising ~7 Pa/h, limit 450 → ≈ 11.4 h; service at the 15:00 shift change."""
    booth = next(s for s in cfg.equipment_types["booth"].signals if s.code == "filter_dp_pa")
    assert booth.limit_hi == 450
    now = cfg.simulation.clock.demo_start  # 16.10.2026 07:00 local
    ts, values = _ramp(now, 370.0, 7.0, hours=8)
    forecast = forecast_limit(ts, values, limit_hi=booth.limit_hi, now=now)
    assert forecast is not None
    assert forecast.n_points == 73  # only the last 6 h at 5 min (both ends included)
    assert forecast.slope_per_h == pytest.approx(7.0, abs=0.6)
    assert forecast.level_now == pytest.approx(370.0, abs=3.0)
    assert forecast.hours_to_limit == pytest.approx(11.4, abs=0.6)

    lookahead = cfg.rules.thresholds.telemetry_limit_lookahead_h
    pf = cfg.simulation.paint_filters
    assert pf is not None
    line = cfg.line_of_equipment("BOOTH-02")
    eq = cfg.equipment["BOOTH-02"]
    advice = limit_advice(
        forecast,
        calendar=cfg.calendar,
        lookahead_h=lookahead,
        stop_min=pf.replacement.median,
        ict_seconds=line.ict_seconds,
        degraded_capacity=eq.degraded_capacity,
    )
    assert advice.alert
    assert advice.window is not None
    assert advice.window.astimezone(cfg.timezone).strftime("%H:%M") == "15:00"
    assert advice.saving.minutes == pytest.approx(40.0)
    assert advice.saving.cars == pytest.approx(40 * 60 / 233)


def test_forecast_edge_cases(cfg: TwinConfig) -> None:
    now = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    ts, values = _ramp(now, 460.0, 7.0, hours=6, noise=0.0)
    above = forecast_limit(ts, values, limit_hi=450, now=now)
    assert above is not None
    assert above.hours_to_limit == 0.0
    assert above.limit_at == now

    ts, values = _ramp(now, 300.0, -3.0, hours=6, noise=0.0)
    falling = forecast_limit(ts, values, limit_hi=450, now=now)
    assert falling is not None
    assert falling.hours_to_limit is None
    assert falling.limit_at is None
    advice = limit_advice(
        falling, calendar=cfg.calendar, lookahead_h=12, stop_min=40, ict_seconds=233
    )
    assert not advice.alert
    assert advice.window is None

    # after a replacement 2 h ago only the new trend counts
    restart = now - timedelta(hours=2)
    jump_ts, jump_values = _ramp(now, 200.0, 7.0, hours=6, noise=0.0)
    jump_values = [
        v + (300.0 if t < restart else 0.0) for t, v in zip(jump_ts, jump_values, strict=True)
    ]
    fresh = forecast_limit(jump_ts, jump_values, limit_hi=450, now=now, since=restart)
    assert fresh is not None
    assert fresh.n_points == 25
    assert fresh.slope_per_h == pytest.approx(7.0)
    assert fresh.hours_to_limit == pytest.approx(250 / 7)

    few_ts, few_values = ts[-3:], values[-3:]
    assert forecast_limit(few_ts, few_values, limit_hi=450, now=now) is None
    flat_ts = [now] * 10
    assert forecast_limit(flat_ts, [1.0] * 10, limit_hi=450, now=now) is None
    with pytest.raises(ValueError, match="same length"):
        forecast_limit(ts, values[:-1], limit_hi=450, now=now)


def test_window_none_when_limit_before_next_shift_change(cfg: TwinConfig) -> None:
    local = cfg.timezone
    now = datetime(2026, 10, 16, 14, 0, tzinfo=local)
    assert recommend_window(cfg.calendar, now=now, limit_at=now + timedelta(minutes=30)) is None
    window = recommend_window(cfg.calendar, now=now, limit_at=now + timedelta(hours=2))
    assert window is not None
    assert window.astimezone(local).hour == 15
    # Friday 23:00 is the last change before Monday 07:00
    late = datetime(2026, 10, 16, 23, 30, tzinfo=local)
    monday = recommend_window(cfg.calendar, now=late, limit_at=late + timedelta(days=3))
    assert monday is not None
    assert monday.astimezone(local) == datetime(2026, 10, 19, 7, 0, tzinfo=local)


def test_maintenance_saving() -> None:
    saving = maintenance_saving(
        stop_min=45, ict_seconds=233, degraded_capacity=0.5, service_loss_min=5
    )
    assert saving.minutes == 40
    assert saving.cars == pytest.approx(40 * 60 / 233 * 0.5)
    assert maintenance_saving(stop_min=10, ict_seconds=233, service_loss_min=20).minutes == 0
    with pytest.raises(ValueError, match="ict_seconds"):
        maintenance_saving(stop_min=10, ict_seconds=0)


def test_limited_signals_come_from_config(cfg: TwinConfig) -> None:
    codes = {
        (t, s.code) for t, et in cfg.equipment_types.items() for s in limited_signals(et.signals)
    }
    assert codes == {
        ("booth", "filter_dp_pa"),
        ("conveyor", "vibration_mm_s"),
        ("conveyor", "chain_elongation_pct"),
    }
