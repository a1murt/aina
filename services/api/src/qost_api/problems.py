"""RFC 7807 error responses (``application/problem+json``, SPEC §12.1)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from twin_core.clock import ClockNotReadyError
from twin_core.importer import ImportFormatError

PROBLEM_JSON = "application/problem+json"
_TYPE_BASE = "/problems/"


class ProblemError(Exception):
    """Raise to answer with a problem document."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        slug: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.slug = slug
        self.extensions = extensions


def problem_response(
    request: Request,
    status: int,
    title: str,
    detail: str | None = None,
    *,
    slug: str | None = None,
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{_TYPE_BASE}{slug}" if slug else "about:blank",
        "title": title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    body["instance"] = request.url.path
    body.update(extensions)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(
            request, exc.status, exc.title, exc.detail, slug=exc.slug, **exc.extensions
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        title = _TITLES.get(exc.status_code, "Error")
        return problem_response(
            request, exc.status_code, title, detail, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return problem_response(
            request, 422, "Request validation failed", slug="validation", errors=errors
        )

    @app.exception_handler(ImportFormatError)
    async def _import(request: Request, exc: ImportFormatError) -> JSONResponse:
        return problem_response(
            request,
            422,
            "File cannot be imported",
            str(exc),
            slug="import-format",
            problems=exc.problems,
        )

    @app.exception_handler(ClockNotReadyError)
    async def _clock(request: Request, exc: ClockNotReadyError) -> JSONResponse:
        return problem_response(
            request, 503, "Plant clock is not available", str(exc), slug="clock-not-ready"
        )


_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    503: "Service Unavailable",
}
