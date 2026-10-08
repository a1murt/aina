"""Integration harness for M3: own test database, isolated Redis (DB 15 + prefixed channels),
unique MQTT topic root and free ports; sim, collector and engine run as real processes.

Never touches the shared stack's data: the database is ``<TEST_DATABASE_URL db>_it_m3`` and the
Redis keys live in DB 15 (pub/sub channels are global, hence the prefix).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sim_support import free_port
from support import CONFIG_DIR, REPO_ROOT

BASE_URL = make_url(
    os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://qost:qost@localhost:5432/qost")
)
REDIS_BASE = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0")
REDIS_URL = REDIS_BASE.rsplit("/", 1)[0] + "/15"
MQTT_HOST = os.environ.get("TEST_MQTT_HOST", "localhost")

DATA_TABLES = (
    "event_raw",
    "telemetry",
    "unit_event",
    "buffer_level",
    "ckd_stock",
    "defect",
    "equipment_state",
    "downtime",
    "kpi_shift",
    "bottleneck_shift",
    "alert_notification",
    "alert",
    "dq_issue",
    "engine_checkpoint",
    "audit_log",
)


async def _admin(sql: str) -> None:
    engine = create_async_engine(BASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def create_database(suffix: str) -> URL:
    """Drop, create and migrate ``<base>_<suffix>`` (sync; for module fixtures)."""
    name = f"{BASE_URL.database}_{suffix}"
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = BASE_URL.set(database=name)
    config = Config(str(REPO_ROOT / "services/api/alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return url


def drop_database(url: URL) -> None:
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))


async def query(url: URL, sql: str, **params: Any) -> list[dict[str, Any]]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings()] if result.returns_rows else []
    finally:
        await engine.dispose()


async def truncate(url: URL) -> None:
    await query(url, f"TRUNCATE {', '.join(DATA_TABLES)} CASCADE")


def url_string(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@dataclass
class Stack:
    """sim + collector + engine as processes, isolated per test run."""

    db_url: URL
    tmp: Path
    speed: float = 60.0
    collector_db_url: str | None = None
    engine_db_url: str | None = None
    prefix: str = field(default_factory=lambda: f"it{uuid.uuid4().hex[:6]}")
    procs: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("opcua", "http", "sim_health", "collector", "engine"):
            self.ports[name] = free_port()
        self.topic_root = f"{self.prefix}/qost/v1/KST"
        self.control = f"{self.prefix}:sim:control"
        self.channel = f"{self.prefix}:live"
        self.redis = Redis.from_url(REDIS_URL)

    # ------------------------------------------------------------------ processes

    def env(self, name: str) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SIM_", "COLLECTOR_", "ENGINE_"))
        }
        endpoint = f"opc.tcp://127.0.0.1:{self.ports['opcua']}/qost/"
        env.update(
            {
                "PLANT_CONFIG_DIR": str(CONFIG_DIR),
                "PLANT_TAG_MAP": str(CONFIG_DIR / "tag_map.demo.yaml"),
                "DATABASE_URL": url_string(self.db_url),
                "REDIS_URL": REDIS_URL,
                "MQTT_URL": f"mqtt://{MQTT_HOST}:1883",
                "CLOCK_MODE": "sim",
                "SIM_CONTROL_CHANNEL": self.control,
                "LIVE_CHANNEL": self.channel,
                "OPCUA_ENDPOINT": endpoint,
                "PYTHONUNBUFFERED": "1",
            }
        )
        if name == "sim":
            env.update(
                {
                    "SIM_OPCUA_BIND": endpoint,
                    "SIM_HTTP_HOST": "127.0.0.1",
                    "SIM_HTTP_PORT": str(self.ports["http"]),
                    "SIM_MQTT_TOPIC_ROOT": self.topic_root,
                    "SIM_RESUME": "false",
                    "SIM_AUTOSTART": "false",
                    "SIM_RESET_ACK_TIMEOUT_S": "20",
                    "HEALTH_PORT": str(self.ports["sim_health"]),
                }
            )
        elif name == "collector":
            env.update(
                {
                    "COLLECTOR_TOPIC_ROOT": self.topic_root,
                    "COLLECTOR_SPOOL_DIR": str(self.tmp / "spool"),
                    "HEALTH_PORT": str(self.ports["collector"]),
                }
            )
            if self.collector_db_url:
                env["DATABASE_URL"] = self.collector_db_url
        elif name == "engine":
            env.update({"HEALTH_PORT": str(self.ports["engine"]), "ENGINE_COMMIT_MS": "500"})
            if self.engine_db_url:
                env["DATABASE_URL"] = self.engine_db_url
        return env

    def spawn(self, name: str) -> None:
        module = {"sim": "qost_sim", "collector": "qost_collector", "engine": "qost_engine"}[name]
        with (self.tmp / f"{name}.log").open("ab") as log:
            self.procs[name] = subprocess.Popen(
                [sys.executable, "-m", module],
                env=self.env(name),
                cwd=REPO_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        proc = self.procs[name]

        def _reap() -> None:
            if proc.poll() is None:
                proc.kill()

        atexit.register(_reap)

    def kill(self, name: str, sig: int = signal.SIGTERM) -> None:
        proc = self.procs.pop(name, None)
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    async def start(self) -> None:
        await self.redis.flushdb()
        self.spawn("sim")
        await self.wait_http(f"http://127.0.0.1:{self.ports['http']}/readyz", 60)
        self.spawn("collector")
        self.spawn("engine")
        await self.wait_http(f"http://127.0.0.1:{self.ports['engine']}/readyz", 60)
        await self.wait_http(f"http://127.0.0.1:{self.ports['collector']}/readyz", 60)

    async def stop(self) -> None:
        for name in ("sim", "collector", "engine"):
            self.kill(name)
        await self.redis.aclose()

    def log(self, name: str) -> str:
        path = self.tmp / f"{name}.log"
        return path.read_text(errors="replace")[-4000:] if path.exists() else ""

    # ------------------------------------------------------------------ http

    async def wait_http(self, url: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        async with httpx2.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                with contextlib.suppress(httpx2.HTTPError):
                    if (await client.get(url)).status_code == 200:
                        return
                for name, proc in self.procs.items():
                    if proc.poll() is not None:
                        raise RuntimeError(f"{name} exited:\n{self.log(name)}")
                await asyncio.sleep(0.2)
        raise TimeoutError(f"{url} not ready; logs:\n" + "\n".join(self.log(n) for n in self.procs))

    async def sim(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        async with httpx2.AsyncClient(timeout=60.0) as client:
            url = f"http://127.0.0.1:{self.ports['http']}{path}"
            response = await client.request(method, url, json=body)
            response.raise_for_status()
            return response.json()

    async def stats(self, name: str) -> dict[str, Any]:
        async with httpx2.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"http://127.0.0.1:{self.ports[name]}/stats")
            data: dict[str, Any] = response.json()
            return data

    async def run_plant(self, speed: float) -> None:
        await self.sim("POST", "/speed", {"value": speed})
        await self.sim("POST", "/start")

    async def caught_up(self, timeout_s: float = 60.0) -> dict[str, Any]:
        """Wait until the collector has written everything and the engine has committed it."""
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            col = await self.stats("collector")
            eng = await self.stats("engine")
            outputs = col["outputs"]
            stream_len = await self.redis.xlen("events")
            last = {"collector": col, "engine": eng, "stream": stream_len}
            if (
                col["pending"] == 0
                and all(o["queued"] == 0 and o["spool_bytes"] == 0 for o in outputs.values())
                and outputs["db"]["written"] >= col["emitted"]
                and eng["pending_ids"] == 0
                and eng["pending_ops"] == 0
                and eng["checkpoint_id"] == eng["last_id"]
            ):
                return last
            await asyncio.sleep(0.25)
        raise TimeoutError(f"stack did not catch up: {json.dumps(last, default=str)[:2000]}")


class TcpProxy:
    """Asyncio TCP proxy to the database; ``cut()`` drops connections and refuses new ones."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.port = free_port()
        self.server: asyncio.Server | None = None
        self.conns: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.target)
        except OSError:
            writer.close()
            return
        self.conns.update({writer, up_writer})
        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        self.conns.discard(writer)
        self.conns.discard(up_writer)

    async def cut(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()  # stop accepting
        for w in list(
            self.conns
        ):  # drop live connections (before wait_closed: 3.12 waits for them)
            with contextlib.suppress(Exception):
                w.transport.abort()
        self.conns.clear()
        if server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=5.0)

    async def restore(self) -> None:
        await self.start()

    async def close(self) -> None:
        await self.cut()
