"""``GET/POST /api/v1/sim/*`` — proxy to the virtual plant console (§6.9); admin, demo profile.

Only the console's own endpoints are forwarded (``status``, ``scenarios`` — GET; ``start``,
``pause``, ``speed``, ``inject``, ``reset`` — POST). Without ``SIM_CONTROL_URL`` (profile
``prod``) the proxy answers 503 ``sim-unavailable``. Every POST is audited (``sim.<action>``).
Errors of the console are passed through as RFC 7807 problems with its status code.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx2
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from qost_api.audit import audit
from qost_api.auth import Principal, require_roles
from qost_api.deps import PlantClock
from qost_api.problems import ProblemError
from twin_core.clock import ClockNotReadyError

router = APIRouter(prefix="/api/v1/sim", tags=["demo"])
Admin = Annotated[Principal, Depends(require_roles("admin"))]
log = structlog.get_logger("qost_api.sim")

GET_PATHS = frozenset({"status", "scenarios"})
POST_PATHS = frozenset({"start", "pause", "speed", "inject", "reset"})


async def _forward(request: Request, method: str, path: str, body: Any) -> tuple[int, Any]:
    settings = request.app.state.settings
    if not settings.sim_control_url:
        raise ProblemError(
            503,
            "Virtual plant console is not available",
            "SIM_CONTROL_URL is not set (the demo console exists only in the demo profile)",
            slug="sim-unavailable",
        )
    transport = getattr(request.app.state, "sim_transport", None)
    async with httpx2.AsyncClient(
        base_url=settings.sim_control_url,
        timeout=settings.sim_control_timeout_s,
        transport=transport,
    ) as client:
        try:
            response = await client.request(method, f"/{path}", json=body)
        except httpx2.HTTPError as exc:
            raise ProblemError(
                502, "Bad Gateway", f"virtual plant console: {exc}", slug="sim-unreachable"
            ) from None
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text[:500]}
    return response.status_code, payload


def _relay_error(status: int, payload: Any) -> ProblemError:
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    text = detail if isinstance(detail, str) else None
    extra = {} if isinstance(detail, str) or detail is None else {"problems": detail}
    return ProblemError(status, "Virtual plant console refused", text, slug="sim-error", **extra)


@router.get("/{path}", summary="Console status / scenarios (proxy)")
async def sim_get(path: str, principal: Admin, request: Request) -> JSONResponse:
    if path not in GET_PATHS:
        raise ProblemError(404, "Not Found", f"unknown console endpoint '{path}'", slug="not-found")
    status, payload = await _forward(request, "GET", path, None)
    if status >= 400:
        raise _relay_error(status, payload)
    return JSONResponse(payload, status_code=status)


@router.post("/{path}", summary="Console actions: start, pause, speed, inject, reset (proxy)")
async def sim_post(
    path: str,
    principal: Admin,
    request: Request,
    clock: PlantClock,
) -> JSONResponse:
    if path not in POST_PATHS:
        raise ProblemError(404, "Not Found", f"unknown console endpoint '{path}'", slug="not-found")
    raw = await request.body()
    body: Any = None
    if raw:
        try:
            body = await request.json()
        except ValueError:
            raise ProblemError(
                422, "Request validation failed", "body must be JSON", slug="validation"
            ) from None
    status, payload = await _forward(request, "POST", path, body)
    if status >= 400:
        raise _relay_error(status, payload)
    sessions = request.app.state.sessionmaker
    try:
        ts = clock.now()
    except ClockNotReadyError:  # no plant clock yet (sim starting): logged, not audited
        ts = None
    if ts is not None and sessions is not None:
        async with sessions() as session:
            audit(
                session,
                ts=ts,
                principal=principal,
                action=f"sim.{path}",
                entity_type="sim",
                entity_id=path,
                after={"request": body, "status": status},
            )
            await session.commit()
    log.info("sim_action", action=path, by=principal.username, status=status)
    return JSONResponse(payload, status_code=status)
