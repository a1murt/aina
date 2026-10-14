"""Engine service supervision (STAB-2/3/8/9, DEMO-1): no loop dies silently.

* a poisoned stream entry is logged (``consume_failed``) and acked, the consumer keeps going;
* a loop that keeps failing ends its task and :meth:`EngineService.run` returns exit code 1;
* ``/readyz`` turns 503 when a task is done or the consumer has stalled; ``lag_ms`` is null
  while the plant clock is paused;
* Redis client errors (``redis.exceptions.*``) do not kill the loops; a failed reset is not acked.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from engine_support import SHIFT_A, at, state
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from structlog.testing import capture_logs

from qost_engine.service import EngineService
from qost_engine.settings import EngineSettings
from twin_core.clock import ManualClock
from twin_core.config import TwinConfig
from twin_core.events import AnyEvent, dumps
from twin_core.health import start_health_server
from twin_core.redis_errors import REDIS_ERRORS


class FakePipe:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops = 0

    def __getattr__(self, name: str) -> Any:
        def op(*_a: Any, **_k: Any) -> FakePipe:
            self.ops += 1
            return self

        return op

    async def execute(self) -> list[Any]:
        if self.redis.fail_publish:
            raise RedisConnectionError("redis is down")
        return []


class FakeRedis:
    """Just enough of ``redis.asyncio.Redis`` for the service loops."""

    def __init__(self, batches: list[list[tuple[bytes, dict[bytes, bytes]]]] | None = None):
        self.batches = list(batches or [])
        self.acked: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.fail_publish = False
        self.reads = 0
        self.lag = 0

    async def xreadgroup(self, *_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0.005)
        self.reads += 1
        if self.batches:
            return [[b"events", self.batches.pop(0)]]
        return []

    async def xack(self, _stream: str, _group: str, *ids: str) -> int:
        self.acked.extend(ids)
        return len(ids)

    async def xgroup_create(self, *_a: Any, **_k: Any) -> None:
        return None

    async def xrange(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    async def xinfo_groups(self, _stream: str) -> list[dict[str, Any]]:
        return [{"name": b"engine", "pending": 0, "lag": self.lag}]

    def pipeline(self, transaction: bool = False) -> FakePipe:
        return FakePipe(self)

    async def publish(self, channel: str, data: str) -> int:
        if self.fail_publish:
            raise RedisConnectionError("redis is down")
        self.published.append((channel, data))
        return 0

    async def delete(self, *_keys: str) -> int:
        return 0

    async def set(self, *_a: Any, **_k: Any) -> None:
        if self.fail_publish:
            raise RedisConnectionError("redis is down")

    async def hgetall(self, _key: str) -> dict[bytes, bytes]:
        return {}

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)


class FakePubSub:
    queue: asyncio.Queue[dict[str, Any]]

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.queue = CONTROL

    async def subscribe(self, _channel: str) -> None:
        return None

    async def get_message(self, **_k: Any) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=0.02)
        except TimeoutError:
            return None

    async def unsubscribe(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


CONTROL: asyncio.Queue[dict[str, Any]]


class FakeWriter:
    def __init__(self) -> None:
        self.commits = 0
        self.fail_pool = False

    async def load_checkpoint(self, _name: str) -> None:
        return None

    async def commit(self, _effects: Any, _cps: Any) -> list[Any]:
        self.commits += 1
        return []

    async def alert_counts(self) -> dict[str, int]:
        return {}

    async def pool(self) -> Any:
        raise RuntimeError("database transaction failed")

    async def reset_pool(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def entry(n: int, event: AnyEvent) -> tuple[bytes, dict[bytes, bytes]]:
    return (
        f"{1791000000000 + n}-0".encode(),
        {b"k": event.kind.encode(), b"j": dumps(event).encode()},
    )


def make_service(
    cfg: TwinConfig, redis: FakeRedis, *, now: datetime = SHIFT_A, **settings: Any
) -> tuple[EngineService, ManualClock]:
    global CONTROL
    CONTROL = asyncio.Queue()
    clock = ManualClock(now, speed=60.0)
    base: dict[str, Any] = {
        "engine_pdm": False,
        "engine_commit_ms": 50,
        "engine_watch_s": 0.05,
        "engine_loop_max_failures": 3,
    }
    svc = EngineService(
        cfg,
        EngineSettings(**{**base, **settings}),
        clock,
        redis,  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
    )
    return svc, clock


async def wait_for(cond: Any, limit_s: float = 3.0) -> None:
    deadline = time.monotonic() + limit_s
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_poisoned_entry_is_logged_acked_and_skipped(cfg: TwinConfig) -> None:
    events = [state("CONV-03", "RUNNING", at(k)) for k in range(1, 7)]
    batches = [[entry(k, e)] for k, e in enumerate(events)]
    redis = FakeRedis(batches)
    svc, _clock = make_service(cfg, redis)
    poison_id = batches[2][0][0].decode()
    real_apply = svc.core.apply

    def apply(event: AnyEvent) -> None:
        if event.ts == events[2].ts:
            raise KeyError("poison event")
        real_apply(event)

    svc.core.apply = apply  # type: ignore[method-assign]
    with capture_logs() as logs:
        task = asyncio.create_task(svc.consume())
        await wait_for(lambda: svc.processed == 5)
        assert not task.done()
        assert await svc.commit()
        svc.stopping.set()
        await task
    failed = [r for r in logs if r["event"] == "consume_failed"]
    assert len(failed) == 1
    assert failed[0]["stream_id"] == poison_id
    assert failed[0]["log_level"] == "error"
    assert svc.poisoned == 1
    assert poison_id in redis.acked
    assert len(redis.acked) == 6


async def test_failing_loop_stops_the_service_with_exit_code_1(cfg: TwinConfig) -> None:
    redis = FakeRedis()
    svc, _clock = make_service(cfg, redis)

    def boom(_now: datetime) -> None:
        raise IndexError("list index out of range")

    svc.core.advance_to = boom  # type: ignore[method-assign,assignment]
    with capture_logs() as logs:
        code = await asyncio.wait_for(svc.run(), timeout=10)
    assert code == 1
    names = [r["event"] for r in logs]
    assert names.count("timers_failed") == 3
    died = [r for r in logs if r["event"] == "task_failed"]
    assert died
    assert died[0]["task"] == "timers"
    assert all(t.done() for t in svc.tasks.values())


async def test_sigterm_exits_with_code_0(cfg: TwinConfig) -> None:
    svc, _clock = make_service(cfg, FakeRedis())
    run = asyncio.create_task(svc.run())
    await wait_for(lambda: svc.is_ready())
    svc.stopping.set()
    assert await asyncio.wait_for(run, timeout=5) == 0


async def test_readyz_and_stats(cfg: TwinConfig) -> None:
    redis = FakeRedis()
    redis.lag = 7
    svc, _clock = make_service(cfg, redis)
    run = asyncio.create_task(svc.run())
    try:
        await wait_for(lambda: svc.is_ready() and svc.group_lag == 7)
        stats = svc.stats()
        assert stats["ready"] is True
        assert stats["tasks_alive"] == dict.fromkeys(
            ("consume", "timers", "commit", "control", "pdm", "watch"), True
        )
        assert stats["group_lag"] == 7
        assert isinstance(stats["last_consume_age_s"], float)
        # the consumer has not read for longer than engine_consume_stale_s -> 503
        svc.last_consume_wall = time.monotonic() - 20.0
        ready, reasons = svc.health()
        assert not ready
        assert reasons[0].startswith("consume_stale")
        svc.paused = True  # a demo reset in progress is not a stall
        assert svc.health()[0]
        svc.paused = False
        await wait_for(lambda: svc.is_ready())
        # a finished task -> 503 (before the done callback has stopped the process)
        svc.tasks["extra"] = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        assert svc.health()[1] == ["task_done:extra"]
        del svc.tasks["extra"]
    finally:
        svc.stopping.set()
        assert await asyncio.wait_for(run, timeout=5) == 0


async def test_lag_ms_is_null_while_paused(cfg: TwinConfig) -> None:
    svc, clock = make_service(cfg, FakeRedis())
    svc._process([entry(1, state("CONV-03", "RUNNING", at(-1)))])
    assert svc.stats()["lag_ms"] == pytest.approx(1000.0)  # 60 plant s at 60x
    clock._speed = 0.0  # paused: no division by ~0 (was 2e13 ms)
    svc._process([entry(2, state("CONV-03", "RUNNING", at(-0.5)))])
    assert svc.stats()["lag_ms"] is None


async def test_stalled_consumer_stops_the_service(cfg: TwinConfig) -> None:
    svc, _clock = make_service(cfg, FakeRedis(), engine_consume_exit_s=0.3)

    async def hang(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(3600)  # e.g. a socket without a timeout

    with capture_logs() as logs:
        run = asyncio.create_task(svc.run())
        await wait_for(lambda: svc.last_consume_wall is not None)
        svc.redis.xreadgroup = hang  # type: ignore[method-assign]
        assert await asyncio.wait_for(run, timeout=5) == 1
    assert any(r["event"] == "consume_stalled" for r in logs)


async def test_redis_errors_do_not_kill_the_loops(cfg: TwinConfig) -> None:
    assert issubclass(RedisConnectionError, REDIS_ERRORS)
    assert issubclass(ResponseError, REDIS_ERRORS)
    redis = FakeRedis()
    svc, clock = make_service(cfg, redis)
    run = asyncio.create_task(svc.run())
    try:
        await wait_for(lambda: svc.is_ready())
        redis.fail_publish = True
        clock.advance(timedelta(minutes=10))
        await asyncio.sleep(0.5)
        assert svc.is_ready()
        assert all(not t.done() for t in svc.tasks.values())
        redis.fail_publish = False
        assert svc.loop_errors["timers"] == 0  # caught at the publish, not by the guard
    finally:
        svc.stopping.set()
        assert await asyncio.wait_for(run, timeout=5) == 0


async def test_failed_reset_is_logged_not_acked_and_control_survives(cfg: TwinConfig) -> None:
    redis = FakeRedis()
    svc, _clock = make_service(cfg, redis)
    run = asyncio.create_task(svc.run())
    try:
        await wait_for(lambda: svc.is_ready())
        with capture_logs() as logs:
            body = {"action": "reset", "epoch": 5, "demo_start": "2026-10-16T02:00:00Z"}
            await CONTROL.put({"data": json.dumps(body)})
            await wait_for(lambda: any(r["event"] == "reset_failed" for r in logs))
        assert not [c for c, _ in redis.published if c.endswith(":ack")]
        assert not svc.paused
        assert not svc.tasks["control"].done()
        assert svc.epoch is None
        # the next reset of the same epoch is attempted again (still failing, still alive)
        await CONTROL.put({"data": json.dumps(body)})
        await asyncio.sleep(0.2)
        assert not svc.tasks["control"].done()
    finally:
        svc.stopping.set()
        assert await asyncio.wait_for(run, timeout=5) == 0


async def test_readyz_http_is_503_with_the_reason(cfg: TwinConfig) -> None:
    svc, _clock = make_service(cfg, FakeRedis())
    server = await start_health_server(
        "engine",
        port=0,
        host="127.0.0.1",
        ready=svc.is_ready,
        stats=svc.stats,
        reasons=lambda: svc.health()[1],
    )
    port = server.sockets[0].getsockname()[1]
    run = asyncio.create_task(svc.run())
    try:
        await wait_for(lambda: svc.is_ready())
        status, body = await http_get(port, "/readyz")
        assert (status, body["status"]) == (200, "ready")
        svc.redis.xreadgroup = _hang  # type: ignore[method-assign]
        await asyncio.sleep(0.05)  # the read in flight returns, the next one hangs
        svc.last_consume_wall = time.monotonic() - 20.0
        status, body = await http_get(port, "/readyz")
        assert status == 503
        assert body["not_ready"][0].startswith("consume_stale")
        status, stats = await http_get(port, "/stats")
        assert stats["tasks_alive"]["consume"] is True
        assert stats["last_consume_age_s"] >= 20
    finally:
        svc.stopping.set()
        await asyncio.wait_for(run, timeout=5)
        server.close()
        await server.wait_closed()


async def _hang(*_a: Any, **_k: Any) -> Any:
    await asyncio.sleep(3600)


async def http_get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


async def test_timers_wait_while_the_plant_clock_is_stale(cfg: TwinConfig) -> None:
    """STAB-4: a stale clock (simulator not writing, host slept) does not drive the timers."""
    svc, clock = make_service(cfg, FakeRedis())
    calls: list[datetime] = []
    svc.core.advance_to = calls.append  # type: ignore[method-assign,assignment]
    stale = {"value": True}
    type(clock).is_stale = property(lambda _self: stale["value"])  # type: ignore[attr-defined]
    try:
        svc.ready = True
        task = asyncio.create_task(svc.timers())
        await asyncio.sleep(0.35)
        assert calls == []
        stale["value"] = False
        await wait_for(lambda: bool(calls))
        svc.stopping.set()
        await task
    finally:
        del type(clock).is_stale  # type: ignore[attr-defined]
