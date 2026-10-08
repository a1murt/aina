"""Working-time axis, level shifts and the step-aware Theil–Sen (AL-M2, SPEC §11.2)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import numpy as np
import pytest

from twin_core.calendar import WorkingTime
from twin_core.config import TwinConfig
from twin_core.limits import detect_level_shifts, forecast_limit, unit_limit_advice
from twin_core.stats import theil_sen_segmented


def test_working_time_collapses_nights_and_inverts(cfg: TwinConfig) -> None:
    day = datetime(2026, 10, 15, 12, 0, tzinfo=cfg.timezone)  # Thursday, shift A+B 07-23
    wt = WorkingTime(cfg.calendar, day)

    def at(h: int, m: int = 0, d: int = 15) -> datetime:
        return datetime(2026, 10, d, h, m, tzinfo=cfg.timezone)

    assert wt.is_working(at(8))
    assert not wt.is_working(at(23, 30))
    assert not wt.is_working(at(5))
    one_day = wt.working_seconds(at(7, d=16)) - wt.working_seconds(at(7))
    assert one_day == 16 * 3600  # 07:00 -> 23:00 works, the night does not count
    night = wt.working_seconds(at(6, d=16)) - wt.working_seconds(at(23))
    assert night == 0
    w = wt.working_seconds(at(10)) + 5 * 3600
    assert wt.instant_at(w) == at(15)
    assert wt.instant_at(wt.working_seconds(at(22)) + 2 * 3600) == at(8, d=16)  # over the night
    assert wt.instant_at(1e12) is None


def test_segmented_theil_sen_ignores_the_jump() -> None:
    x = np.linspace(-6, 0, 73)
    y = 400 + 7 * x
    y[x >= -0.25] -= 80  # the filter state was reset: a level shift of -80 at the end
    seg = (x >= -0.25).astype(int)
    line = theil_sen_segmented(x, y, seg)
    assert line is not None
    assert line.slope == pytest.approx(7.0, abs=0.05)
    assert line.intercept == pytest.approx(320.0, abs=0.5)
    assert theil_sen_segmented(x, y, np.zeros_like(seg))  # no shift known: the plain fit exists
    assert theil_sen_segmented(x[-2:], y[-2:], seg[-2:]) is None  # fewer than 3 points in the level
    with pytest.raises(ValueError, match="same length"):
        theil_sen_segmented(x, y[:-1], seg)


def test_level_shift_detection_is_robust_and_quiet_on_noise() -> None:
    rng = random.Random(3)
    noise = [450 + rng.gauss(0, 4) for _ in range(80)]
    assert detect_level_shifts(noise, min_jump=30) == []
    stepped = noise[:40] + [v - 80 for v in noise[40:]]
    assert detect_level_shifts(stepped, min_jump=30) == [40]
    smooth = [0.4 + 0.001 * i for i in range(50)]  # noise-free drift: nothing to flag
    assert detect_level_shifts(smooth, min_jump=0.25) == []
    assert detect_level_shifts([1, 2, 3], min_jump=0) == []


def test_forecast_on_working_time_after_a_night_and_a_step(cfg: TwinConfig) -> None:
    rng = random.Random(5)
    demo = cfg.simulation.clock.demo_start  # 07:00 local
    ts: list[datetime] = []
    vals: list[float] = []
    t = demo - timedelta(hours=14)  # 17:00 local the day before: shift B
    level = 394.0
    while t < demo:
        if cfg.calendar.shift_at(t, working_only=True) is not None:
            level = min(450.0, level + 7 * 5 / 60)
        ts.append(t)
        vals.append(level + rng.gauss(0, 4))
        t += timedelta(minutes=5)
    now = demo + timedelta(minutes=30)
    for k in range(31):  # S2: the state jumps to 370 and rises again
        ts.append(demo + timedelta(minutes=k))
        vals.append(370 + 7 * k / 60 + rng.gauss(0, 4))
    wall = forecast_limit(ts, vals, limit_hi=450, now=now)  # wall-clock window, no shift handling
    work = forecast_limit(
        ts, vals, limit_hi=450, now=now, working=WorkingTime(cfg.calendar, now), shift_min_jump=30
    )
    assert work is not None
    assert work.level_shifts == 1
    assert work.level_now == pytest.approx(373.5, abs=5)
    assert work.hours_to_limit is not None
    assert 9.5 <= work.hours_to_limit <= 12.5
    assert work.work_hours_to_limit == pytest.approx(
        work.hours_to_limit, abs=0.01
    )  # no night ahead
    assert wall is None or wall.hours_to_limit != work.hours_to_limit
    # a step with fewer than 3 samples after it yields no forecast instead of a wrong one
    early = forecast_limit(
        ts[:-29],
        vals[:-29],
        limit_hi=450,
        now=demo + timedelta(minutes=1),
        working=WorkingTime(cfg.calendar, demo),
        shift_min_jump=30,
    )
    assert early is None


def test_unit_limit_advice_for_booths_and_conveyors(cfg: TwinConfig) -> None:
    now = cfg.simulation.clock.demo_start + timedelta(hours=1)
    ts = [now - timedelta(minutes=k) for k in range(60, -1, -1)]
    rising = [380 + 7 * (t - now).total_seconds() / 3600 + 60 * 7 / 60 for t in ts]
    out = unit_limit_advice(cfg, "BOOTH-02", {"filter_dp_pa": (ts, rising)}, now=now)
    assert set(out) == {"filter_dp_pa"}
    assert out["filter_dp_pa"].alert
    assert out["filter_dp_pa"].saving.minutes == 40.0
    # a conveyor repair is longer than a filter change; units without limited signals give nothing
    flat = {"vibration_mm_s": (ts, [2.0] * len(ts))}
    conv = unit_limit_advice(cfg, "CONV-03", flat, now=now)
    assert not conv["vibration_mm_s"].alert
    assert conv["vibration_mm_s"].saving.minutes == 45.0
    assert (
        unit_limit_advice(cfg, "ABB-04", {"motor_current_a": (ts, [1.0] * len(ts))}, now=now) == {}
    )
