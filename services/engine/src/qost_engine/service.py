"""Live driver of the engine (SPEC §9.1): Redis stream in, Redis live view + DB out.

* Consumes ``events`` with the consumer group ``engine`` (pending entries first, then new).
* Applies events to :class:`EngineCore`, publishes its live messages at once (``live:*`` +
  ``live`` channel; alerts also to the ``alerts`` stream), so the live latency does not depend on
  the database.
* Every ``ENGINE_COMMIT_MS`` writes the coalesced effects and the checkpoint (stream id, last
  event time, state snapshot) in one transaction, then XACKs. While the database is down the
  effects wait in memory (write-behind) and live publishing continues; past
  ``ENGINE_MAX_PENDING_OPS`` the engine stops reading (the stream keeps the events).
* On start it restores the checkpoint (NFR-03); if the stream was trimmed past it, the gap is
  replayed from ``event_raw`` first.
* Handles the demo reset on ``sim:control`` (delete the live tail, restore the baseline, ack).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import structlog
from redis.asyncio import Redis

from qost_engine.core import EngineCore, LiveMsg
from qost_engine.core.effects import AlertEscalate, AlertUpsert, Effect
from qost_engine.pdm import PdmCache, PdmFeed, PdmServing
from qost_engine.replay import BASELINE, event_from_row
from qost_engine.settings import EngineSettings
from qost_engine.writer import Checkpoint, EngineWriter, delete_derived, delete_facts
from twin_core.clock import (
    Clock,
    ClockNotReadyError,
    RedisKV,
    SimClock,
    create_clock,
    system_now,
)
from twin_core.config import TwinConfig
from twin_core.events import parse_event
from twin_core.health import load_config_or_exit, start_health_server
from twin_core.live import HASHES, STRINGS, LiveKeys, envelope
from twin_core.log import configure_logging
from twin_core.redis_errors import REDIS_ERRORS
from twin_core.settings import TwinSettings

log = structlog.get_logger("qost_engine.service")

REWIND_TOLERANCE = timedelta(minutes=5)
"""After a reset the plant clock must come back to demo_start (+ this) before timers resume."""

TASKS = ("consume", "timers", "commit", "control", "pdm", "watch")
"""Supervised loops of the live service: any of them ending stops the process (exit code 1)."""


def _stream_id_le(a: str, b: str) -> bool:
    ma, _, sa = a.partition("-")
    mb, _, sb = b.partition("-")
    return (int(ma), int(sa or 0)) <= (int(mb), int(sb or 0))


class Publisher:
    """Writes live messages: snapshot entries, the ``live`` channel, the ``alerts`` stream."""

    def __init__(self, redis: Redis, settings: EngineSettings) -> None:
        self.redis = redis
        self.settings = settings
        self.keys = LiveKeys(settings.live_prefix, settings.live_channel)
        self.published = 0
        self.last_publish_wall = 0.0

    async def publish(self, msgs: list[LiveMsg]) -> None:
        if not msgs:
            return
        pipe = self.redis.pipeline(transaction=False)
        for msg in msgs:
            if msg.store is not None:
                name, field, value = msg.store
                key = self.keys.key(name)
                if field is None:
                    if value is None:
                        pipe.delete(key)
                    else:
                        pipe.set(key, json.dumps(value, ensure_ascii=False, default=str))
                elif value is None:
                    pipe.hdel(key, field)
                else:
                    pipe.hset(key, field, json.dumps(value, ensure_ascii=False, default=str))
            if msg.extra.get("publish", True):
                pipe.publish(self.keys.channel, envelope(msg.type, msg.ts, msg.data))
                self.published += 1
            if msg.type == "alert":
                pipe.xadd(
                    self.settings.alerts_stream,
                    {"j": json.dumps(msg.data, ensure_ascii=False, default=str)},
                    maxlen=self.settings.alerts_stream_maxlen,
                    approximate=True,
                )
        await pipe.execute()
        self.last_publish_wall = time.monotonic()

    async def clear(self) -> None:
        await self.redis.delete(*[self.keys.key(n) for n in (*HASHES, *STRINGS)])


async def reset_stream_and_live(settings: EngineSettings) -> None:
    """After a replay: drop the stream, the consumer group and the live view (fresh start)."""
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.delete(settings.events_stream)
        keys = LiveKeys(settings.live_prefix, settings.live_channel)
        await redis.delete(*[keys.key(n) for n in (*HASHES, *STRINGS)])
    except REDIS_ERRORS as exc:
        log.warning("stream_reset_skipped", error=str(exc))
    finally:
        await redis.aclose()


class EngineService:
    def __init__(
        self,
        cfg: TwinConfig,
        settings: EngineSettings,
        clock: Clock,
        redis: Redis,
        writer: EngineWriter | None = None,
        serving: PdmServing | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = settings
        self.clock = clock
        self.redis = redis
        self.writer = writer or EngineWriter(settings.database_url, cfg=cfg)
        self.publisher = Publisher(redis, settings)
        self.core = self._new_core()
        self.pending: list[Effect] = []
        self.pending_ids: list[str] = []
        self.last_id: str | None = None
        self.checkpoint_id: str | None = None
        self.last_event_ts: datetime | None = None
        self.ready = False
        self.paused = False
        self.stopping = asyncio.Event()
        self.epoch: int | None = None
        self.clock_floor: datetime | None = None
        self.commits = 0
        self.commit_errors = 0
        self.db_ok = True
        self.lag_ms: float | None = None
        self.processed = 0
        self.skipped = 0
        self.poisoned = 0
        self.exit_code = 0
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.backpressure = False
        self.last_consume_wall: float | None = None
        """``time.monotonic()`` of the last XREADGROUP that returned (with or without entries)."""
        self.consume_loop_wall: float | None = None
        """``time.monotonic()`` of the last consume iteration that finished an XREADGROUP call,
        a handled Redis error included: the watchdog exits only on a hung await, not on a
        Redis outage (``/readyz`` still answers 503 from :attr:`last_consume_wall`)."""
        self.group_lag: int | None = None
        self.group_pending: int | None = None
        self.loop_errors: dict[str, int] = dict.fromkeys(TASKS, 0)
        self._failures: dict[str, int] = dict.fromkeys(TASKS, 0)
        self.last_commit_wall = 0.0
        self._live_buffer: list[LiveMsg] = []
        self.serving: PdmServing | None = serving
        self.pdm_cache = PdmCache()
        self.pdm_feed: PdmFeed | None = None
        self.pdm_ticks = 0
        self.pdm_errors = 0
        self._pdm_retry_wall = 0.0

    def _new_core(self, state: dict[str, Any] | None = None) -> EngineCore:
        kwargs: dict[str, Any] = {
            "mode": "live",
            "line_state_source": self.settings.engine_line_state,
        }
        if state:
            return EngineCore.restore(self.cfg, state, **kwargs)
        return EngineCore(self.cfg, **kwargs)

    # ------------------------------------------------------------------ startup

    async def start(self) -> None:
        cp = await self._retry(lambda: self.writer.load_checkpoint(self.settings.engine_checkpoint))
        if cp is not None and cp.state:
            self.core = self._new_core(cp.state)
            self.checkpoint_id = cp.stream_id
            self.last_event_ts = cp.event_ts
            log.info("restored", stream_id=cp.stream_id, event_ts=str(cp.event_ts))
        await self._ensure_group()
        await self._recover_gap(cp)
        await self._start_pdm()
        await self._rebuild_live("restart")
        self.ready = True

    async def _start_pdm(self) -> None:
        """Load the models (``qost_ml``: LightGBM + SHAP, a few seconds) in a thread."""
        if self.serving is None and self.settings.engine_pdm:
            try:
                self.serving = await asyncio.to_thread(PdmServing.load, self.cfg)
            except Exception as exc:  # the engine must run without ML (e.g. a lean image)
                log.warning("pdm_disabled", error=str(exc)[:300])
        if self.serving is not None:
            self.pdm_feed = self.serving.feed(self.writer.pool)
            log.info("pdm_ready", units=len(self.serving.spec.equipment))

    async def _retry(self, fn: Any) -> Any:
        delay = 0.5
        while True:
            try:
                return await fn()
            except REDIS_ERRORS as exc:
                log.warning("db_unavailable", error=str(exc), retry_s=delay)
                await self.writer.reset_pool()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
            except Exception as exc:  # asyncpg errors
                if exc.__class__.__module__.startswith("asyncpg"):
                    log.warning("db_unavailable", error=str(exc), retry_s=delay)
                    await self.writer.reset_pool()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
                else:
                    raise

    async def _ensure_group(self) -> None:
        start = "0"
        try:
            await self.redis.xgroup_create(
                self.settings.events_stream, self.settings.engine_group, id=start, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _recover_gap(self, cp: Checkpoint | None) -> None:
        """Replay ``event_raw`` between the checkpoint and the stream's first entry."""
        if cp is None or cp.event_ts is None or cp.stream_id is None:
            return
        first: Any = await self.redis.xrange(self.settings.events_stream, count=1)
        if not first:
            return
        first_id = first[0][0].decode()
        if _stream_id_le(first_id, cp.stream_id):
            return
        first_ts = parse_event(first[0][1][b"j"]).ts
        log.warning("stream_gap", checkpoint=cp.stream_id, first=first_id)
        pool = await self.writer.pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_id, ts, received_ts, source, entity_type, entity, kind, "
                "data::text AS data, quality FROM event_raw WHERE ts > $1 AND ts < $2 "
                "AND kind <> 'telemetry' ORDER BY ts, received_ts NULLS FIRST, event_id",
                cp.event_ts,
                first_ts,
            )
        site = self.cfg.plant.site.code
        for row in rows:
            self.core.apply(event_from_row(row, site))
        effects, _live = self.core.drain()
        self.pending.extend(effects)

    async def _rebuild_live(self, reason: str) -> None:
        now = self._now()
        await self.publisher.clear()
        if now is not None:
            msgs = self.core.full_views(now)
            for m in msgs:
                m.extra["publish"] = False
            await self.publisher.publish(msgs)
        await self._refresh_counts()
        await self.redis.publish(
            self.settings.live_channel,
            envelope("snapshot", now or system_now(), {"reason": reason}),
        )

    def _now(self) -> datetime | None:
        try:
            now = self.clock.now()
        except ClockNotReadyError:
            return None
        if self.clock_floor is not None:
            # after a reset, ignore the old plant time until the simulator has rewound
            if now > self.clock_floor:
                return None
            self.clock_floor = None
        return now

    # ------------------------------------------------------------------ loops

    async def _guard(self, name: str, body: Callable[[], Awaitable[None]]) -> None:
        """Run one loop iteration; log and count a failure, re-raise after too many in a row
        (the task then ends and :meth:`_task_done` stops the process)."""
        try:
            await body()
        except Exception:
            self.loop_errors[name] += 1
            self._failures[name] += 1
            failures = self._failures[name]
            log.exception(f"{name}_failed", failures=failures)
            if failures >= self.settings.engine_loop_max_failures:
                raise
            await asyncio.sleep(min(0.1 * failures, 2.0))
        else:
            self._failures[name] = 0

    async def consume(self) -> None:
        stream = self.settings.events_stream
        group, consumer = self.settings.engine_group, self.settings.engine_consumer
        pending_phase = True
        while not self.stopping.is_set():
            self.backpressure = len(self.pending) > self.settings.engine_max_pending_ops
            if self.paused or self.backpressure:
                await asyncio.sleep(0.05)
                continue
            try:
                resp: Any = await self.redis.xreadgroup(
                    group,
                    consumer,
                    {stream: "0" if pending_phase else ">"},
                    count=self.settings.engine_read_count,
                    block=None if pending_phase else self.settings.engine_block_ms,
                )
            except Exception as exc:
                if "NOGROUP" in str(exc):
                    with contextlib.suppress(*REDIS_ERRORS):
                        await self._ensure_group()
                    self.consume_loop_wall = time.monotonic()
                    continue
                log.warning("redis_unavailable", error=str(exc))
                self.consume_loop_wall = time.monotonic()
                await asyncio.sleep(0.5)
                continue
            self.last_consume_wall = self.consume_loop_wall = time.monotonic()
            entries = resp[0][1] if resp else []
            if pending_phase and not entries:
                pending_phase = False
                continue
            if not entries:
                continue
            try:
                self._process(entries)
                await self._flush_live()
            except Exception:
                # the batch ids are already in pending_ids: they are acked with the next commit
                self.loop_errors["consume"] += 1
                log.exception(
                    "consume_failed",
                    stream_id=f"{entries[0][0].decode()}..{entries[-1][0].decode()}",
                )

    def _process(self, entries: list[tuple[bytes, dict[bytes, bytes]]]) -> None:
        for raw_id, fields in entries:
            sid = raw_id.decode()
            self.pending_ids.append(sid)
            self.last_id = sid
            if self.checkpoint_id is not None and _stream_id_le(sid, self.checkpoint_id):
                self.skipped += 1
                continue
            if fields.get(b"k") == b"telemetry":
                continue
            raw = fields.get(b"j")
            if raw is None:
                continue
            try:
                event = parse_event(raw)
            except ValueError as exc:
                log.warning("bad_event", stream_id=sid, error=str(exc)[:200])
                continue
            try:
                self.core.apply(event)
            except Exception:
                # a poisoned event is logged and skipped (acked with the batch), never retried
                self.poisoned += 1
                log.exception(
                    "consume_failed",
                    stream_id=sid,
                    kind=event.kind,
                    entity=event.entity,
                    ts=event.ts.isoformat(),
                )
                continue
            self.processed += 1
            self.last_event_ts = event.ts
            now = self._now()
            speed = self.clock.speed
            if now is not None and speed > 0:
                self.lag_ms = max(0.0, (now - event.ts).total_seconds()) * 1000.0 / speed
        effects, live = self.core.drain()
        self.pending.extend(effects)
        self._live_buffer.extend(live)

    async def _flush_live(self) -> None:
        msgs, self._live_buffer = self._live_buffer, []
        now = self._now()
        if now is not None and self.core.kpi_due(now):
            wall = time.monotonic()
            if (
                wall - self.publisher.last_publish_wall
            ) * 1000.0 >= self.settings.engine_publish_min_ms:
                kpi = self.core.live_kpi(now)
                for m in kpi:
                    if m.data.get("level") == "equipment":
                        m.extra["publish"] = False
                msgs.extend(kpi)
                effects, live = self.core.drain()
                self.pending.extend(effects)
                msgs.extend(live)
        try:
            await self.publisher.publish(msgs)
        except REDIS_ERRORS as exc:
            log.warning("publish_failed", error=str(exc))

    def _clock_stale(self) -> bool:
        """The simulator stopped writing the clock (or the host slept): plant time is frozen at
        the cap, so timers and PdM wait instead of acting on it (STAB-4)."""
        return bool(getattr(self.clock, "is_stale", False))

    async def timers(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(0.1)
            if self.paused or not self.ready or self._clock_stale():
                continue
            await self._guard("timers", self._timers_tick)

    async def _timers_tick(self) -> None:
        now = self._now()
        if now is None:
            return
        self.core.close_grace_s = self.settings.engine_close_grace_wall_s * max(
            self.clock.speed, 1.0
        )
        self.core.advance_to(now)
        effects, live = self.core.drain()
        self.pending.extend(effects)
        self._live_buffer.extend(live)
        await self._flush_live()

    async def pdm(self) -> None:
        """PdM tick every ``pdm_tick_min`` plant minutes (SPEC §11.1): refresh the cache from
        the database, evaluate in a thread, apply to the core."""
        while not self.stopping.is_set():
            await asyncio.sleep(0.5)
            if self.paused or not self.ready or self.serving is None or self.pdm_feed is None:
                continue
            if self._clock_stale():
                continue
            await self._guard("pdm", self._pdm_tick)

    async def _pdm_tick(self) -> None:
        if self.serving is None or self.pdm_feed is None:
            return
        now = self._now()
        if now is None or time.monotonic() < self._pdm_retry_wall:
            return
        slot = self.core.pdm_slot(now, self.settings.engine_pdm_settle_s)
        if slot is None:
            return
        epoch, core = self.epoch, self.core
        asof = slot + timedelta(seconds=self.settings.engine_pdm_settle_s)
        try:
            await self.pdm_feed.refresh(self.pdm_cache, asof)
            tick = await asyncio.to_thread(self.serving.evaluate, self.pdm_cache, slot, None, asof)
        except Exception as exc:
            self.pdm_errors += 1
            self._pdm_retry_wall = time.monotonic() + self.settings.engine_pdm_retry_s
            self.pdm_cache.clear()
            log.warning("pdm_tick_failed", error=str(exc)[:300])
            return
        if self.paused or self.epoch != epoch or self.core is not core:
            return  # a demo reset happened meanwhile
        core.apply_pdm(tick)
        log.info(
            "pdm_tick",
            slot=slot.isoformat(),
            units=len(tick.units),
            limits=[
                (
                    i.equipment,
                    i.signal,
                    i.hours_to_limit and round(i.hours_to_limit, 1),
                    i.n_points,
                )
                for i in tick.limits
                if i.alert
            ],
        )
        effects, live = core.drain()
        self.pending.extend(effects)
        self._live_buffer.extend(live)
        self.pdm_ticks += 1
        await self._flush_live()

    async def committer(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(self.settings.engine_commit_ms / 1000.0)
            if self.paused:
                continue
            await self._guard("commit", self._commit_tick)

    async def _commit_tick(self) -> None:
        await self.commit()

    async def commit(self) -> bool:
        if not self.pending and not self.pending_ids:
            return True
        last_id = self.last_id
        cp = Checkpoint(
            self.settings.engine_checkpoint,
            last_id,
            self.last_event_ts,
            self.core.snapshot(),
            self._now() or self.last_event_ts or system_now(),
        )
        effects, self.pending = self.pending, []
        ids, self.pending_ids = self.pending_ids, []
        try:
            escalated = await self.writer.commit(effects, [cp])
        except Exception as exc:
            self.commit_errors += 1
            self.db_ok = False
            log.warning("commit_failed", error=str(exc)[:200], pending=len(effects))
            self.pending = effects + self.pending
            self.pending_ids = ids + self.pending_ids
            await self.writer.reset_pool()
            return False
        self.db_ok = True
        self.commits += 1
        self.checkpoint_id = last_id
        self.last_commit_wall = time.monotonic()
        if ids:
            with contextlib.suppress(Exception):
                await self.redis.xack(self.settings.events_stream, self.settings.engine_group, *ids)
        try:
            if escalated:
                await self._publish_escalations(escalated)
            if any(isinstance(e, AlertUpsert) for e in effects) or escalated:
                await self._refresh_counts()
        except REDIS_ERRORS as exc:
            log.warning("publish_failed", error=str(exc))
        await self._meta()
        return True

    async def _publish_escalations(self, items: list[AlertEscalate]) -> None:
        msgs = [
            LiveMsg(
                "alert",
                e.ts,
                {"dedup_key": e.dedup_key, "escalation_level": e.level, "notify": list(e.roles)},
            )
            for e in items
        ]
        await self.publisher.publish(msgs)

    async def _refresh_counts(self) -> None:
        try:
            counts = await self.writer.alert_counts()
        except Exception as exc:
            log.warning("alert_counts_failed", error=str(exc)[:200])
            return
        await self.redis.set(
            self.publisher.keys.key("alerts_open"), json.dumps(counts, ensure_ascii=False)
        )

    async def _meta(self) -> None:
        meta = {
            "engine_ts": self.last_event_ts.isoformat() if self.last_event_ts else None,
            "stream_id": self.checkpoint_id,
            "epoch": self.epoch,
            "pdm": {
                "enabled": self.serving is not None,
                "ticks": self.pdm_ticks,
                "errors": self.pdm_errors,
            },
            **self.core.stats,
        }
        with contextlib.suppress(Exception):
            await self.redis.set(
                self.publisher.keys.key("meta"), json.dumps(meta, ensure_ascii=False, default=str)
            )

    # ------------------------------------------------------------------ demo reset

    async def control(self) -> None:
        """Listen for demo resets on ``sim:control``; resubscribe after a Redis outage. A failed
        reset is logged and not acked (the simulator reports a timeout), the loop continues."""
        while not self.stopping.is_set():
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(self.settings.sim_control_channel)
                while not self.stopping.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is not None:
                        await self._guard("control", partial(self._control_msg, msg))
            except REDIS_ERRORS as exc:
                log.warning("control_unavailable", error=str(exc))
                await asyncio.sleep(1.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe()
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _control_msg(self, msg: dict[str, Any]) -> None:
        try:
            body = json.loads(msg["data"])
            action = body.get("action") if isinstance(body, dict) else None
        except (ValueError, TypeError):
            return
        if action != "reset":
            return
        epoch = int(body["epoch"])
        try:
            await self.reset(epoch, body["demo_start"])
        except Exception:
            log.exception("reset_failed", epoch=epoch)

    async def _collector_idle(self) -> None:
        deadline = time.monotonic() + self.settings.engine_reset_wait_s
        while time.monotonic() < deadline:
            try:
                raw: Any = await self.redis.hgetall(self.settings.collector_status_key)
            except REDIS_ERRORS:
                return
            if raw:
                status = {k.decode(): v.decode() for k, v in raw.items()}
                if status.get("queued", "0") == "0" and status.get("spooled", "0") == "0":
                    return
            else:
                return
            await asyncio.sleep(0.1)

    async def reset(self, epoch: int, demo_start: str) -> None:
        if self.epoch == epoch:
            await self.redis.publish(
                f"{self.settings.sim_control_channel}:ack", json.dumps({"epoch": epoch})
            )
            return
        t = datetime.fromisoformat(demo_start.replace("Z", "+00:00"))
        log.info("reset_start", epoch=epoch, demo_start=demo_start)
        self.paused = True
        try:
            await self._collector_idle()
            self.pending, self.pending_ids, self._live_buffer = [], [], []
            pool = await self.writer.pool()
            async with pool.acquire() as conn, conn.transaction():
                facts = await delete_facts(conn, t)
                derived = await delete_derived(conn, self.cfg, t, None, reopen=True)
                row = await conn.fetchrow(
                    "SELECT state, event_ts FROM engine_checkpoint WHERE name = $1", BASELINE
                )
                await conn.execute(
                    "DELETE FROM engine_checkpoint WHERE name = $1", self.settings.engine_checkpoint
                )
            state = None
            if row is not None and row["state"] is not None:
                raw_state = row["state"]
                state = json.loads(raw_state) if isinstance(raw_state, str) else raw_state
            self.core = self._new_core(state)
            self.pdm_cache.clear()
            await self.redis.xtrim(self.settings.events_stream, maxlen=0, approximate=False)
            with contextlib.suppress(Exception):
                await self.redis.xgroup_setid(
                    self.settings.events_stream, self.settings.engine_group, "$"
                )
            self.last_id = None
            self.checkpoint_id = None
            self.last_event_ts = t
            self.clock_floor = t + REWIND_TOLERANCE
            await self.publisher.clear()
            msgs = self.core.full_views(t)
            for m in msgs:
                m.extra["publish"] = False
            await self.publisher.publish(msgs)
            await self._refresh_counts()
            await self.redis.publish(
                self.settings.live_channel, envelope("snapshot", t, {"reason": "reset"})
            )
            self.epoch = epoch
            log.info("reset_done", epoch=epoch, facts=facts, derived=derived)
        finally:
            self.paused = False
        await self.redis.publish(
            f"{self.settings.sim_control_channel}:ack", json.dumps({"epoch": epoch})
        )

    # ------------------------------------------------------------------ health

    def consume_age_s(self) -> float | None:
        """Wall seconds since the last XREADGROUP returned (``None`` before the first one)."""
        if self.last_consume_wall is None:
            return None
        return time.monotonic() - self.last_consume_wall

    def _consume_stalled(self, limit_s: float, *, loop: bool = False) -> bool:
        """The consumer has not read for ``limit_s`` (``loop``: the consume loop itself has not
        come back from an XREADGROUP call, errors included, i.e. a hung await)."""
        if self.paused or self.backpressure or limit_s <= 0:
            return False
        wall = self.consume_loop_wall if loop else self.last_consume_wall
        age = None if wall is None else time.monotonic() - wall
        return age is not None and age > limit_s

    def health(self) -> tuple[bool, list[str]]:
        """Readiness and the reasons it is false (``/readyz``, STAB-2)."""
        reasons: list[str] = []
        if not self.ready:
            reasons.append("starting")
        dead = [name for name, task in self.tasks.items() if task.done()]
        if dead:
            reasons.append("task_done:" + ",".join(dead))
        if self.ready and self._consume_stalled(self.settings.engine_consume_stale_s):
            reasons.append(f"consume_stale:{self.consume_age_s():.0f}s")
        if self.ready and self.last_consume_wall is None and self.tasks:
            reasons.append("consume_not_started")
        return not reasons, reasons

    def is_ready(self) -> bool:
        return self.health()[0]

    def stats(self) -> dict[str, object]:
        age = self.consume_age_s()
        speed = self.clock.speed
        ready, reasons = self.health()
        return {
            "ready": ready,
            "not_ready": reasons,
            "db_ok": self.db_ok,
            "processed": self.processed,
            "skipped": self.skipped,
            "poisoned": self.poisoned,
            "pending_ops": len(self.pending),
            "pending_ids": len(self.pending_ids),
            "checkpoint_id": self.checkpoint_id,
            "last_id": self.last_id,
            "lag_ms": None if self.lag_ms is None or speed <= 0 else round(self.lag_ms, 1),
            "clock_speed": speed,
            "commits": self.commits,
            "commit_errors": self.commit_errors,
            "published": self.publisher.published,
            "epoch": self.epoch,
            "paused": self.paused,
            "tasks_alive": {name: not task.done() for name, task in self.tasks.items()},
            "loop_errors": dict(self.loop_errors),
            "last_consume_age_s": None if age is None else round(age, 2),
            "last_consume_wall": None
            if age is None
            else (system_now() - timedelta(seconds=age)).isoformat(),
            "group_lag": self.group_lag,
            "group_pending": self.group_pending,
            **self.core.stats,
        }

    async def watch(self) -> None:
        """Watchdog: refresh the consumer-group lag (XINFO GROUPS) and stop the process when the
        consumer has stalled (a hung await, STAB-2): the container restarts from the checkpoint."""
        period = self.settings.engine_watch_s
        last = time.monotonic()
        strikes = 0
        while not self.stopping.is_set():
            await asyncio.sleep(period)
            now = time.monotonic()
            # a long synchronous step (the loop itself was blocked) is not a stalled consumer
            blocked, last = now - last > period + 5.0, now
            await self._refresh_group_lag()
            if blocked or not self._consume_stalled(self.settings.engine_consume_exit_s, loop=True):
                strikes = 0
                continue
            strikes += 1
            if strikes >= 3:
                log.error("consume_stalled", age_s=round(self.consume_age_s() or 0.0, 1))
                self._fail()
                return

    async def _refresh_group_lag(self) -> None:
        try:
            groups: Any = await self.redis.xinfo_groups(self.settings.events_stream)
        except REDIS_ERRORS as exc:
            if "no such key" not in str(exc).lower():
                log.warning("group_lag_failed", error=str(exc)[:200])
            self.group_lag = self.group_pending = None
            return
        for group in groups or []:
            info = {_text(k): v for k, v in dict(group).items()}
            if _text(info.get("name")) == self.settings.engine_group:
                lag, pending = info.get("lag"), info.get("pending")
                self.group_lag = int(lag) if lag is not None else None
                self.group_pending = int(pending) if pending is not None else None
                return
        self.group_lag = self.group_pending = None

    def _fail(self) -> None:
        self.exit_code = 1
        self.stopping.set()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if self.stopping.is_set():
            return
        name = task.get_name()
        if task.cancelled():
            log.error("task_cancelled", task=name)
        elif (exc := task.exception()) is not None:
            log.error("task_failed", task=name, exc_info=exc)
        else:
            log.error("task_exited", task=name)
        self._fail()

    def _spawn(self) -> None:
        loops: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {
            "consume": self.consume,
            "timers": self.timers,
            "commit": self.committer,
            "control": self.control,
            "pdm": self.pdm,
            "watch": self.watch,
        }
        for name in TASKS:
            task = asyncio.create_task(loops[name](), name=name)
            task.add_done_callback(self._task_done)
            self.tasks[name] = task

    async def run(self) -> int:
        """Run until SIGTERM/SIGINT (exit code 0) or until a loop dies (exit code 1: the
        container restarts and resumes from the checkpoint, STAB-2)."""
        await self.start()
        self._spawn()
        try:
            await self.stopping.wait()
        finally:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await self.commit()
            await self.writer.aclose()
        if self.exit_code:
            log.error("engine_exit", code=self.exit_code)
        return self.exit_code


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def run_service() -> int:
    log_ = configure_logging("engine")
    cfg = load_config_or_exit("engine")
    settings = EngineSettings()
    twin = TwinSettings()
    redis = Redis.from_url(settings.redis_url)
    clock = create_clock(twin, RedisKV(redis))
    service = EngineService(cfg, settings, clock, redis)
    server = await start_health_server(
        "engine",
        port=settings.health_port,
        ready=service.is_ready,
        stats=service.stats,
        reasons=lambda: service.health()[1],
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.stopping.set)
    log_.info("engine_starting", port=settings.health_port, clock=twin.clock_mode)
    if isinstance(clock, SimClock):
        async with clock:
            code = await service.run()
    else:
        code = await service.run()
    server.close()
    await server.wait_closed()
    await redis.aclose()
    log_.info("engine_stopped", code=code)
    return code
