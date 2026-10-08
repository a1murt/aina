"""Plan read model (SPEC §5.8): output MTD, daily output and the monthly targets.

:func:`mtd_output` is *the* month-to-date output: finished cars = units leaving the last flow
line (QC-1) with ``pass`` or ``rework_pass`` (:data:`twin_core.events.FINISHED_RESULTS`) since the
plant-local month start. The forecast (M6, ``qost_api.forecast.data.load_plant_state``) and
``GET /plan/progress`` both use it, so the director sees one number.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.db import ProductionPlan
from twin_core.events import FINISHED_RESULTS
from twin_core.forecast.calibration import month_bounds

_MTD = text(
    """
    SELECT COUNT(*) FROM unit_event
    WHERE line = :line AND result = ANY(:results) AND ts >= :m0 AND ts < :as_of
    """
)
_DAILY = text(
    """
    SELECT (ts AT TIME ZONE :tz)::date AS day, COUNT(*) AS n FROM unit_event
    WHERE line = :line AND result = ANY(:results) AND ts >= :m0 AND ts < :hi
    GROUP BY 1 ORDER BY 1
    """
)


async def mtd_output(session: AsyncSession, cfg: TwinConfig, *, as_of: datetime) -> int:
    """Finished cars from the start of the plant-local month of ``as_of`` up to ``as_of``."""
    now = ensure_utc(as_of)
    local = now.astimezone(cfg.timezone)
    m0, _ = month_bounds(cfg, f"{local.year:04d}-{local.month:02d}")
    result = await session.execute(
        _MTD,
        {"line": cfg.flow_lines[-1], "results": sorted(FINISHED_RESULTS), "m0": m0, "as_of": now},
    )
    return int(result.scalar_one())


async def daily_output(
    session: AsyncSession, cfg: TwinConfig, month: str, *, as_of: datetime
) -> dict[date, int]:
    """Finished cars per plant-local day of ``month`` (up to ``as_of``)."""
    m0, m1 = month_bounds(cfg, month)
    hi = min(m1, ensure_utc(as_of))
    if hi <= m0:
        return {}
    rows = await session.execute(
        _DAILY,
        {
            "tz": cfg.plant.site.timezone,
            "line": cfg.flow_lines[-1],
            "results": sorted(FINISHED_RESULTS),
            "m0": m0,
            "hi": hi,
        },
    )
    return {r[0]: int(r[1]) for r in rows.all()}


async def plan_rows(
    session: AsyncSession, month: str
) -> list[tuple[str, str | None, str | None, int]]:
    """``production_plan`` rows ``(level, line, product, qty)`` of ``month``."""
    rows = await session.execute(
        select(
            ProductionPlan.level, ProductionPlan.line, ProductionPlan.product, ProductionPlan.qty
        ).where(ProductionPlan.month == month)
    )
    return [(r[0], r[1], r[2], int(r[3])) for r in rows.all()]
