"""Time until a signal reaches its limit (SPEC §11.2, AL-M2).

For signals with ``limit_hi`` (filter ΔP, chain elongation, vibration): a Theil–Sen line over the
last ``window_h`` hours (6 h by SPEC) → the level "now" and the hours until ``limit_hi``. If that is
within ``rules.yaml: thresholds.telemetry_limit_lookahead_h``, AL-M2 recommends servicing at the
nearest shift change before the limit (calendar from :mod:`twin_core.calendar`) and estimates the
minutes and cars saved compared with an unplanned stop during production.

Wear-like signals move only while the plant works. With a :class:`~twin_core.calendar.WorkingTime`
the fit uses the last ``window_h`` *working* hours (idle samples are dropped, the night and weekends
collapse), and the time to the limit is converted back to a wall-clock instant. A level shift inside
the window (a replaced filter, an S2-style step) is detected, the slope then comes from pairs inside
the segments and the level from the newest segment only
(:func:`twin_core.stats.theil_sen_segmented`).

Pure functions: "now" is a parameter (the caller takes it from :class:`twin_core.clock.Clock`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from twin_core.calendar import PlantCalendar, WorkingTime
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.config.plant import Signal
from twin_core.stats import Line, theil_sen, theil_sen_segmented

FIT_WINDOW_H = 6.0
"""SPEC §11.2: robust regression over the last 6 hours."""
MIN_POINTS = 6
"""Fewer samples in the window → no forecast (a gap in data must not raise an alarm)."""
MAX_POINTS = 180
"""Samples above this are thinned evenly (Theil–Sen is O(n²)); the newest sample is kept."""
SHIFT_SIGMA = 6.0
"""A jump between consecutive samples counts as a level shift above this many robust sigmas."""
SHIFT_RANGE_FRACTION = 0.05
"""... and above this share of the signal's physical range (noise-free signals stay quiet)."""
_SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class LimitForecast:
    """Theil–Sen extrapolation of one signal towards ``limit``."""

    limit: float
    level_now: float
    """Fitted value at ``now`` (robust to the last noisy sample)."""
    slope_per_h: float
    hours_to_limit: float | None
    """0 if the fitted level is already at/above the limit; ``None`` if the signal is not rising."""
    limit_at: datetime | None
    n_points: int
    now: datetime
    work_hours_to_limit: float | None = None
    """Same, in working hours (when the fit used a working-time axis)."""
    level_shifts: int = 0
    """Level shifts found inside the window (the fit ignores the jumps)."""


def detect_level_shifts(
    values: Sequence[float], *, min_jump: float = 0.0, sigma: float = SHIFT_SIGMA
) -> list[int]:
    """Indices ``i`` where ``values[i]`` jumps away from ``values[i - 1]`` (a level shift).

    The jump must exceed ``sigma`` robust standard deviations of the consecutive differences
    (1.4826 x MAD) and ``min_jump``. ``values`` are in time order.
    """
    v = np.asarray(values, dtype=np.float64)
    if len(v) < 4:
        return []
    d = np.diff(v)
    med = float(np.median(d))
    scale = 1.4826 * float(np.median(np.abs(d - med)))
    limit = max(sigma * scale, min_jump, 1e-12)
    return [int(i) + 1 for i in np.nonzero(np.abs(d - med) > limit)[0]]


