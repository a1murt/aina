"""``GET /live/snapshot`` (§12.3) and ``GET /history/timeline`` (state intervals) — all roles."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from qost_api.auth import AnyUser
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, RedisDep, Settings, window
from qost_api.live.snapshot import snapshot
from qost_api.problems import ProblemError

router = APIRouter(prefix="/api/v1", tags=["live"])


@router.get("/live/snapshot", summary="The plant now: clock, lines, equipment, buffers, bottleneck")
async def get_snapshot(
    principal: AnyUser, cfg: Config, clock: PlantClock, redis: RedisDep, settings: Settings
) -> dict[str, Any]:
    return await snapshot(redis, cfg, clock, settings)


_TIMELINE = text(
    """
    SELECT entity, entity_type, start_ts, end_ts, state, reason_code, source
    FROM equipment_state
    WHERE entity = ANY(:entities) AND start_ts < :hi AND (end_ts IS NULL OR end_ts > :lo)
    ORDER BY entity, start_ts
    """
)


@router.get("/history/timeline", summary="State intervals of entities over a period (Gantt)")
async def get_timeline(
    principal: AnyUser,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    entity: Annotated[
        list[str] | None,
        Query(description="line/equipment codes (repeat or comma-separate); default: all lines"),
    ] = None,
    start: Annotated[str | None, Query(alias="from", description="ISO date/datetime")] = None,
    end: Annotated[str | None, Query(alias="to", description="ISO date/datetime")] = None,
) -> dict[str, Any]:
    codes = [c.strip() for item in (entity or []) for c in item.split(",") if c.strip()]
    if not codes:
        codes = list(cfg.flow_lines)
    known = set(cfg.lines) | set(cfg.equipment)
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise ProblemError(
            404, "Not Found", f"unknown entity: {', '.join(unknown)}", slug="not-found"
        )
    lo, hi = window(cfg, clock, start, end, default=timedelta(hours=8), max_span=timedelta(days=31))
    rows = (await session.execute(_TIMELINE, {"entities": codes, "lo": lo, "hi": hi})).mappings()
    out: dict[str, list[dict[str, Any]]] = {c: [] for c in codes}
    for r in rows:
        out[r["entity"]].append(
            {
                "start": r["start_ts"].isoformat(),
                "end": r["end_ts"].isoformat() if r["end_ts"] else None,
                "state": r["state"],
                "reason_code": r["reason_code"],
                "source": r["source"],
            }
        )
    return {
        "from": lo.isoformat(),
        "to": hi.isoformat(),
        "entities": [
            {
                "code": c,
                "entity_type": "line" if c in cfg.lines else "equipment",
                "intervals": out[c],
            }
            for c in codes
        ],
    }
