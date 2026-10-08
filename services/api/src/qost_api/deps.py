"""Shared request dependencies: config, clock, settings, Redis, live keys, pagination."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from redis.asyncio import Redis

from qost_api.problems import ProblemError
from qost_api.settings import ApiSettings
from twin_core.clock import (
    Clock,
    ClockNotReadyError,
    ClockState,
    SimClock,
    ensure_utc,
    system_now,
)
from twin_core.config import TwinConfig
from twin_core.live import LiveKeys


def get_config(request: Request) -> TwinConfig:
    config: TwinConfig = request.app.state.config
    return config


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_settings(request: Request) -> ApiSettings:
    settings: ApiSettings = request.app.state.settings
    return settings


def get_redis(request: Request) -> Redis:
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        raise ProblemError(
            503, "Live store is not configured", "Redis is disabled", slug="no-redis"
        )
    return redis


def live_keys(settings: ApiSettings) -> LiveKeys:
    return LiveKeys(settings.live_prefix, settings.live_channel)


Config = Annotated[TwinConfig, Depends(get_config)]
PlantClock = Annotated[Clock, Depends(get_clock)]
Settings = Annotated[ApiSettings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def current_clock_state(clock: Clock) -> ClockState | None:
    """The plant clock at this instant as a :class:`ClockState` (sim: the last Redis value
    extrapolated to now; other clocks: "now" at their speed); ``None`` before the first value."""
    if isinstance(clock, SimClock):
        state = clock.state
        if state is None:
            return None
        return replace(state, plant_time=clock.now(), wall_ts=system_now())
    try:
        now = clock.now()
    except ClockNotReadyError:
        return None
    return ClockState(plant_time=now, wall_ts=now, speed=clock.speed or 1.0)


clock_state = current_clock_state


# --------------------------------------------------------------------------- periods


def local_day_bounds(cfg: TwinConfig, first: date, last: date) -> tuple[datetime, datetime]:
    """UTC bounds of plant-local days ``first`` .. ``last`` (inclusive)."""
    tz = cfg.timezone
    lo = datetime.combine(first, time.min, tzinfo=tz)
    hi = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz)
    return ensure_utc(lo), ensure_utc(hi)


def parse_instant(value: str, cfg: TwinConfig, *, end: bool = False) -> datetime:
    """ISO datetime (with zone) or a plant-local date (``end``: the end of that day)."""
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            lo, hi = local_day_bounds(cfg, day, day)
            return hi if end else lo
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProblemError(
            422,
            "Request validation failed",
            f"'{value}' is not an ISO date or datetime",
            slug="validation",
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=cfg.timezone)
    return ensure_utc(moment)


def parse_day(value: str | None, cfg: TwinConfig) -> date | None:
    """Plant-local date of an ISO date/datetime (``None`` passes through)."""
    if value is None:
        return None
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return parse_instant(value, cfg).astimezone(cfg.timezone).date()


def window(
    cfg: TwinConfig,
    clock: Clock,
    start: str | None,
    end: str | None,
    *,
    default: timedelta,
    max_span: timedelta | None = None,
) -> tuple[datetime, datetime]:
    """``[from, to)`` in UTC: defaults to ``now - default .. now``; validates the order/span."""
    hi = parse_instant(end, cfg, end=True) if end else clock.now()
    lo = parse_instant(start, cfg) if start else hi - default
    if hi <= lo:
        raise ProblemError(
            422, "Request validation failed", "'from' must be before 'to'", slug="validation"
        )
    if max_span is not None and hi - lo > max_span:
        raise ProblemError(
            422,
            "Request validation failed",
            f"the period is longer than {max_span.days} days",
            slug="validation",
        )
    return lo, hi


# --------------------------------------------------------------------------- pagination


@dataclass(frozen=True, slots=True)
class Page:
    limit: int
    after: tuple[str, int] | None
    """Keyset position ``(sort key ISO, id)`` of the last item of the previous page."""


def encode_cursor(sort_key: str, row_id: int) -> str:
    raw = json.dumps([sort_key, row_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(key), int(row_id)
    except (ValueError, TypeError):
        raise ProblemError(
            422, "Request validation failed", "invalid cursor", slug="validation"
        ) from None


def page_params(
    limit: Annotated[int, Query(ge=1, le=500, description="page size")] = 100,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page")] = None,
) -> Page:
    return Page(limit, decode_cursor(cursor) if cursor else None)


PageDep = Annotated[Page, Depends(page_params)]


def page_body(
    items: list[dict[str, Any]], page: Page, key: str, *, id_key: str = "id"
) -> dict[str, Any]:
    """``{"items", "next_cursor"}`` from ``limit + 1`` fetched rows sorted by (key, id) desc."""
    more = len(items) > page.limit
    items = items[: page.limit]
    next_cursor = (
        encode_cursor(str(items[-1][key]), int(items[-1][id_key])) if more and items else None
    )
    return {"items": items, "next_cursor": next_cursor}