def forecast_limit(
    ts: Sequence[datetime],
    values: Sequence[float],
    *,
    limit_hi: float,
    now: datetime,
    window_h: float = FIT_WINDOW_H,
    min_points: int = MIN_POINTS,
    since: datetime | None = None,
    working: WorkingTime | None = None,
    shift_min_jump: float | None = None,
    max_points: int = MAX_POINTS,
) -> LimitForecast | None:
    """Forecast when ``values`` reach ``limit_hi`` using samples of the last ``window_h`` hours.

    ``since`` cuts the window further, e.g. at the end of the last filter replacement or
    maintenance: the trend restarts there. With ``working`` the window is ``window_h`` working
    hours and the axis is working time (see the module docstring); ``shift_min_jump`` switches on
    level-shift detection with that minimal jump (signal units). Returns ``None`` with fewer than
    ``min_points`` finite samples in the window.
    """
    if len(ts) != len(values):
        raise ValueError("ts and values must have the same length")
    moment = ensure_utc(now)
    lo = moment - timedelta(hours=window_h) if working is None else moment - timedelta(days=60)
    if since is not None:
        lo = max(lo, ensure_utc(since))
    w_now = working.working_seconds(moment) if working is not None else 0.0
    inside: list[tuple[datetime, float]] = []
    for instant, value in zip(ts, values, strict=True):
        t = ensure_utc(instant)
        if lo <= t <= moment and math.isfinite(value):
            inside.append((t, float(value)))
    inside.sort(key=lambda r: r[0])
    rows: list[tuple[datetime, float, float]] = []
    for t, value in reversed(inside):
        if working is None:
            x = (t - moment).total_seconds() / _SECONDS_PER_HOUR
        else:
            if not working.is_working(t):
                continue
            x = (working.working_seconds(t) - w_now) / _SECONDS_PER_HOUR
            if x < -window_h:
                break
        rows.append((t, x, value))
    rows.reverse()
    if len(rows) < min_points:
        return None
    if len(rows) > max_points:
        keep = np.unique(np.linspace(0, len(rows) - 1, max_points).round().astype(int))
        rows = [rows[i] for i in keep]
    xs = [r[1] for r in rows]
    ys = [r[2] for r in rows]
    shifts = detect_level_shifts(ys, min_jump=shift_min_jump) if shift_min_jump is not None else []
    line: Line | None
    if shifts:
        seg = np.zeros(len(rows), dtype=np.int64)
        for idx in shifts:
            seg[idx:] += 1
        line = theil_sen_segmented(xs, ys, seg)
    else:
        line = theil_sen(xs, ys)
    if line is None:
        return None
    level = line.intercept  # x = 0 is "now"
    need: float | None  # hours on the fit's axis
    if level >= limit_hi:
        need = 0.0
    elif line.slope > 0:
        need = (limit_hi - level) / line.slope
    else:
        need = None
    limit_at: datetime | None = None
    hours: float | None = None
    if need is not None:
        if working is None:
            limit_at = moment + timedelta(hours=need)
        elif need == 0.0:
            limit_at = moment
        else:
            limit_at = working.instant_at(w_now + need * _SECONDS_PER_HOUR)
        if limit_at is not None:
            hours = (limit_at - moment).total_seconds() / _SECONDS_PER_HOUR
    return LimitForecast(
        limit=limit_hi,
        level_now=level,
        slope_per_h=line.slope,
        hours_to_limit=hours,
        limit_at=limit_at,
        n_points=line.n,
        now=moment,
        work_hours_to_limit=need if working is not None else hours,
        level_shifts=len(shifts),
    )


def recommend_window(
    calendar: PlantCalendar, *, now: datetime, limit_at: datetime
) -> datetime | None:
    """Nearest working-shift change after ``now`` that comes no later than ``limit_at``.

    ``None`` means the limit is reached before the next shift change: service now.
    """
    change = calendar.next_shift_change(now)
    if change is None or change > ensure_utc(limit_at):
        return None
    return change


@dataclass(frozen=True, slots=True)
class MaintenanceSaving:
    minutes: float
    """Production minutes not lost to an unplanned stop."""
    cars: float
    """Cars not lost: minutes x 60 / ICT, scaled by the share of capacity the stop takes away."""


