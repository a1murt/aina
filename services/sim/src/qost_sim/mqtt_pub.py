"""MQTT Unified Namespace publisher (SPEC §6.8).

Signals: ``{topic_root}/{AREA}/{LINE}/{EQUIPMENT}/{signal}`` (and line / buffer / CKD / plant
topics), payload ``{"ts": "...Z", "value": ..., "quality": "good"}``, QoS 1, retained (last
value). ``State`` carries the same Int32 as OPC UA (``state_enum`` of the tag map).
Unit and defect events: ``{topic_root}/{AREA}/{LINE}/units`` with the full event JSON
(``twin_core.events``), not retained. The hidden ``Degradation`` is never published (FR-SIM-02).

Publishing goes through a bounded queue drained by a reconnecting background task, so a broker
outage never stalls the model (oldest messages are dropped when the queue is full).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import aiomqtt
import structlog

from qost_sim.address_space import AddressSpace
from qost_sim.model import EventFactory, PlantModel, Rec
from twin_core.clock import format_utc
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState
from twin_core.events import dumps

log = structlog.get_logger("qost_sim.mqtt")


@dataclass(frozen=True, slots=True)
class Message:
    topic: str
    payload: str
    retain: bool


def signal_payload(value: Any, ts: datetime, quality: str = "good") -> str:
    if isinstance(value, datetime):
        value = format_utc(value)
    return json.dumps(
        {"ts": format_utc(ts), "value": value, "quality": quality}, ensure_ascii=False
    )


class MqttPublisher:
    def __init__(
        self,
        cfg: TwinConfig,
        space: AddressSpace,
        *,
        url: str,
        topic_root: str,
        state_codes: Mapping[EquipmentState, int],
        qos: int = 1,
        max_queue: int = 50_000,
        client_id: str = "qost-sim",
        retain: bool = True,
    ) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or 1883
        self.cfg = cfg
        self.space = space
        self.topic_root = topic_root.rstrip("/")
        self.state_codes = state_codes
        self.qos = qos
        self.client_id = client_id
        self.retain = retain
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0
        self.published = 0
        self.connected = False
        self._task: asyncio.Task[None] | None = None
        self._topics = {
            (v.kind, v.target, v.signal): space.topic(v, self.topic_root)
            for v in space.variables
            if not v.hidden
        }
        self._units = {
            line: space.units_topic(cfg, line, self.topic_root) for line in cfg.flow_lines
        }

    # ------------------------------------------------------------------ queue

    def _put(self, message: Message) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.queue.task_done()
                self.dropped += 1
        self.queue.put_nowait(message)

    def signal(self, kind: str, target: str, signal: str, value: Any, ts: datetime) -> None:
        topic = self._topics.get((kind, target, signal))
        if topic is None:
            return
        if isinstance(value, EquipmentState):
            value = self.state_codes[value]
        self._put(Message(topic, signal_payload(value, ts), retain=self.retain))

    def event(self, line: str, event_json: str) -> None:
        topic = self._units.get(line)
        if topic is not None:
            self._put(Message(topic, event_json, retain=False))

    def publish_records(
        self, model: PlantModel, records: Iterable[Rec], factory: EventFactory
    ) -> None:
        for rec in records:
            ts = model.at(rec.t)
            data = rec.data
            if rec.kind == "state":
                self.signal(rec.entity_type, rec.entity, "state", EquipmentState(data["state"]), ts)
                if rec.entity_type == "equipment":
                    self.signal("equipment", rec.entity, "state_since", ts, ts)
                    self.signal(
                        "equipment", rec.entity, "alarm_code", data.get("alarm_code") or "", ts
                    )
            elif rec.kind == "alarm":
                self.signal("equipment", rec.entity, "alarm", bool(data["active"]), ts)
            elif rec.kind == "telemetry":
                self.signal("equipment", rec.entity, data["signal"], data["value"], ts)
            elif rec.kind == "buffer_level":
                self.signal("buffer", rec.entity, "level", data["level"], ts)
            elif rec.kind == "ckd":
                self.signal("product", rec.entity, "kits", max(0, data["kits"]), ts)
            elif rec.kind in ("unit", "defect"):
                event = factory.convert(rec)
                if event is not None:
                    self.event(rec.entity, dumps(event))
                if rec.kind == "unit" and rec.extra is not None:
                    extra = rec.extra
                    self.signal("line", rec.entity, "produced_count", extra["produced"], ts)
                    self.signal("line", rec.entity, "good_count", extra["good"], ts)
                    self.signal("line", rec.entity, "reject_count", extra["reject"], ts)
                    if data["result"] != "rework_pass":
                        self.signal("line", rec.entity, "last_body_id", data["body_id"], ts)
                        self.signal("line", rec.entity, "last_product", data["product"], ts)
                        self.signal("line", rec.entity, "cycle_time_s", extra["cycle_time_s"], ts)

    def publish_snapshot(self, model: PlantModel, ts: datetime) -> None:
        snap = model.snapshot()
        for code, u in snap["units"].items():
            self.signal("equipment", code, "state", u["state"], ts)
            self.signal("equipment", code, "state_since", model.at(u["since"]), ts)
            self.signal("equipment", code, "alarm_code", u["reason"] or "", ts)
            self.signal("equipment", code, "alarm", bool(u["alarm"]), ts)
            for signal, value in u["values"].items():
                self.signal("equipment", code, signal, value, ts)
        for code, ln in snap["lines"].items():
            self.signal("line", code, "state", ln["state"], ts)
            for name, key in (
                ("good_count", "good"),
                ("reject_count", "reject"),
                ("produced_count", "produced"),
                ("last_body_id", "last_body"),
                ("last_product", "last_product"),
                ("cycle_time_s", "cycle_time_s"),
            ):
                self.signal("line", code, name, ln[key], ts)
        for code, b in snap["buffers"].items():
            self.signal("buffer", code, "level", b["level"], ts)
            self.signal("buffer", code, "capacity", b["capacity"], ts)
        for product, kits in snap["kits"].items():
            self.signal("product", product, "kits", max(0, kits), ts)

    # ------------------------------------------------------------------ connection

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="mqtt-publisher")

    async def stop(self, *, drain_timeout_s: float = 2.0) -> None:
        if self._task is None:
            return
        if self.connected:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(drain_timeout_s):
                    await self.queue.join()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                async with aiomqtt.Client(
                    self.host, self.port, identifier=self.client_id
                ) as client:
                    self.connected = True
                    backoff = 0.5
                    log.info("mqtt_connected", host=self.host, port=self.port)
                    while True:
                        message = await self.queue.get()
                        try:
                            await client.publish(
                                message.topic, message.payload, qos=self.qos, retain=message.retain
                            )
                            self.published += 1
                        finally:
                            self.queue.task_done()
            except aiomqtt.MqttError as exc:
                self.connected = False
                log.warning("mqtt_unavailable", error=str(exc), retry_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
