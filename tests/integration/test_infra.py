"""Infrastructure from docker-compose.yml (``make up``): TimescaleDB, Redis, Mosquitto.

Run with ``make test`` (starts the stack). Host-side URLs default to localhost and can be
overridden with TEST_DATABASE_URL / TEST_REDIS_URL / TEST_MQTT_HOST.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from support import REPO_ROOT
from twin_core.clock import ClockState, RedisKV, SimClock, publish_clock_state

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://qost:qost@localhost:5432/qost"
)
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0")
MQTT_HOST = os.environ.get("TEST_MQTT_HOST", "localhost")


def test_alembic_upgrade_enables_timescaledb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    config = Config(str(REPO_ROOT / "services/api/alembic.ini"))
    command.upgrade(config, "head")

    async def query() -> tuple[str | None, str | None]:
        engine = create_async_engine(DATABASE_URL)
        async with engine.connect() as conn:
            ext = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            )
            rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        await engine.dispose()
        return ext, rev

    extversion, revision = asyncio.run(query())
    assert extversion is not None
    assert revision == "0001"


async def test_sim_clock_over_real_redis() -> None:
    client = Redis.from_url(REDIS_URL)
    kv = RedisKV(client)
    key = "test:plant:clock"
    plant_time = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
    wall = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    try:
        await publish_clock_state(kv, ClockState(plant_time, wall, speed=60.0), key=key)
        clock = SimClock(kv, key=key, wall=lambda: wall + timedelta(seconds=2))
        state = await clock.wait_ready(timeout_s=2)
        assert state.speed == 60.0
        assert clock.now() == plant_time + timedelta(minutes=2)
    finally:
        await client.delete(key)
        await client.aclose()


def test_mosquitto_accepts_anonymous_mqtt_connect() -> None:
    # MQTT 3.1.1 CONNECT: protocol "MQTT", level 4, clean session, keep-alive 60, client id "m0".
    connect = (
        bytes([0x10, 0x0E, 0x00, 0x04]) + b"MQTT" + bytes([0x04, 0x02, 0x00, 0x3C, 0x00, 0x02])
    )
    connect += b"m0"
    with socket.create_connection((MQTT_HOST, 1883), timeout=5) as sock:
        sock.sendall(connect)
        connack = sock.recv(4)
    assert connack == bytes([0x20, 0x02, 0x00, 0x00]), connack.hex()
