"""M0 service stubs: health endpoints, API app, entry points."""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from qost_api.app import create_app
from twin_core.config import TwinConfig
from twin_core.health import start_health_server


async def http_get(port: int, path: str, method: str = "GET") -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, json.loads(body) if body else {}


async def test_health_server_endpoints() -> None:
    ready = False
    server = await start_health_server("engine", port=0, host="127.0.0.1", ready=lambda: ready)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await http_get(port, "/healthz") == (200, {"status": "ok", "service": "engine"})
        assert (await http_get(port, "/readyz"))[0] == 503
        ready = True
        assert (await http_get(port, "/readyz?x=1"))[0] == 200
        assert (await http_get(port, "/nope"))[0] == 404
        assert (await http_get(port, "/healthz", method="POST"))[0] == 405
        assert await http_get(port, "/healthz", method="HEAD") == (200, {})
    finally:
        server.close()
        await server.wait_closed()


def test_api_health_and_schema(cfg: TwinConfig) -> None:
    with TestClient(create_app(cfg, database_url=None, redis=None)) as client:
        assert client.get("/healthz").json() == {"status": "ok", "service": "api"}
        assert client.get("/readyz").json()["site"] == "KST"
        assert client.get("/api/openapi.json").status_code == 200


def test_api_docs_work_offline(cfg: TwinConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """M4: Swagger UI assets are served locally, so /api/docs is on even with OFFLINE=true."""
    monkeypatch.setenv("OFFLINE", "true")
    with TestClient(create_app(cfg, database_url=None, redis=None)) as client:
        html = client.get("/api/docs").text
        assert "/api/docs/assets/swagger-ui-bundle.js" in html
        assert "http" not in html.split("<body>")[-1].replace("http-equiv", "")


def test_api_loads_config_at_startup(monkeypatch: pytest.MonkeyPatch, config_dir: object) -> None:
    monkeypatch.setenv("PLANT_CONFIG_DIR", str(config_dir))
    with TestClient(create_app(database_url=None, redis=None)) as client:
        assert client.get("/readyz").status_code == 200


@pytest.mark.parametrize(
    ("module", "port"),
    [("qost_collector", 8110), ("qost_engine", 8120), ("qost_notifier", 8130)],
)
def test_stub_entry_points(module: str, port: int) -> None:
    entry = importlib.import_module(f"{module}.__main__")
    assert port == entry.DEFAULT_PORT
    assert callable(entry.main)
