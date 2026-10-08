"""``GET /healthz`` (liveness), ``GET /readyz`` (database, Redis, plant clock) — public (NFR-07)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from twin_core.clock import SimClock

SERVICE = "api"
router = APIRouter(tags=["service"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE}


async def _check(coro: Any) -> str:
    try:
        async with asyncio.timeout(2.0):
            await coro
    except Exception as exc:
        return f"error: {str(exc)[:120] or type(exc).__name__}"
    return "ok"


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, str] = {"config": "ok"}
    sessions = getattr(state, "sessionmaker", None)
    if sessions is not None:

        async def db() -> None:
            async with sessions() as session:
                await session.execute(text("SELECT 1"))

        checks["database"] = await _check(db())
    else:
        checks["database"] = "not configured"
    redis = getattr(state, "redis", None)
    checks["redis"] = await _check(redis.ping()) if redis is not None else "not configured"
    clock = getattr(state, "clock", None)
    if isinstance(clock, SimClock):
        checks["plant_clock"] = "ok" if clock.state is not None else "waiting for the simulator"
    ready = all(v in ("ok", "not configured") for k, v in checks.items() if k != "plant_clock")
    body = {
        "status": "ready" if ready else "not ready",
        "service": SERVICE,
        "site": state.config.plant.site.code,
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
