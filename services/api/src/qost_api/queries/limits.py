"""AL-M2 limit forecasts of a unit computed on request (health endpoint, copilot).

Same function and data as the engine (:func:`twin_core.limits.unit_limit_advice` on the working
time axis, the trend restarts after the unit's last long stop), read from ``telemetry`` and
``downtime``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from twin_core.alert_text import limit_text_ru
from twin_core.calendar import WorkingTime
from twin_core.config import TwinConfig
from twin_core.limits import limited_signals, unit_limit_advice

HISTORY = timedelta(days=5)

_SERIES = text(
    "SELECT signal, ts, value FROM telemetry WHERE equipment = :code AND signal = ANY(:signals) "
    "AND ts > :lo AND ts <= :now AND quality <> 'bad' ORDER BY ts"
)
_LAST_STOP = text(
    "SELECT max(end_ts) FROM downtime WHERE entity = :code AND import_id IS NULL "
    "AND end_ts IS NOT NULL AND end_ts <= :now AND duration_s >= :min_s"
)


async def limit_forecasts(
    session: AsyncSession, cfg: TwinConfig, code: str, now: datetime
) -> list[dict[str, Any]]:
    """One entry per limited signal of the unit with enough samples (empty for other units)."""
    eq = cfg.equipment[code]
    signals = {s.code: s for s in limited_signals(cfg.equipment_types[eq.type].signals)}
    if not signals:
        return []
    series: dict[str, tuple[list[datetime], list[float]]] = {c: ([], []) for c in signals}
    rows = await session.execute(
        _SERIES, {"code": code, "signals": list(signals), "lo": now - HISTORY, "now": now}
    )
    for signal, ts, value in rows.all():
        series[signal][0].append(ts)
        series[signal][1].append(float(value))
    since = (
        await session.execute(
            _LAST_STOP,
            {"code": code, "now": now, "min_s": cfg.rules.thresholds.microstop_threshold_s},
        )
    ).scalar()
    advice = unit_limit_advice(
        cfg, code, series, now=now, since=since, working=WorkingTime(cfg.calendar, now)
    )
    out: list[dict[str, Any]] = []
    for signal, adv in advice.items():
        f, s = adv.forecast, signals[signal]
        value = {
            "signal": signal,
            "signal_name_ru": s.name_ru,
            "unit": s.unit,
            "limit": f.limit,
            "level_now": round(f.level_now, 2),
            "slope_per_h": round(f.slope_per_h, 3),
            "hours_to_limit": None if f.hours_to_limit is None else round(f.hours_to_limit, 1),
            "limit_at": f.limit_at.isoformat() if f.limit_at else None,
            "window": adv.window.isoformat() if adv.window else None,
            "saving_min": round(adv.saving.minutes, 1),
            "saving_cars": round(adv.saving.cars, 1),
        }
        out.append(
            {
                **value,
                "alert": adv.alert,
                "n_points": f.n_points,
                "level_shifts": f.level_shifts,
                "text_ru": limit_text_ru(cfg, code, value) if adv.alert else None,
            }
        )
    return out
