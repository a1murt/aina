"""``GET /kpi``, ``GET /kpi/losses``, ``GET /plan/progress`` — all roles (§5.4, §5.8, §12.2)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from redis.asyncio import Redis

from qost_api.auth import AnyUser
from qost_api.db import Session
from qost_api.deps import Config, PlantClock, live_keys, parse_day
from qost_api.forecast.data import load_targets
from qost_api.problems import ProblemError
from qost_api.queries.kpi import (
    Granularity,
    Level,
    LineShift,
    aggregate_rows,
    equipment_rows,
    group_lines,
    line_shifts,
    live_line_shifts,
)
from qost_api.queries.losses import loss_inputs, loss_tree_view
from qost_api.queries.plan import daily_output, mtd_output, plan_rows
from twin_core.clock import Clock
from twin_core.config import TwinConfig
from twin_core.config.plant import Month
from twin_core.forecast.calibration import month_bounds, month_of
from twin_core.live import read_views
from twin_core.plan import line_plan_by_product, plan_progress, plan_progress_json

router = APIRouter(prefix="/api/v1", tags=["kpi"])

MAX_DAYS = 400


def _period(
    cfg: TwinConfig, clock: Clock, start: str | None, end: str | None
) -> tuple[date, date, datetime]:
    now = clock.now()
    today = now.astimezone(cfg.timezone).date()
    d1 = parse_day(end, cfg) or today
    d0 = parse_day(start, cfg) or d1.replace(day=1)
    if d1 < d0:
        raise ProblemError(
            422, "Request validation failed", "'from' is after 'to'", slug="validation"
        )
    if (d1 - d0).days > MAX_DAYS:
        raise ProblemError(
            422,
            "Request validation failed",
            f"the period exceeds {MAX_DAYS} days",
            slug="validation",
        )
    return d0, d1, now


def _check_code(cfg: TwinConfig, level: Level, code: str | None) -> None:
    if code is None:
        return
    known: dict[str, Any] = {
        "line": cfg.lines,
        "area": {a.code: a for a in cfg.plant.areas if a.lines},
        "plant": {cfg.plant.site.code: None},
        "equipment": cfg.equipment,
    }
    if code not in known[level]:
        raise ProblemError(
            404,
            "Not Found",
            f"unknown {level} '{code}' (known: {', '.join(known[level])})",
            slug="not-found",
        )


async def _records(
    request: Request,
    session: Any,
    cfg: TwinConfig,
    lines: list[str],
    d0: date,
    d1: date,
    *,
    include_live: bool,
) -> list[LineShift]:
    records = await line_shifts(session, lines, d0, d1)
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if include_live and redis is not None:
        try:
            views = await read_views(redis, live_keys(request.app.state.settings))
        except (OSError, ConnectionError):  # the live part is best effort
            views = {}
        known = {(r.line, r.day, r.shift) for r in records}
        records += live_line_shifts(views.get("lines") or {}, lines, d0, d1, known)
    return records


@router.get("/kpi", summary="KPIs by level and period (ISO 22400, §5.4)")
async def get_kpi(
    principal: AnyUser,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    level: Annotated[Level, Query(description="line | area | plant | equipment")] = "line",
    code: Annotated[
        str | None, Query(description="entity code; default: every entity of the level")
    ] = None,
    start: Annotated[
        str | None, Query(alias="from", description="plant-local date (default: month start)")
    ] = None,
    end: Annotated[
        str | None, Query(alias="to", description="plant-local date, inclusive (default: today)")
    ] = None,
    granularity: Annotated[Granularity, Query(description="shift | day | month")] = "day",
    include_live: Annotated[
        bool, Query(description="add the running shift from the live view")
    ] = True,
) -> dict[str, Any]:
    _check_code(cfg, level, code)
    d0, d1, now = _period(cfg, clock, start, end)
    if level == "equipment":
        codes = [code] if code else list(cfg.equipment)
        shifts = [s for s in cfg.calendar.materialize(d0, d1) if s.working]
        items = await equipment_rows(session, cfg, codes, shifts, now, granularity)
    else:
        groups = group_lines(level, cfg, code)
        lines = sorted({line for ls in groups.values() for line in ls})
        records = await _records(request, session, cfg, lines, d0, d1, include_live=include_live)
        items = aggregate_rows(records, groups, level, granularity)
    return {
        "level": level,
        "code": code,
        "from": d0.isoformat(),
        "to": d1.isoformat(),
        "granularity": granularity,
        "items": items,
    }


@router.get("/kpi/losses", summary="Loss tree: minutes, cars and ₸ by category (FR-KPI-05)")
async def get_losses(
    principal: AnyUser,
    request: Request,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    level: Annotated[Literal["plant", "area", "line"], Query()] = "plant",
    code: Annotated[str | None, Query(description="area or line code (level area/line)")] = None,
    start: Annotated[
        str | None, Query(alias="from", description="plant-local date (default: month start)")
    ] = None,
    end: Annotated[
        str | None, Query(alias="to", description="plant-local date, inclusive (default: today)")
    ] = None,
    include_live: bool = True,
) -> dict[str, Any]:
    _check_code(cfg, level, code)
    d0, d1, now = _period(cfg, clock, start, end)
    groups = group_lines(level, cfg, code)
    lines = sorted({line for ls in groups.values() for line in ls}, key=list(cfg.flow_lines).index)
    records = await _records(request, session, cfg, lines, d0, d1, include_live=include_live)
    flow, shares = await loss_inputs(session, cfg, records, now)
    body = loss_tree_view(cfg, records, flow, shares, lines)
    return {"level": level, "code": code, "from": d0.isoformat(), "to": d1.isoformat(), **body}


@router.get(
    "/plan/progress", summary="Target, line plan, output MTD, fulfilment, required rate (§5.8)"
)
async def get_plan_progress(
    principal: AnyUser,
    cfg: Config,
    clock: PlantClock,
    session: Session,
    month: Annotated[Month | None, Query(description="YYYY-MM; default: current month")] = None,
) -> dict[str, Any]:
    now = clock.now()
    current = month_of(cfg, now)
    month = month or current
    as_of = now if month == current else _month_end_or_start(cfg, month, now)
    mtd = await mtd_output(session, cfg, as_of=as_of) if as_of > _month_start(cfg, month) else 0
    targets = await load_targets(session, cfg, month)
    rows = await plan_rows(session, month)
    progress = plan_progress(
        cfg,
        month,
        as_of=as_of,
        mtd_output=mtd,
        plant_target=targets.plant_target,
        line_plan=targets.line_plan,
        line_plan_by_product=line_plan_by_product(cfg, month, rows),
        daily_output=await daily_output(session, cfg, month, as_of=as_of),
    )
    body = plan_progress_json(progress)
    body["targets_source"] = targets.source
    body["output_line"] = cfg.flow_lines[-1]
    return body


def _month_start(cfg: TwinConfig, month: str) -> datetime:
    return month_bounds(cfg, month)[0]


def _month_end_or_start(cfg: TwinConfig, month: str, now: datetime) -> datetime:
    """A past month is evaluated at its end, a future one at its start."""
    m0, m1 = month_bounds(cfg, month)
    return m1 if m1 <= now else m0
