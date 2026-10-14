"""Warm-up of the director's forecast results (DEMO-12).

With the simulator clock, the API watches plant time once a second: when the plant is paused, a
reset is detected (plant time goes back) or the API has just started, the levers and the default
effect are computed in the background (after ``forecast_warm_settle_s`` for a reset, while the
engine rebuilds). A director window opened or reloaded afterwards gets them from the cache
instead of waiting 5–8 s.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from qost_api.auth import SYSTEM_PRINCIPAL
from qost_api.forecast.service import ForecastService
from twin_core.clock import Clock

POLL_S = 1.0
RETRY_S = 5.0
"""First delay before a failed warm-up is retried; doubles on each further failure."""
MAX_RETRIES = 4

log = structlog.get_logger("qost_api.forecast")


async def warm_loop(
    service: ForecastService,
    clock: Clock,
    *,
    settle_s: float,
    poll_s: float = POLL_S,
    retry_s: float = RETRY_S,
    max_retries: int = MAX_RETRIES,
) -> None:
    """Run until cancelled; never raises. A failed warm-up (e.g. the database is still busy
    right after ``make demo``) is retried up to ``max_retries`` times with doubling delay."""
    loop = asyncio.get_running_loop()
    paused_before: bool | None = None
    due: float | None = loop.time() + settle_s  # first warm-up after start
    retries = 0
    while True:
        try:
            now = clock.now()
            state: Any = getattr(clock, "state", None)
            paused = bool(getattr(state, "paused", False))
            if service.observe(now):
                due, retries = loop.time() + settle_s, 0
            if paused and paused_before is False:
                due, retries = loop.time(), 0
            paused_before = paused
            if due is not None and loop.time() >= due:
                due = None
                try:
                    durations = await service.warm(SYSTEM_PRINCIPAL)
                except Exception:
                    if retries < max_retries:
                        due = loop.time() + retry_s * 2**retries
                        retries += 1
                    raise
                retries = 0
                log.info("forecast_warmed", epoch=service.epoch, paused=paused, **durations)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # clock not ready, database busy, month closed, ...
            log.warning(
                "forecast_warm_failed",
                error=f"{type(exc).__name__}: {exc}"[:200],
                retry=due is not None,
            )
        await asyncio.sleep(poll_s)
