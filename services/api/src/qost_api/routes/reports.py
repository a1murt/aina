"""``/api/v1/reports/shift`` — shift reports (SPEC §11.5, §12.2; roles master, director).

``POST`` builds the report (template or LLM with the number check) and stores it with an audit
entry; ``GET`` lists the stored reports of a shift, newest first. Errors are RFC 7807 problems:
``validation`` 422 (unknown shift code), ``shift-not-started`` 422, ``shift-open`` 409 (the
engine has not closed the shift yet), ``no-shift-data`` 404, ``no-database`` 503.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from qost_api.auth import Principal, require_roles
from qost_api.llm import create_provider
from qost_api.problems import ProblemError
from qost_api.reports.data import DbReportBackend
from qost_api.reports.service import (
    ReportError,
    ReportService,
    ReportView,
    ShiftReportRequest,
)
from twin_core.forecast.result import ForecastResult
from twin_core.report import Lang

REPORT_ROLES = ("master", "director")

router = APIRouter(prefix="/api/v1", tags=["reports"])
Reporter = Annotated[Principal, Depends(require_roles(*REPORT_ROLES))]


def get_report_service(request: Request) -> ReportService:
    """The app's report service, built on first use from ``app.state`` (no lifespan changes)."""
    state = request.app.state
    service: ReportService | None = getattr(state, "report_service", None)
    if service is not None:
        return service
    if getattr(state, "sessionmaker", None) is None:
        raise ProblemError(
            503, "Database is not configured", "DATABASE_URL is not set", slug="no-database"
        )
    forecast_service = getattr(state, "forecast_service", None)

    async def outlook(month: str, principal: Principal) -> ForecastResult | None:
        if forecast_service is None:
            return None
        result: ForecastResult = await forecast_service.outlook(month, principal)
        return result

    service = ReportService(
        state.config,
        state.clock,
        DbReportBackend(state.sessionmaker),
        create_provider(),
        outlook,
    )
    state.report_service = service
    return service


Service = Annotated[ReportService, Depends(get_report_service)]


def _problem(exc: ReportError) -> ProblemError:
    return ProblemError(exc.status, exc.title, exc.detail, slug=exc.slug)


@router.post(
    "/reports/shift",
    status_code=status.HTTP_201_CREATED,
    summary="Generate a shift report (template or LLM with the number check, SPEC §11.5)",
)
async def post_shift_report(
    body: ShiftReportRequest, principal: Reporter, service: Service
) -> ReportView:
    try:
        return await service.create(body, principal)
    except ReportError as exc:
        raise _problem(exc) from exc


@router.get("/reports/shift", summary="Stored reports of a shift, newest first")
async def get_shift_reports(
    principal: Reporter,
    service: Service,
    day: Annotated[date, Query(alias="date", description="shift date, YYYY-MM-DD")],
    shift: Annotated[str, Query(min_length=1, max_length=8)],
    lang: Annotated[Lang | None, Query()] = None,
) -> list[ReportView]:
    try:
        return await service.find(day, shift, lang)
    except ReportError as exc:
        raise _problem(exc) from exc
