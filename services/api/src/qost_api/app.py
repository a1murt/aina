"""FastAPI application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from qost_api import __version__
from qost_api.db import make_engine, make_sessionmaker
from qost_api.forecast.data import DbForecastBackend, ForecastBackend
from qost_api.forecast.service import ForecastService
from qost_api.problems import install_problem_handlers
from qost_api.routes import forecast, imports
from qost_api.routes import reports as reports_routes
from qost_api.settings import ApiSettings
from twin_core.clock import Clock, SimClock, create_clock
from twin_core.config import TwinConfig
from twin_core.health import load_config_or_exit

SERVICE = "api"
_UNSET = object()


def create_app(
    config: TwinConfig | None = None,
    *,
    clock: Clock | None = None,
    database_url: str | object | None = _UNSET,
    forecast_backend: ForecastBackend | None = None,
) -> FastAPI:
    """Build the app.

    Without ``config`` the plant config is loaded at startup (fail fast); without ``clock`` it
    follows ``CLOCK_MODE``; ``database_url`` defaults to ``DATABASE_URL`` (``None`` = no DB);
    ``forecast_backend`` replaces the database as the forecast's data source (tests).
    """
    settings = ApiSettings()
    db_url = settings.database_url if database_url is _UNSET else database_url

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log = structlog.get_logger().bind(service=SERVICE)
        app.state.settings = settings
        app.state.config = config if config is not None else load_config_or_exit(SERVICE)
        app.state.clock = clock if clock is not None else create_clock(settings)
        engine = make_engine(db_url) if isinstance(db_url, str) and db_url else None
        app.state.sessionmaker = make_sessionmaker(engine) if engine is not None else None
        backend: ForecastBackend | None = forecast_backend
        if backend is None and app.state.sessionmaker is not None:
            backend = DbForecastBackend(app.state.sessionmaker)
        app.state.forecast_service = (
            ForecastService(app.state.config, app.state.clock, backend)
            if backend is not None
            else None
        )
        if settings.dev_auth_default_role:
            # DEV ONLY until M4 (JWT): requests without X-Dev-Role act as this role.
            log.warning("dev_auth_enabled", default_role=settings.dev_auth_default_role)
        async with contextlib.AsyncExitStack() as stack:
            if isinstance(app.state.clock, SimClock) and clock is None:
                await stack.enter_async_context(app.state.clock)
            try:
                yield
            finally:
                if engine is not None:
                    await engine.dispose()

    app = FastAPI(
        title="Qost Twin API",
        version=__version__,
        lifespan=lifespan,
        # Swagger UI pulls its assets from a CDN; with OFFLINE=true only the schema is served
        # until the assets are vendored (stage M4).
        docs_url=None if settings.offline else "/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    install_problem_handlers(app)
    app.include_router(imports.router)
    app.include_router(forecast.router)
    app.include_router(reports_routes.router)

    @app.get("/healthz", tags=["service"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE}

    @app.get("/readyz", tags=["service"])
    async def readyz() -> dict[str, str]:
        cfg: TwinConfig = app.state.config
        return {"status": "ready", "service": SERVICE, "site": cfg.plant.site.code}

    return app
