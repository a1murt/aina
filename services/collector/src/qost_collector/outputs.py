"""Outputs of the collector: database (``DbSink``) and Redis Stream ``events``, each behind its
own spool (FR-ING-02/03).

An output writes batches directly while its target is healthy. On a failure (or when its queue is
full) the batch goes to the spool; while the spool holds anything, newer batches are spooled too,
so the target always receives batches in their original order. A drainer re-sends the spool and
then switches back to direct writes. Repeats are safe: the database ignores known ``event_id``s
and the engine drops duplicate ids from the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from redis.asyncio import Redis

from qost_collector.spool import Spool
from twin_core.db.sink import DbSink
from twin_core.events import AnyEvent, dumps, parse_event

log = structlog.get_logger("qost_collector.outputs")


@dataclass(frozen=True, slots=True)
class Batch:
    events: tuple[AnyEvent, ...]
    jsons: tuple[str, ...]

    @classmethod
    def of(cls, events: Sequence[AnyEvent]) -> Batch:
        return cls(tuple(events), tuple(dumps(e) for e in events))

    def line(self) -> str:
        return "[" + ",".join(self.jsons) + "]"

    @classmethod
    def from_line(cls, line: str) -> Batch:
        items = json.loads(line)
        events = [parse_event(item) for item in items]
        return cls(tuple(events), tuple(json.dumps(item, ensure_ascii=False) for item in items))


class Target(Protocol):
    name: str

    async def write(self, batch: Batch) -> None: ...

    async def recover(self) -> None: ...


class DbTarget:
    name = "db"

    def __init__(self, sink: DbSink) -> None:
        self.sink = sink

    async def write(self, batch: Batch) -> None:
        await self.sink.write_batch(batch.events)

    async def recover(self) -> None:
        await self.sink.reset_pool()


class StreamTarget:
    name = "stream"

    def __init__(self, redis: Redis, stream: str, maxlen: int) -> None:
        self.redis = redis
        self.stream = stream
        self.maxlen = maxlen
        self.written = 0

    async def write(self, batch: Batch) -> None:
        pipe = self.redis.pipeline(transaction=False)
        for event, raw in zip(batch.events, batch.jsons, strict=True):
            pipe.xadd(
                self.stream,
                {"k": event.kind, "e": event.entity, "j": raw},
                maxlen=self.maxlen,
                approximate=True,
            )
        await pipe.execute()
        self.written += len(batch.events)

    async def recover(self) -> None:
        with contextlib.suppress(Exception):
            await self.redis.connection_pool.disconnect()


class SpooledOutput:
    def __init__(self, target: Target, spool: Spool, *, max_queue: int = 100) -> None:
        self.target = target
        self.spool = spool
        self.queue: asyncio.Queue[Batch] = asyncio.Queue(maxsize=max_queue)
        self.written_events = 0
        self.spooled_batches = 0
        self.failures = 0
        self.healthy = True
        self.last_error: str | None = None
        self.last_write_wall = 0.0
        self._wake = asyncio.Event()

    @property
    def name(self) -> str:
        return self.target.name

    def submit(self, batch: Batch) -> None:
        """Hand a batch over without blocking the sources."""
        if not self.spool.empty() or self.queue.full():
            self._to_spool(batch)
        else:
            self.queue.put_nowait(batch)
        self._wake.set()

    def _to_spool(self, batch: Batch) -> None:
        self.spool.append(batch.line())
        self.spooled_batches += 1

    @property
    def backlog(self) -> int:
        return self.queue.qsize()

    async def _write(self, batch: Batch) -> bool:
        try:
            await self.target.write(batch)
        except Exception as exc:
            self.failures += 1
            self.healthy = False
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("output_failed", output=self.name, error=self.last_error)
            with contextlib.suppress(Exception):
                await self.target.recover()
            return False
        self.healthy = True
        self.written_events += len(batch.events)
        self.last_write_wall = time.monotonic()
        return True

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 0.2
        while not stop.is_set():
            if not self.spool.empty():
                # keep order: everything queued now goes behind the spooled batches
                while not self.queue.empty():
                    self._to_spool(self.queue.get_nowait())
                head = self.spool.peek()
                if head is None:
                    await asyncio.sleep(0.05)
                    continue
                cursor, line = head
                if await self._write(Batch.from_line(line)):
                    self.spool.ack(cursor)
                    backoff = 0.2
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                continue
            try:
                batch = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            if not await self._write(batch):
                self._to_spool(batch)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    def stats(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "written": self.written_events,
            "queued": self.queue.qsize(),
            "spooled_batches": self.spooled_batches,
            "spool_bytes": self.spool.size_bytes,
            "spool_dropped_segments": self.spool.dropped_segments,
            "failures": self.failures,
            "last_error": self.last_error,
        }


__all__ = ["Batch", "DbTarget", "SpooledOutput", "StreamTarget", "Target"]
