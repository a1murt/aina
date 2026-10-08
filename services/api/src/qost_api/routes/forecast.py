"""``/api/v1/forecast``, ``/api/v1/calibration``, ``/api/v1/effect`` (SPEC §10, §12.2).

Roles: forecast and levers — director, admin; calibration — director, admin, maintenance;
effect — director, admin. Errors are RFC 7807 problems (``validation`` 422 with ``errors``,
``forecast-month`` 422, ``not-implemented`` 501 for ``mode: des``, ``not-found`` 404,
``no-database`` 503).
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, Request, status

from qost_api.auth import Principal, require_roles
from qost_api.forecast.service import (
    CalibrationView,
    EffectRequest,
    ForecastError,
    ForecastRequest,
    ForecastRunView,
    ForecastService,
)
from qost_api.plan_risk import record_plan_risk
from qost_api.problems import ProblemError
from twin_core.config.plant import Month
from twin_core.forecast.effect import EffectResult
from twin_core.forecast.levers import LeversResult

FORECAST_ROLES = ("director", "admin")
CALIBRATION_ROLES = ("director", "admin", "maintenance")
EFFECT_ROLES = ("director", "admin")

router = APIRouter(prefix="/api/v1", tags=["forecast"])
log = structlog.get_logger("qost_api.forecast")
Forecaster = Annotated[Principal, Depends(require_roles(*FORECAST_ROLES))]
Calibrator = Annotated[Principal, Depends(require_roles(*CALIBRATION_ROLES))]
Economist = Annotated[Principal, Depends(require_roles(*EFFECT_ROLES))]


def get_service(request: Request) -> ForecastService:
    service: ForecastService | None = getattr(request.app.state, "forecast_service", None)
    if service is None:
        raise ProblemError(
            503, "Database is not configured", "DATABASE_URL is not set", slug="no-database"
        )
    return service


Service = Annotated[ForecastService, Depends(get_service)]


def _problem(exc: ForecastError) -> ProblemError:
    if exc.errors is not None:
        return ProblemError(exc.status, exc.title, exc.detail, slug=exc.slug, errors=exc.errors)
    return ProblemError(exc.status, exc.title, exc.detail, slug=exc.slug)


@router.post(
    "/forecast",
    status_code=status.HTTP_201_CREATED,
    summary="Month forecast, fast Monte Carlo with what-if overrides (FR-FC-01/02)",
)
async def post_forecast(
    body: ForecastRequest, principal: Forecaster, service: Service, request: Request
) -> ForecastRunView:
    try:
        view = await service.forecast(body, principal)
    except ForecastError as exc:
        raise _problem(exc) from exc
    try:  # AL-P1 on the baseline of the current month (M4); never fails the forecast
        await record_plan_risk(
            request.app,
            view.month,
            view.overrides,
            view.result.model_dump(mode="json") if view.result is not None else None,
        )
    except Exception as exc:
        log.warning("plan_risk_failed", error=str(exc)[:200])
    return view


@router.get("/forecast/levers", summary="Levers with Δ cars, Δ P and effect (SPEC §10.4)")
async def get_levers(
    principal: Forecaster,
    service: Service,
    month: Annotated[Month | None, Query(description="YYYY-MM; default: current month")] = None,
    n_runs: Annotated[int | None, Query(ge=1)] = None,
) -> LeversResult:
    try:
        return await service.levers(month, principal, n_runs=n_runs)
    except ForecastError as exc:
        raise _problem(exc) from exc


@router.get("/forecast/{run_id}", summary="A stored forecast run")
async def get_forecast(run_id: int, principal: Forecaster, service: Service) -> ForecastRunView:
    view = await service.get(run_id)
    if view is None:
        raise ProblemError(404, "Not Found", f"forecast {run_id} does not exist", slug="not-found")
    return view


@router.get("/calibration", summary="Model parameters from history (SPEC §10.1)")
async def get_calibration(principal: Calibrator, service: Service) -> CalibrationView:
    snapshot = await service.calibration(principal)
    return CalibrationView(
        id=snapshot.id, ts=snapshot.ts, window_days=snapshot.window_days, params=snapshot.params
    )


@router.post("/effect", summary="Economic effect of a scenario (SPEC §10.5)")
async def post_effect(body: EffectRequest, principal: Economist, service: Service) -> EffectResult:
    try:
        return await service.effect(body, principal)
    except ForecastError as exc:
        raise _problem(exc) from exc
