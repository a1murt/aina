"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from qost_api import __version__
from twin_core.config import TwinConfig
from twin_core.health import load_config_or_exit
from twin_core.settings import TwinSettings

SERVICE = "api"


def create_app(config: TwinConfig | None = None) -> FastAPI:
    """Build the app. Without ``config`` the plant config is loaded at startup (fail fast)."""
    settings = TwinSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config if config is not None else load_config_or_exit(SERVICE)
        yield

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

    @app.get("/healthz", tags=["service"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE}

    @app.get("/readyz", tags=["service"])
    async def readyz() -> dict[str, str]:
        cfg: TwinConfig = app.state.config
        return {"status": "ready", "service": SERVICE, "site": cfg.plant.site.code}

    return app
