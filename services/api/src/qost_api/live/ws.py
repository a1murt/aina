"""``WS /ws/live?token=<JWT>`` (SPEC §12.3), all roles.

Protocol:

* server → client: envelopes ``{"type", "ts", "data"}``. The first message is always
  ``snapshot`` (the ``GET /live/snapshot`` body); after that deltas ``state | unit | buffer |
  kpi | alert | clock | bottleneck | forecast_progress`` as published by the engine (and by the
  API for alert actions), at most 2 per second per type/entity. A new ``snapshot`` is sent after
  the engine rebuilt its view (reset/restart), after a lost Redis subscription and when the
  client fell behind; a reconnecting client always starts with a snapshot.
* client → server: ``{"subscribe": ["state", "alert"]}`` (``null`` or ``["*"]`` = everything) →
  ``{"type": "subscribed", "data": {"types": [...]}}``; ``{"resync": true}`` → a fresh snapshot;
  ``{"ping": …}`` → ``{"type": "pong"}``.
* close codes: 4401 — missing/invalid/expired token, 4403 — unknown role, 1011 — the live store
  is not available.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from qost_api.auth import decode_token
from qost_api.live.hub import LIVE_TYPES, RESYNC, LiveClient, LiveHub, throttle_key
from qost_api.live.snapshot import snapshot
from qost_api.problems import ProblemError
from twin_core.clock import format_utc, system_now

router = APIRouter(tags=["live"])
log = structlog.get_logger("qost_api.ws")


def _env(kind: str, data: Any) -> str:
    return json.dumps(
        {"type": kind, "ts": format_utc(system_now()), "data": data},
        ensure_ascii=False,
        default=str,
    )


async def _send_snapshot(ws: WebSocket) -> None:
    app = ws.app
    body = await snapshot(app.state.redis, app.state.config, app.state.clock, app.state.settings)
    await ws.send_text(_env("snapshot", body))


async def _sender(ws: WebSocket, client: LiveClient) -> None:
    await _send_snapshot(ws)
    throttle = client.throttle
    while True:
        due_at = throttle.next_due()
        timeout = None if due_at is None else max(due_at - time.monotonic(), 0.0)
        item: tuple[dict[str, Any], str] | str | None
        try:
            item = await asyncio.wait_for(client.inbox.get(), timeout)
        except TimeoutError:
            item = None
        if item == RESYNC:
            throttle.clear()
            await _send_snapshot(ws)
            continue
        if isinstance(item, tuple):
            env, raw = item
            if client.wants(str(env.get("type"))):
                now_raw = throttle.offer(throttle_key(env), raw, time.monotonic())
                if now_raw is not None:
                    await ws.send_text(now_raw)
        for raw in throttle.due(time.monotonic()):
            await ws.send_text(raw)


def _subscribe(client: LiveClient, value: Any) -> str:
    if value is None or value == "*" or (isinstance(value, list) and "*" in value):
        client.types = None
        return _env("subscribed", {"types": list(LIVE_TYPES)})
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return _env("error", {"detail": "subscribe expects a list of message types"})
    unknown = [v for v in value if v not in LIVE_TYPES]
    if unknown:
        return _env("error", {"detail": f"unknown types {unknown}", "known": list(LIVE_TYPES)})
    client.types = set(value)
    # drop parked messages of types the client no longer wants
    for key in list(client.throttle.pending):
        kind = "state" if key[0] == "downtime" else key[0]
        if kind not in client.types:
            client.throttle.pending.pop(key, None)
    return _env("subscribed", {"types": sorted(client.types)})


async def live_socket(ws: WebSocket, token: str | None = None) -> None:
    await ws.accept()
    app = ws.app
    try:
        if not token:
            raise ProblemError(401, "Unauthorized", "token is required: /ws/live?token=<JWT>")
        principal = decode_token(app.state.settings, app.state.config, token)
    except ProblemError as exc:
        await ws.close(
            code=4401 if exc.status == 401 else 4403, reason=(exc.detail or exc.title)[:120]
        )
        return
    hub: LiveHub | None = getattr(app.state, "live_hub", None)
    if hub is None or getattr(app.state, "redis", None) is None:
        await ws.close(code=1011, reason="live store is not available")
        return
    hub.start()
    client = hub.register()
    sender = asyncio.create_task(_sender(ws, client), name=f"ws-{principal.username}")
    log.info("ws_connected", user=principal.username, role=principal.role, clients=len(hub.clients))
    try:
        while True:
            receive = asyncio.create_task(ws.receive_text())
            done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                receive.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await receive
                error = sender.exception() if not sender.cancelled() else None
                if error is not None and not isinstance(error, WebSocketDisconnect):
                    log.warning("ws_sender_failed", error=str(error)[:200])
                break
            text = receive.result()
            try:
                msg = json.loads(text)
            except ValueError:
                await ws.send_text(_env("error", {"detail": "messages must be JSON"}))
                continue
            if not isinstance(msg, dict):
                continue
            if "subscribe" in msg:
                await ws.send_text(_subscribe(client, msg["subscribe"]))
            if msg.get("resync"):
                client.request_resync()
            if "ping" in msg:
                await ws.send_text(_env("pong", {"ping": msg["ping"]}))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unregister(client)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
        if ws.client_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await ws.close()
        log.info("ws_closed", user=principal.username, dropped=client.dropped)


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket, token: str | None = None) -> None:
    await live_socket(websocket, token)


@router.websocket("/api/v1/ws/live")
async def ws_live_v1(websocket: WebSocket, token: str | None = None) -> None:
    """Alias under the REST prefix (for proxies that forward only ``/api``)."""
    await live_socket(websocket, token)
