"""Threshold overrides stored in ``settings`` (read side, :mod:`twin_core.thresholds`)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from twin_core.db import Setting
from twin_core.thresholds import OVERRIDE_KEYS, overrides_from_rows


async def stored_overrides(
    session: AsyncSession,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Overrides from ``settings`` and their metadata (who/when) per section."""
    rows = (await session.scalars(select(Setting).where(Setting.key.in_(OVERRIDE_KEYS)))).all()
    overrides = overrides_from_rows({r.key: r.value for r in rows})
    meta = {
        r.key: {"updated_by": r.updated_by, "updated_ts": r.updated_ts.isoformat()} for r in rows
    }
    return overrides, meta
