"""Minimal ``/healthz`` + ``/readyz`` HTTP endpoint and a stub-service runner.

Stage M0 ships every service as a stub: it validates the plant config at start (FR-DOM-01:
invalid config = refuse to start with a readable message) and answers health checks so that
Docker Compose ``depends_on: service_healthy`` works. Real services replace the stub later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable

from twin_core.config import ConfigError, TwinConfig, load_config_from_settings
from twin_core.log import configure_logging
from twin_core.settings import TwinSettings

_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}
_READ_TIMEOUT_S = 5.0
_MAX_HEADER_LINES = 100


async def start_health_server(
    service: str,
    *,
    port: int,
    host: str = "0.0.0.0",
    ready: Callable[[], bool] | None = None,
    stats: Callable[[], dict[str, object]] | None = None,
) -> asyncio.Server:
    """Serve ``GET /healthz`` (liveness), ``GET /readyz`` (readiness) and, when ``stats`` is
    given, ``GET /stats`` (service counters) as JSON."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                request_line = await reader.readline()
                for _ in range(_MAX_HEADER_LINES):
                    if (await reader.readline()) in (b"\r\n", b"\n", b""):
                        break
            parts = request_line.decode("latin-1").split()
            method, path = (parts[0], parts[1].split("?", 1)[0]) if len(parts) >= 2 else ("", "")
            payload: dict[str, object]
            if method not in ("GET", "HEAD"):
                status, payload = 405, {"error": "method not allowed"}
            elif path == "/healthz":
                status, payload = 200, {"status": "ok", "service": service}
            elif path == "/readyz":
                is_ready = ready() if ready is not None else True
                status = 200 if is_ready else 503
                payload = {"status": "ready" if is_ready else "not ready", "service": service}
            elif path == "/stats" and stats is not None:
                status, payload = 200, {"service": service, **stats()}
            else:
                status, payload = 404, {"error": "not found"}
            body = json.dumps(payload, default=str).encode()
            head = (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            writer.write(head if method == "HEAD" else head + body)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


def load_config_or_exit(service: str) -> TwinConfig:
    """Load the plant config; on error print every problem and exit with code 2."""
    try:
        return load_config_from_settings(TwinSettings())
    except ConfigError as exc:
        print(f"[{service}] refusing to start:\n{exc.render()}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None


async def run_stub(service: str, *, default_port: int) -> None:
    """Run a stub service: validate config, serve health endpoints until SIGTERM/SIGINT."""
    log = configure_logging(service)
    config = load_config_or_exit(service)
    port = int(os.environ.get("HEALTH_PORT", default_port))
    server = await start_health_server(service, port=port)
    log.info("stub_started", port=port, config=repr(config), stage="M0")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()
    await server.wait_closed()
    log.info("stub_stopped")


def stub_main(service: str, *, default_port: int) -> None:
    """Synchronous entry point for ``python -m qost_<service>``."""
    asyncio.run(run_stub(service, default_port=default_port))
