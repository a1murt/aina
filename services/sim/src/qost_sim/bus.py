"""Redis side of the simulator: plant clock, persisted run state, reset handshake.

Reset handshake (proposed to M3, see docs/plans/M2-plan.md §7): the simulator publishes
``{"action": "reset", "epoch": N, "demo_start": "...Z"}`` on ``sim:control``; a listener (the
engine) deletes the live tail (ts >= demo_start) from its tables and the ``events`` stream,
reloads its state and answers ``{"epoch": N}`` on ``sim:control:ack``. With no subscriber the
simulator does not wait; without an ack it gives up after a timeout and continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any, Literal, Protocol

from redis.asyncio import Redis

ResetOutcome = Literal["acked", "no_listeners", "timeout", "error"]


class SimBus(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def broadcast_reset(
        self, channel: str, message: dict[str, Any], *, timeout_s: float
    ) -> ResetOutcome: ...

    async def aclose(self) -> None: ...


class RedisBus:
    def __init__(self, url: str) -> None:
        self.redis: Redis = Redis.from_url(url)

    async def get(self, key: str) -> bytes | str | None:
        value: bytes | str | None = await self.redis.get(key)
        return value

    async def set(self, key: str, value: str) -> None:
        await self.redis.set(key, value)

    async def broadcast_reset(
        self, channel: str, message: dict[str, Any], *, timeout_s: float
    ) -> ResetOutcome:
        pubsub = self.redis.pubsub()
        ack_channel = f"{channel}:ack"
        try:
            await pubsub.subscribe(ack_channel)
            receivers = await self.redis.publish(channel, json.dumps(message))
            if not receivers:
                return "no_listeners"
            async with asyncio.timeout(timeout_s):
                while True:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is None:
                        continue
                    with contextlib.suppress(ValueError, TypeError, KeyError):
                        if json.loads(msg["data"])["epoch"] == message["epoch"]:
                            return "acked"
        except TimeoutError:
            return "timeout"
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(ack_channel)
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def aclose(self) -> None:
        await self.redis.aclose()


class MemoryBus:
    """In-process bus for tests; ``on_reset`` plays the engine (returns True to ack)."""

    def __init__(self, on_reset: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.data: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.resets: list[dict[str, Any]] = []
        self.on_reset = on_reset

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value
        self.writes.append((key, value))

    async def broadcast_reset(
        self, channel: str, message: dict[str, Any], *, timeout_s: float
    ) -> ResetOutcome:
        self.resets.append(message)
        if self.on_reset is None:
            return "no_listeners"
        return "acked" if self.on_reset(message) else "timeout"

    async def aclose(self) -> None:
        return None
