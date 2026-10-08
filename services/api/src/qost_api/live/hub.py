"""One Redis subscription per API process, fanned out to the WebSocket clients (SPEC §12.3).

* :class:`LiveHub` reads the ``live`` channel (the engine's envelopes ``{type, ts, data}``) and
  offers every message to every connected :class:`LiveClient`; a ``snapshot`` envelope (the
  engine rebuilt its view after a reset or restart) and a re-established subscription make each
  client re-read and resend the snapshot.
* The hub also emits ``clock`` messages (``data.event = "tick"``: plant time, shift, speed,
  paused) every ``WS_CLOCK_TICK_S`` and immediately when the speed or pause state changes, so the
  UI header follows the demo console.
* :class:`Throttle` (per client): at most one message per type/entity per
  ``WS_MIN_INTERVAL_MS`` (2/s); a burst keeps the first message and delivers the *latest* one at
  the end of the interval (the views are absolute, so dropping intermediate ones loses nothing).
  ``unit`` messages (one per body) are events and are never coalesced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from redis.asyncio import Redis

from qost_api.deps import current_clock_state
from twin_core.clock import Clock, SimClock, format_utc
from twin_core.config import TwinConfig
from twin_core.live import build_snapshot

log = structlog.get_logger("qost_api.live")

LIVE_TYPES = (
    "snapshot",
    "state",
    "unit",
    "buffer",
    "kpi",
    "alert",
    "clock",
    "bottleneck",
    "forecast_progress",
)
"""Envelope types of §12.3 (``subscribe`` accepts these)."""

RESYNC = "__resync__"
"""Source marker: the subscription was (re)established — clients must resynchronise."""


# --------------------------------------------------------------------------- throttle


def throttle_key(env: dict[str, Any]) -> tuple[str, ...]:
    """Coalescing key of an envelope: type + entity (``unit`` messages are never coalesced)."""
    kind = str(env.get("type"))
    raw_data = env.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    if kind == "state":
        if "code" in data:
            return ("state", str(data["code"]))
        for key in ("downtime", "downtime_closed"):
            item = data.get(key)
            if isinstance(item, dict):
                return ("downtime", str(item.get("entity")))
        return ("state", json.dumps(data, sort_keys=True, default=str))
    if kind == "kpi":
        return ("kpi", str(data.get("level")), str(data.get("code")))
    if kind == "buffer":
        return ("buffer", str(data.get("code")))
    if kind == "alert":
        return ("alert", str(data.get("dedup_key") or data.get("id")))
    if kind == "clock":
        return ("clock", str(data.get("event")))
    if kind == "forecast_progress":
        return ("forecast_progress", str(data.get("id")))
    if kind == "unit":
        return ("unit", str(data.get("line")), str(data.get("body_id")), str(env.get("ts")))
    return (kind,)


@dataclass
class Throttle:
    """Per-key rate limit with trailing delivery of the latest message (pure, clock passed in)."""

    interval_s: float
    last_sent: dict[tuple[str, ...], float] = field(default_factory=dict)
    pending: OrderedDict[tuple[str, ...], str] = field(default_factory=OrderedDict)

    def offer(self, key: tuple[str, ...], raw: str, now: float) -> str | None:
        """The message to send now, or ``None`` if it was parked until its slot."""
        if key[0] == "unit":
            return raw
        last = self.last_sent.get(key)
        if key not in self.pending and (last is None or now - last >= self.interval_s):
            self.last_sent[key] = now
            return raw
        self.pending[key] = raw
        self.pending.move_to_end(key)
        return None

    def next_due(self) -> float | None:
        if not self.pending:
            return None
        return min(self.last_sent.get(k, 0.0) + self.interval_s for k in self.pending)

    def due(self, now: float) -> list[str]:
        out: list[str] = []
        for key in list(self.pending):
            if now - self.last_sent.get(key, 0.0) >= self.interval_s:
                out.append(self.pending.pop(key))
                self.last_sent[key] = now
        return out

    def clear(self) -> None:
        self.pending.clear()


# --------------------------------------------------------------------------- clients


class LiveClient:
    """One WebSocket connection: an inbox from the hub, a type filter, a throttle."""

    def __init__(self, *, interval_s: float, queue_size: int) -> None:
        self.inbox: asyncio.Queue[tuple[dict[str, Any], str] | str] = asyncio.Queue(queue_size)
        self.types: set[str] | None = None
        self.throttle = Throttle(interval_s)
        self.dropped = 0

    def wants(self, kind: str) -> bool:
        return self.types is None or kind in self.types

    def offer(self, env: dict[str, Any], raw: str) -> None:
        if not self.wants(str(env.get("type"))):
            return
        try:
            self.inbox.put_nowait((env, raw))
        except asyncio.QueueFull:
            self.dropped += 1
            self.request_resync()

    def request_resync(self) -> None:
        """Ask the sender to resend the snapshot (drops a backlog that the snapshot supersedes)."""
        try:
            self.inbox.put_nowait(RESYNC)
        except asyncio.QueueFull:
            while not self.inbox.empty():
                self.inbox.get_nowait()
            self.inbox.put_nowait(RESYNC)


# --------------------------------------------------------------------------- sources


class LiveSource(Protocol):
    """Envelopes of the ``live`` channel (``RESYNC`` after every (re)subscription)."""

    def messages(self) -> AsyncIterator[str]: ...


class RedisLiveSource:
    """Redis pub/sub on the ``live`` channel with reconnection."""

    def __init__(self, redis: Redis, channel: str, *, retry_s: float = 1.0) -> None:
        self.redis = redis
        self.channel = channel
        self.retry_s = retry_s

    async def messages(self) -> AsyncIterator[str]:
        while True:
            pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(self.channel)
                yield RESYNC
                while True:
                    msg = await pubsub.get_message(timeout=1.0)
                    if msg is None or msg.get("type") != "message":
                        continue
                    data = msg["data"]
                    yield data.decode() if isinstance(data, bytes) else str(data)
            except (OSError, ConnectionError) as exc:
                log.warning("live_subscription_lost", error=str(exc)[:200])
                await asyncio.sleep(self.retry_s)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]


# --------------------------------------------------------------------------- hub


class LiveHub:
    def __init__(
        self,
        source: LiveSource,
        *,
        cfg: TwinConfig,
        clock: Clock,
        mode: str,
        clock_tick_s: float,
        interval_s: float,
        queue_size: int,
        wall: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.cfg = cfg
        self.clock = clock
        self.mode = mode
        self.clock_tick_s = clock_tick_s
        self.interval_s = interval_s
        self.queue_size = queue_size
        self.wall = wall
        self.clients: set[LiveClient] = set()
        self.received = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._subscribed_once = False

    # -- lifecycle
    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._consume(), name="live-hub"),
                asyncio.create_task(self._clock_ticks(), name="live-clock"),
            ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

    def register(self) -> LiveClient:
        client = LiveClient(interval_s=self.interval_s, queue_size=self.queue_size)
        self.clients.add(client)
        return client

    def unregister(self, client: LiveClient) -> None:
        self.clients.discard(client)

    # -- fan-out
    def dispatch(self, raw: str) -> None:
        if raw == RESYNC:
            if self._subscribed_once:
                for client in self.clients:
                    client.request_resync()
            self._subscribed_once = True
            return
        try:
            env = json.loads(raw)
        except ValueError:
            log.warning("live_bad_message", raw=raw[:200])
            return
        if not isinstance(env, dict) or "type" not in env:
            return
        self.received += 1
        if env["type"] == "snapshot":
            for client in self.clients:
                client.request_resync()
            return
        for client in self.clients:
            client.offer(env, raw)

    async def _consume(self) -> None:
        while True:
            try:
                async for raw in self.source.messages():
                    self.dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the hub alive whatever the source does
                log.warning("live_source_failed", error=str(exc)[:200])
            await asyncio.sleep(1.0)

    # -- clock
    def clock_envelope(self) -> str | None:
        state = current_clock_state(self.clock)
        if state is None:
            return None
        clock = build_snapshot({}, self.cfg, state, mode=self.mode)["clock"] or {}
        data = {"event": "tick", **clock, "paused": state.paused, "sim_mode": state.sim_mode}
        return json.dumps(
            {"type": "clock", "ts": format_utc(state.plant_time), "data": data},
            ensure_ascii=False,
        )

    async def _clock_ticks(self) -> None:
        last_key: tuple[Any, ...] | None = None
        last_sent = 0.0
        while True:
            await asyncio.sleep(0.25)
            if not self.clients:
                continue
            state = self.clock.state if isinstance(self.clock, SimClock) else None
            key = (state.speed, state.paused, state.sim_mode) if state else None
            now = self.wall()
            if key == last_key and now - last_sent < self.clock_tick_s:
                continue
            raw = self.clock_envelope()
            if raw is None:
                continue
            last_key, last_sent = key, now
            env = json.loads(raw)
            for client in self.clients:
                client.offer(env, raw)
