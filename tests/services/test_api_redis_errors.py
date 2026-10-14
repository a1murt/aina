"""STAB-3: redis-py errors (not ``OSError``) on the best-effort live part of a route do not
fail the request — the route answers from the database."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, cast

from redis.exceptions import ConnectionError as RedisConnectionError

import qost_api.app  # noqa: F401  (route modules after the app: no import cycle)
from api_support import FakeSession
from qost_api.routes import kpi as kpi_routes
from qost_api.settings import ApiSettings
from twin_core.config import TwinConfig


class BrokenRedis:
    async def get(self, *_: Any) -> Any:
        raise RedisConnectionError("redis is restarting")

    async def hget(self, *_: Any) -> Any:
        raise RedisConnectionError("redis is restarting")

    async def hgetall(self, *_: Any) -> Any:
        raise RedisConnectionError("redis is restarting")

    def pipeline(self, *_: Any, **__: Any) -> Any:
        raise RedisConnectionError("redis is restarting")


async def test_kpi_live_part_survives_a_redis_error(cfg: TwinConfig) -> None:
    assert not issubclass(RedisConnectionError, OSError)  # why the old except missed it
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=BrokenRedis(), settings=ApiSettings()))
    )
    records = await kpi_routes._records(
        cast(Any, request),
        FakeSession(),
        cfg,
        list(cfg.flow_lines),
        date(2026, 10, 16),
        date(2026, 10, 16),
        include_live=True,
    )
    assert records == []
