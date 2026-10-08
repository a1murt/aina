"""Collector service: sources -> normalizer -> batches -> spooled outputs (SPEC §7.1–7.3).

Batches close at ``COLLECTOR_BATCH_MAX`` events or ``COLLECTOR_BATCH_MS`` after their first event
(FR-ING-02) and go to two independent outputs, the database and the Redis Stream ``events``,
each with its own store-and-forward spool (FR-ING-03). The service publishes its counters in the
Redis hash ``collector:status`` (the engine waits for an idle collector during a demo reset) and
on ``/stats``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections.abc import Sequence
from typing import Any

import structlog
from redis.asyncio import Redis

from qost_collector.normalize import Normalizer, Stamper
from qost_collector.outputs import Batch, DbTarget, SpooledOutput, StreamTarget
from qost_collector.settings import CollectorSettings
from qost_collector.sources import MqttSource, OpcUaSource, received_now
from qost_collector.spool import Spool
from qost_collector.tagmap import Bindings, compile_bindings
from twin_core.clock import Clock, RedisKV, SimClock, create_clock
from twin_core.config import TwinConfig
from twin_core.db.sink import DbSink, DqRecord
from twin_core.domain import EquipmentState
from twin_core.events import AnyEvent, EventSource
from twin_core.health import load_config_or_exit, start_health_server
from twin_core.log import configure_logging
from twin_core.settings import TwinSettings

log = structlog.get_logger("qost_collector.service")


class CollectorService:
    def __init__(
        self, cfg: TwinConfig, settings: CollectorSettings, clock: Clock, redis: Redis
    ) -> None:
        if cfg.tag_map is None:
            raise SystemExit("collector needs a tag map (PLANT_TAG_MAP or config/tag_map*.yaml)")
        self.cfg = cfg
        self.settings = settings
        self.clock = clock
        self.redis = redis
        self.tag_map = cfg.tag_map
        self.bindings: Bindings = compile_bindings(
            cfg, self.tag_map, topic_root=settings.collector_topic_root
        )
        state_enum: dict[int, EquipmentState] = dict(self.tag_map.opcua.state_enum)
        signals_source: EventSource = "opcua" if settings.collector_signals == "opcua" else "mqtt"
        self.normalizer = Normalizer(cfg, state_enum, signals_source)
        self.mqtt_normalizer = (
            self.normalizer if signals_source == "mqtt" else Normalizer(cfg, state_enum, "mqtt")
        )
        self.sink = DbSink(
            settings.database_url,
            area_of_line={line: cfg.area_of_line(line).code for line in cfg.lines},
        )
        segment = int(settings.collector_spool_segment_mb * 1024 * 1024)
        cap = int(settings.collector_spool_max_gb * 1024**3)
        self.outputs = [
            SpooledOutput(
                DbTarget(self.sink),
                Spool(settings.collector_spool_dir / "db", segment_bytes=segment, max_bytes=cap),
                max_queue=settings.collector_output_queue,
            ),
            SpooledOutput(
                StreamTarget(redis, settings.events_stream, settings.events_stream_maxlen),
                Spool(
                    settings.collector_spool_dir / "stream", segment_bytes=segment, max_bytes=cap
                ),
                max_queue=settings.collector_output_queue,
            ),
        ]
        self.pending: list[AnyEvent] = []
        self.pending_since: float | None = None
        self.emitted = 0
        self.batches = 0
        self.dq_pending: list[DqRecord] = []
        self.stop = asyncio.Event()
        self.stamp = Stamper(lambda: received_now(clock))
        self.sources: list[Any] = []
        if settings.collector_signals == "opcua":
            self.sources.append(
                OpcUaSource(
                    endpoint=settings.opcua_endpoint or self.tag_map.opcua.endpoint,
                    tag_map=self.tag_map.opcua,
                    bindings=self.bindings,
                    normalizer=self.normalizer,
                    emit=self.emit,
                    clock=clock,
                    stamp=self.stamp,
                    queue_size=settings.collector_opcua_queue_size,
                    username=settings.opcua_username,
                    password=settings.opcua_password,
                    cert=str(settings.collector_opcua_cert)
                    if settings.collector_opcua_cert
                    else None,
                    key=str(settings.collector_opcua_key) if settings.collector_opcua_key else None,
                )
            )
        if settings.collector_signals == "mqtt" or settings.collector_units == "mqtt":
            self.sources.append(
                MqttSource(
                    url=settings.mqtt_url or self.tag_map.mqtt.url,
                    topic_root=settings.collector_topic_root or self.tag_map.mqtt.topic_root,
                    bindings=self.bindings,
                    normalizer=self.mqtt_normalizer,
                    emit=self.emit,
                    clock=clock,
                    stamp=self.stamp,
                    signals=settings.collector_signals == "mqtt",
                    units=settings.collector_units == "mqtt",
                    join_grace_ms=settings.collector_join_grace_ms,
                )
            )

    # ------------------------------------------------------------------ pipeline

    def emit(self, events: Sequence[AnyEvent]) -> None:
        if not events:
            return
        if not self.pending:
            self.pending_since = time.monotonic()
        self.pending.extend(events)
        self.emitted += len(events)
        while len(self.pending) >= self.settings.collector_batch_max:
            self._submit(self.pending[: self.settings.collector_batch_max])
            self.pending = self.pending[self.settings.collector_batch_max :]
            self.pending_since = time.monotonic() if self.pending else None

    def _submit(self, events: Sequence[AnyEvent]) -> None:
        batch = Batch.of(events)
        self.batches += 1
        for out in self.outputs:
            out.submit(batch)

    async def batcher(self) -> None:
        period = self.settings.collector_batch_ms / 1000.0
        while not self.stop.is_set():
            await asyncio.sleep(min(period / 4, 0.05))
            if (
                self.pending
                and self.pending_since is not None
                and time.monotonic() - self.pending_since >= period
            ):
                events, self.pending = self.pending, []
                self.pending_since = None
                self._submit(events)

    async def dq_writer(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(2.0)
            self.dq_pending.extend(self.normalizer.drain_dq())
            if self.mqtt_normalizer is not self.normalizer:
                self.dq_pending.extend(self.mqtt_normalizer.drain_dq())
            if not self.dq_pending:
                continue
            try:
                await self.sink.write_dq(self.dq_pending)
                self.dq_pending = []
            except Exception as exc:
                log.warning("dq_write_failed", error=str(exc)[:200])
                with contextlib.suppress(Exception):
                    await self.sink.reset_pool()

    async def status(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(0.25)
            stats = self.stats()
            flat: dict[str, int] = {
                "emitted": int(stats["emitted"]),
                "queued": len(self.pending) + sum(o.backlog for o in self.outputs),
                "spooled": sum(o.spool.size_bytes for o in self.outputs),
                "db_written": self.outputs[0].written_events,
                "stream_written": self.outputs[1].written_events,
                "db_healthy": int(self.outputs[0].healthy),
                "stream_healthy": int(self.outputs[1].healthy),
            }
            with contextlib.suppress(Exception):
                await self.redis.hset(self.settings.collector_status_key, mapping=dict(flat))  # type: ignore[arg-type]

    async def control(self) -> None:
        """Simulator reset: values restart at demo_start, forget the re-delivery caches."""
        while not self.stop.is_set():
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(self.settings.sim_control_channel)
                while not self.stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is None:
                        continue
                    with contextlib.suppress(ValueError, TypeError, KeyError):
                        if json.loads(msg["data"]).get("action") == "reset":
                            self.normalizer.reset()
                            self.mqtt_normalizer.reset()
                            log.info("reset_caches")
            except Exception as exc:
                log.warning("control_unavailable", error=str(exc)[:200])
                await asyncio.sleep(1.0)

    def ready(self) -> bool:
        return all(getattr(s, "connected", True) for s in self.sources)

    def stats(self) -> dict[str, Any]:
        sources: dict[str, Any] = {}
        for s in self.sources:
            if isinstance(s, OpcUaSource):
                sources["opcua"] = {
                    "connected": s.connected,
                    "subscribed": len(s.subscribed),
                    "notifications": s.notifications,
                    "overflows": s.overflows,
                    "reconnects": s.reconnects,
                }
            elif isinstance(s, MqttSource):
                sources["mqtt"] = {
                    "connected": s.connected,
                    "messages": s.messages,
                    "unit_events": s.unit_events,
                }
        return {
            "emitted": self.emitted,
            "batches": self.batches,
            "pending": len(self.pending),
            "dropped_redeliveries": self.normalizer.dropped,
            "outputs": {o.name: o.stats() for o in self.outputs},
            "sources": sources,
            "bindings": {
                "nodes": len(self.bindings.nodes),
                "unused": len(self.bindings.unused),
                "rejected": len(self.bindings.rejected),
            },
        }

    async def run(self) -> None:
        tasks = [asyncio.create_task(s.run(self.stop)) for s in self.sources]
        tasks += [asyncio.create_task(o.run(self.stop)) for o in self.outputs]
        tasks += [
            asyncio.create_task(self.batcher()),
            asyncio.create_task(self.dq_writer()),
            asyncio.create_task(self.status()),
            asyncio.create_task(self.control()),
        ]
        await self.stop.wait()
        if self.pending:
            self._submit(self.pending)
            self.pending = []
        # give the outputs a moment to flush their queues
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(o.backlog for o in self.outputs):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # whatever is still queued goes to the spool (sent on the next start)
        for o in self.outputs:
            while not o.queue.empty():
                o.spool.append(o.queue.get_nowait().line())
        await self.sink.aclose()


async def run_service() -> None:
    log_ = configure_logging("collector")
    cfg = load_config_or_exit("collector")
    settings = CollectorSettings()
    twin = TwinSettings()
    redis = Redis.from_url(settings.redis_url)
    clock = create_clock(twin, RedisKV(redis))
    service = CollectorService(cfg, settings, clock, redis)
    server = await start_health_server(
        "collector", port=settings.health_port, ready=service.ready, stats=service.stats
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.stop.set)
    log_.info(
        "collector_starting",
        signals=settings.collector_signals,
        units=settings.collector_units,
        nodes=len(service.bindings.nodes),
        unused=len(service.bindings.unused),
        rejected=service.bindings.rejected,
    )
    if isinstance(clock, SimClock):
        async with clock:
            await service.run()
    else:
        await service.run()
    server.close()
    await server.wait_closed()
    await redis.aclose()
    log_.info("collector_stopped")
