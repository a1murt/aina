"""FastAPI application factory (SPEC §12).

Routers come from :data:`qost_api.routes.ROUTERS`. At startup the app loads the plant config
(fail fast), starts the plant clock, opens the database pool, syncs the reference tables from
YAML in the background (§5.1), connects Redis and starts the live hub for ``/ws/live``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from qost_api import __version__
from qost_api.db import make_engine, make_sessionmaker
from qost_api.forecast.data import DbForecastBackend, ForecastBackend
from qost_api.forecast.service import ForecastService
from qost_api.live.hub import LiveHub, LiveSource, RedisLiveSource
from qost_api.openapi import OPENAPI_URL, install_docs, install_openapi
from qost_api.problems import install_problem_handlers
from qost_api.refsync import sync_reference
from qost_api.routes import ROUTERS
from qost_api.settings import DEFAULT_JWT_SECRET, ApiSettings
from twin_core.clock import Clock, SimClock, create_clock
from twin_core.config import TwinConfig
from twin_core.health import load_config_or_exit

SERVICE = "api"
_UNSET: Any = object()

DESCRIPTION = """Aina — REST + WebSocket API (SPEC §12).

Log in with `POST /api/v1/auth/login` and send `Authorization: Bearer <token>`; the WebSocket
is `/ws/live?token=<token>`. Errors are RFC 7807 problems; time is ISO 8601 UTC; lists page with
`limit` + `cursor`. Each operation lists its roles (`x-roles`).
"""


async def _sync_reference_later(app: FastAPI, log: Any) -> None:
    """Mirror YAML reference data into SQL; retried while the database is not reachable."""
    delay = 1.0
    for attempt in range(1, 8):
        try:
            async with app.state.sessionmaker() as session, session.begin():
                counts = await sync_reference(session, app.state.config)
            log.info("reference_synced", **counts)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("reference_sync_failed", attempt=attempt, error=str(exc)[:200])
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def create_app(
    config: TwinConfig | None = None,
    *,
    clock: Clock | None = None,
    database_url: str | None = _UNSET,
    forecast_backend: ForecastBackend | None = None,
    redis: Redis | None = _UNSET,
    live_source: LiveSource | None = None,
    sim_transport: Any = None,
    sync_reference_on_start: bool = True,
) -> FastAPI:
    """Build the app.

    Without ``config`` the plant config is loaded at startup (fail fast); without ``clock`` it
    follows ``CLOCK_MODE``; ``database_url`` defaults to ``DATABASE_URL`` (``None`` = no DB);
    ``forecast_backend`` replaces the database as the forecast's data source (tests);
    ``redis`` defaults to ``REDIS_URL`` (``None`` = no Redis: live endpoints answer 503);
    ``live_source`` replaces the Redis subscription of the WebSocket hub (tests);
    ``sim_transport`` is an ``httpx2`` transport for the demo-console proxy (tests).
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
        if settings.jwt_secret.get_secret_value() == DEFAULT_JWT_SECRET:
            log.warning("jwt_default_secret", hint="set JWT_SECRET in .env outside the demo")
        own_redis = redis is _UNSET
        app.state.redis = (
            (Redis.from_url(settings.redis_url) if settings.redis_enabled else None)
            if own_redis
            else redis
        )
        app.state.sim_transport = sim_transport
        source = live_source
        if source is None and app.state.redis is not None:
            source = RedisLiveSource(app.state.redis, settings.live_channel)
        app.state.live_hub = (
            LiveHub(
                source,
                cfg=app.state.config,
                clock=app.state.clock,
                mode="sim" if settings.clock_mode == "sim" else "system",
                clock_tick_s=settings.ws_clock_tick_s,
                interval_s=settings.ws_min_interval_ms / 1000.0,
                queue_size=settings.ws_queue_size,
            )
            if source is not None
            else None
        )
        async with contextlib.AsyncExitStack() as stack:
            if isinstance(app.state.clock, SimClock) and clock is None:
                await stack.enter_async_context(app.state.clock)
            sync_task = (
                asyncio.create_task(_sync_reference_later(app, log), name="reference-sync")
                if sync_reference_on_start and app.state.sessionmaker is not None
                else None
            )
            try:
                yield
            finally:
                if sync_task is not None:
                    sync_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sync_task
                if app.state.live_hub is not None:
                    await app.state.live_hub.stop()
                if own_redis and app.state.redis is not None:
                    await app.state.redis.aclose()
                if engine is not None:
                    await engine.dispose()

    app = FastAPI(
        title="Aina API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
    )
    install_problem_handlers(app)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Location"],
        )

    @app.middleware("http")
    async def trace(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """NFR-07: one ``trace_id`` per request in every log line and the response header."""
        trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(trace_id=trace_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")
        response.headers["X-Request-ID"] = trace_id
        return response

    for router in ROUTERS:
        app.include_router(router)
    install_openapi(app)
    install_docs(app)
    return app
