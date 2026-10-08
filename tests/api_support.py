"""Test helpers for the API: bearer tokens, a fake Redis, a fake live source, a fake session."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import queue
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

from qost_api.auth import Principal, issue_token
from qost_api.live.hub import RESYNC
from qost_api.settings import ApiSettings

PROBLEM = "application/problem+json"
NOW = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)
"""09:30 plant time on Demo Day (shift A)."""


def token(
    role: str,
    *,
    user_id: int | None = None,
    username: str | None = None,
    lines: tuple[str, ...] = (),
    now: datetime | None = None,
) -> str:
    """A JWT signed with the app's settings (``JWT_SECRET`` of the environment)."""
    principal = Principal(
        username=username or role, role=role, user_id=user_id, display_name=role, lines=lines
    )
    return issue_token(ApiSettings(), principal, now=now).token


def bearer(role: str, **kwargs: Any) -> dict[str, str]:
    """``Authorization`` header for a role (operators default to ``ASSY-1``)."""
    if role == "operator" and "lines" not in kwargs:
        kwargs["lines"] = ("ASSY-1",)
    return {"Authorization": f"Bearer {token(role, **kwargs)}"}


# --------------------------------------------------------------------------- fake Redis


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> FakePipeline:
            self.ops.append((name, args, kwargs))
            return self

        return record

    async def execute(self) -> list[Any]:
        return [await getattr(self.redis, name)(*a, **kw) for name, a, kw in self.ops]


class FakeRedis:
    """The subset of ``redis.asyncio.Redis`` the API uses (in memory)."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[dict[str, Any]]] = {}
        self.published: list[tuple[str, str]] = []
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("fake redis is down")

    async def ping(self) -> bool:
        self._check()
        return True

    async def get(self, key: str) -> bytes | None:
        self._check()
        value = self.strings.get(key)
        return value.encode() if value is not None else None

    async def set(self, key: str, value: str) -> bool:
        self._check()
        self.strings[key] = value
        return True

    async def hget(self, key: str, field: str) -> bytes | None:
        self._check()
        value = self.hashes.get(key, {}).get(field)
        return value.encode() if value is not None else None

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        self._check()
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}

    async def hset(self, key: str, field: str, value: str) -> int:
        self._check()
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def delete(self, *keys: str) -> int:
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
        return len(keys)

    async def publish(self, channel: str, message: str) -> int:
        self._check()
        self.published.append((channel, message))
        return 1

    async def xadd(self, stream: str, fields: dict[str, Any], **_: Any) -> str:
        self._check()
        entries = self.streams.setdefault(stream, [])
        entries.append(dict(fields))
        return f"{len(entries)}-0"

    async def keys(self, pattern: str = "*") -> list[str]:
        return [k for k in [*self.strings, *self.hashes] if fnmatch.fnmatch(k, pattern)]

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def aclose(self) -> None:
        return None

    # -- helpers
    def put_view(self, name: str, code: str | None, value: Any, prefix: str = "live:") -> None:
        if code is None:
            self.strings[f"{prefix}{name}"] = json.dumps(value)
        else:
            self.hashes.setdefault(f"{prefix}{name}", {})[code] = json.dumps(value)


class QueueSource:
    """A live source fed from the test thread (thread-safe ``queue.Queue``)."""

    def __init__(self) -> None:
        self.queue: queue.Queue[str] = queue.Queue()

    def push(self, kind: str, data: dict[str, Any], ts: str = "2026-10-16T04:30:00Z") -> None:
        self.queue.put(json.dumps({"type": kind, "ts": ts, "data": data}))

    async def messages(self) -> AsyncIterator[str]:
        yield RESYNC
        while True:
            try:
                yield self.queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- fake DB session


class FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self.rows

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> Any:
        return self.rows[0]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.rows)


class FakeSession:
    """Records what a route adds/executes; ``results`` feeds ``execute`` in order."""

    def __init__(self, results: list[list[Any]] | None = None) -> None:
        self.added: list[Any] = []
        self.executed: list[tuple[Any, Any]] = []
        self.results = list(results or [])
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        self.executed.append((stmt, params))
        return FakeResult(self.results.pop(0) if self.results else [])

    async def scalar(self, stmt: Any) -> Any:
        return None

    async def get(self, model: Any, key: Any) -> Any:
        return None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None
