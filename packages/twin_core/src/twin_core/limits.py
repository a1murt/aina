"""Time until a signal reaches its limit (SPEC §11.2, AL-M2).

For signals with ``limit_hi`` (filter ΔP, chain elongation, vibration): a Theil–Sen line over the
last ``window_h`` hours (6 h by SPEC) → the level "now" and the hours until ``limit_hi``. If that is
within ``rules.yaml: thresholds.telemetry_limit_lookahead_h``, AL-M2 recommends servicing at the
nearest shift change before the limit (calendar from :mod:`twin_core.calendar`) and estimates the
minutes and cars saved compared with an unplanned stop during production.

Pure functions: "now" is a parameter (the caller takes it from :class:`twin_core.clock.Clock`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from twin_core.calendar import PlantCalendar
from twin_core.clock import ensure_utc
from twin_core.config.plant import Signal
from twin_core.stats import theil_sen

FIT_WINDOW_H = 6.0
"""SPEC §11.2: robust regression over the last 6 hours."""
MIN_POINTS = 6
"""Fewer samples in the window → no forecast (a gap in data must not raise an alarm)."""
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


def forecast_limit(
    ts: Sequence[datetime],
    values: Sequence[float],
    *,
    limit_hi: float,
    now: datetime,
    window_h: float = FIT_WINDOW_H,
    min_points: int = MIN_POINTS,
    since: datetime | None = None,
) -> LimitForecast | None:
    """Forecast when ``values`` reach ``limit_hi`` using samples in ``[now - window_h, now]``.

    ``since`` cuts the window further, e.g. at the end of the last filter replacement or
    maintenance: the trend restarts there. Returns ``None`` with fewer than ``min_points`` finite
    samples in the window.
    """
    if len(ts) != len(values):
        raise ValueError("ts and values must have the same length")
    moment = ensure_utc(now)
    lo = moment - timedelta(hours=window_h)
    if since is not None:
        lo = max(lo, ensure_utc(since))
    xs: list[float] = []
    ys: list[float] = []
    for instant, value in zip(ts, values, strict=True):
        t = ensure_utc(instant)
        if lo <= t <= moment and math.isfinite(value):
            xs.append((t - moment).total_seconds() / _SECONDS_PER_HOUR)
            ys.append(float(value))
    if len(xs) < min_points:
        return None
    line = theil_sen(xs, ys)
    if line is None:
        return None
    level = line.intercept  # x = 0 is "now"
    hours: float | None
    if level >= limit_hi:
        hours = 0.0
    elif line.slope > 0:
        hours = (limit_hi - level) / line.slope
    else:
        hours = None
    limit_at = moment + timedelta(hours=hours) if hours is not None else None
    return LimitForecast(
        limit=limit_hi,
        level_now=level,
        slope_per_h=line.slope,
        hours_to_limit=hours,
        limit_at=limit_at,
        n_points=line.n,
        now=moment,
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