def maintenance_saving(
    *,
    stop_min: float,
    ict_seconds: float,
    degraded_capacity: float = 0.0,
    service_loss_min: float = 0.0,
) -> MaintenanceSaving:
    """Saving from servicing at a shift change instead of failing during production.

    Args:
        stop_min: expected duration of the unplanned stop (e.g. the filter replacement median or
            the unit's MTTR for the reason).
        ict_seconds: ideal cycle time of the unit's line.
        degraded_capacity: share of line capacity left while the unit is down (0 for class A).
        service_loss_min: production minutes the planned service itself still takes.
    """
    if stop_min < 0 or service_loss_min < 0 or ict_seconds <= 0:
        raise ValueError("stop_min, service_loss_min must be >= 0 and ict_seconds > 0")
    minutes = max(0.0, stop_min - service_loss_min)
    lost_share = 1.0 - min(1.0, max(0.0, degraded_capacity))
    cars = minutes * 60.0 / ict_seconds * lost_share
    return MaintenanceSaving(minutes=minutes, cars=cars)


@dataclass(frozen=True, slots=True)
class LimitAdvice:
    """Everything AL-M2 needs for one signal of one unit."""

    forecast: LimitForecast
    alert: bool
    """``hours_to_limit`` is known and within the look-ahead."""
    window: datetime | None
    """Recommended service time (nearest shift change before the limit); ``None``: service now."""
    saving: MaintenanceSaving


def limit_advice(
    forecast: LimitForecast,
    *,
    calendar: PlantCalendar,
    lookahead_h: float,
    stop_min: float,
    ict_seconds: float,
    degraded_capacity: float = 0.0,
) -> LimitAdvice:
    """Combine a forecast with the calendar and the saving estimate (SPEC §11.2)."""
    hours = forecast.hours_to_limit
    alert = hours is not None and hours <= lookahead_h
    window = (
        recommend_window(calendar, now=forecast.now, limit_at=forecast.limit_at)
        if forecast.limit_at is not None
        else None
    )
    saving = maintenance_saving(
        stop_min=stop_min, ict_seconds=ict_seconds, degraded_capacity=degraded_capacity
    )
    return LimitAdvice(forecast=forecast, alert=alert, window=window, saving=saving)


def limited_signals(signals: Sequence[Signal]) -> list[Signal]:
    """Signals of an equipment type that have ``limit_hi`` (the ones AL-M2 watches)."""
    return [s for s in signals if s.limit_hi is not None]


def default_stop_minutes(cfg: TwinConfig, equipment_type: str) -> float:
    """Typical unplanned stop of a type, for the saving estimate: the filter replacement for
    booths, the chain-break repair (when the type has one) or the type's repair median."""
    sim = cfg.simulation
    if sim.paint_filters is not None and sim.paint_filters.equipment_type == equipment_type:
        return float(sim.paint_filters.replacement.median)
    failures = sim.failures.get(equipment_type)
    if failures is None:
        return 30.0
    if failures.chain_break is not None:
        return float(failures.chain_break.mttr.median)
    return float(failures.mttr.median)


def unit_limit_advice(
    cfg: TwinConfig,
    equipment: str,
    series: Mapping[str, tuple[Sequence[datetime], Sequence[float]]],
    *,
    now: datetime,
    since: datetime | None = None,
    working: WorkingTime | None = None,
    lookahead_h: float | None = None,
) -> dict[str, LimitAdvice]:
    """AL-M2 advice for every limited signal of a unit that has enough samples in ``series``."""
    eq = cfg.equipment[equipment]
    line = cfg.line_of_equipment(equipment)
    look = lookahead_h or cfg.rules.thresholds.telemetry_limit_lookahead_h
    stop_min = default_stop_minutes(cfg, eq.type)
    out: dict[str, LimitAdvice] = {}
    for signal in limited_signals(cfg.equipment_types[eq.type].signals):
        data = series.get(signal.code)
        if data is None or signal.limit_hi is None:
            continue
        forecast = forecast_limit(
            data[0],
            data[1],
            limit_hi=signal.limit_hi,
            now=now,
            since=since,
            working=working,
            shift_min_jump=SHIFT_RANGE_FRACTION * (signal.hi - signal.lo),
        )
        if forecast is None:
            continue
        out[signal.code] = limit_advice(
            forecast,
            calendar=cfg.calendar,
            lookahead_h=look,
            stop_min=stop_min,
            ict_seconds=line.ict_seconds,
            degraded_capacity=eq.degraded_capacity,
        )
    return out
