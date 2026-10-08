"""Read-only data sources of the collector: OPC UA subscription and MQTT UNS (SPEC §7.1, §7.3).

FR-ING-05 / T-RO: this module only browses, reads and subscribes. It never writes values or
calls methods on the plant side (a static test scans the collector's sources).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import aiomqtt
import structlog
from asyncua import ua
from asyncua.client.client import Client
from asyncua.common.subscription import DataChangeEvent
from asyncua.ua.object_ids import ObjectIds
from asyncua.ua.uaerrors import UaError

from qost_collector.normalize import Normalizer, Stamp, mqtt_payload
from qost_collector.tagmap import Binding, Bindings, forbidden, is_units_topic
from twin_core.clock import Clock, ClockNotReadyError, system_now
from twin_core.config.tag_map import OpcUaMap
from twin_core.events import AnyEvent, Quality, parse_event

log = structlog.get_logger("qost_collector.sources")

for _name in ("asyncua", "asyncua.client", "asyncua.common"):
    logging.getLogger(_name).setLevel(logging.WARNING)

Emit = Callable[[Sequence[AnyEvent]], None]
_OVERFLOW_BITS = 0x0480  # InfoType = DataValue | Overflow (OPC UA Part 4, StatusCode bits)


def received_now(clock: Clock) -> datetime | None:
    try:
        return clock.now()
    except ClockNotReadyError:
        return None


def quality_of(code: ua.StatusCode | None) -> Quality:
    """OPC UA StatusCode -> event quality (FR-ING-04)."""
    if code is None:
        return "good"
    value = int(code.value)
    severity = value >> 30
    if severity == 0:
        return "good"
    if severity == 1:
        return "uncertain"
    return "bad"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class OpcUaSource:
    """Subscribes to the bound nodes; one publish response = one normalizer batch."""

    def __init__(
        self,
        *,
        endpoint: str,
        tag_map: OpcUaMap,
        bindings: Bindings,
        normalizer: Normalizer,
        emit: Emit,
        clock: Clock,
        stamp: Stamp,
        queue_size: int,
        username: str | None = None,
        password: str | None = None,
        cert: str | None = None,
        key: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.tag_map = tag_map
        self.bindings = bindings
        self.normalizer = normalizer
        self.emit = emit
        self.clock = clock
        self.stamp = stamp
        self.queue_size = queue_size
        self.username = username
        self.password = password
        self.cert = cert
        self.key = key
        self.connected = False
        self.subscribed: list[str] = []
        self.notifications = 0
        self.overflows = 0
        self.reconnects = 0
        self._by_nodeid: dict[ua.NodeId, Binding] = {}

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 0.5
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = 0.5
            except (OSError, TimeoutError, UaError, ConnectionError) as exc:
                log.warning("opcua_unavailable", endpoint=self.endpoint, error=str(exc)[:200])
            except Exception as exc:
                log.warning("opcua_session_error", error=f"{type(exc).__name__}: {exc}"[:300])
            self.connected = False
            if stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def _session(self, stop: asyncio.Event) -> None:
        client = Client(url=self.endpoint, timeout=10)
        security = self.tag_map.security
        if security.mode != "None":
            if not (self.cert and self.key):
                raise ValueError("OPC UA security requires COLLECTOR_OPCUA_CERT and _KEY")
            await client.set_security_string(
                f"{security.policy},{security.mode},{self.cert},{self.key}"
            )
        if self.username:
            client.set_user(self.username)
            client.set_password(self.password or "")
        async with client:
            ns = await client.get_namespace_index(self.tag_map.namespace_uri)
            nodes = []
            self._by_nodeid.clear()
            for node_id, binding in self.bindings.nodes.items():
                if forbidden(node_id):  # defence in depth (T-RO)
                    continue
                nid = ua.NodeId.from_string(node_id)
                if nid.NamespaceIndex == 2 and ns != 2:
                    nid = ua.NodeId(nid.Identifier, ua.Int16(ns), nid.NodeIdType)
                node = client.get_node(nid)
                nodes.append(node)
                self._by_nodeid[node.nodeid] = binding
            sub = await client.create_subscription(
                self.tag_map.publishing_interval_ms, handler=None, queue_maxsize=200_000
            )
            results = await sub.subscribe_data_change(
                nodes, queuesize=self.queue_size, sampling_interval=ua.Double(0.0)
            )
            self.subscribed = []
            now = received_now(self.clock) or system_now()
            for node, result in zip(nodes, results, strict=True):
                binding = self._by_nodeid[node.nodeid]
                if isinstance(result, ua.StatusCode):
                    log.warning("opcua_node_unknown", node=binding.key, status=str(result))
                    self.normalizer.unknown("tag", binding.key, now, status=str(result))
                else:
                    self.subscribed.append(binding.key)
            self.connected = True
            log.info("opcua_subscribed", endpoint=self.endpoint, nodes=len(self.subscribed))
            queue = getattr(sub, "_event_queue", None)
            while not stop.is_set():
                event = await sub.next_event(timeout=5.0)
                if event is None:
                    # no data for a while: probe the session (a Read service call)
                    server_time = client.get_node(
                        ua.NodeId(ua.Int32(ObjectIds.Server_ServerStatus_CurrentTime))
                    )
                    await asyncio.wait_for(server_time.read_value(), timeout=5.0)
                    continue
                self._feed(event)
                while queue is not None and not queue.empty():
                    item = queue.get_nowait()
                    if item is None:
                        break
                    self._feed(item)
                self.emit(self.normalizer.flush(self.stamp))

    def _feed(self, event: Any) -> None:
        if not isinstance(event, DataChangeEvent):
            return
        binding = self._by_nodeid.get(event.node.nodeid)
        if binding is None:
            return
        self.notifications += 1
        dv = event.data.monitored_item.Value
        code = dv.StatusCode if dv is not None else None
        if code is not None and (int(code.value) & _OVERFLOW_BITS) == _OVERFLOW_BITS:
            self.overflows += 1
        ts = _aware(dv.SourceTimestamp if dv is not None else None)
        if ts is None:
            ts = _aware(dv.ServerTimestamp if dv is not None else None) or received_now(self.clock)
        if ts is None:
            return
        self.normalizer.feed(binding, event.value, ts, quality_of(code))


class MqttSource:
    """MQTT UNS subscriber: signal topics (``COLLECTOR_SIGNALS=mqtt``) and/or line ``units``."""

    def __init__(
        self,
        *,
        url: str,
        topic_root: str,
        bindings: Bindings,
        normalizer: Normalizer,
        emit: Emit,
        clock: Clock,
        stamp: Stamp,
        signals: bool,
        units: bool,
        join_grace_ms: int = 50,
    ) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or 1883
        self.topic_root = topic_root.rstrip("/")
        self.bindings = bindings
        self.normalizer = normalizer
        self.emit = emit
        self.clock = clock
        self.stamp = stamp
        self.signals = signals
        self.units = units
        self.join_grace_s = join_grace_ms / 1000.0
        self.connected = False
        self.messages = 0
        self.unit_events = 0
        self._flush_handle: asyncio.TimerHandle | None = None

    def topics(self) -> list[str]:
        if self.signals:
            return [f"{self.topic_root}/#"]
        if self.units:
            return [f"{self.topic_root}/+/+/units"]
        return []

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 0.5
        identifier = f"qost-collector-{os.getpid()}-{id(self) & 0xFFFF:x}"
        while not stop.is_set() and self.topics():
            try:
                async with aiomqtt.Client(self.host, self.port, identifier=identifier) as client:
                    for topic in self.topics():
                        await client.subscribe(topic, qos=1)
                    self.connected = True
                    backoff = 0.5
                    log.info("mqtt_subscribed", topics=self.topics())
                    async for message in client.messages:
                        self._on_message(str(message.topic), message.payload)
                        if stop.is_set():
                            break
            except aiomqtt.MqttError as exc:
                log.warning("mqtt_unavailable", error=str(exc)[:200], retry_s=backoff)
            self.connected = False
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _on_message(self, topic: str, payload: Any) -> None:
        self.messages += 1
        raw = payload if isinstance(payload, bytes | str) else bytes(payload or b"")
        if is_units_topic(topic):
            if not self.units or topic not in self.bindings.units_topics:
                return
            try:
                event = parse_event(raw)
            except ValueError:
                self.normalizer.unknown("payload", topic, received_now(self.clock) or system_now())
                return
            event = event.model_copy(update={"source": "mqtt", "received_ts": self.stamp()})
            self.unit_events += 1
            self.emit([event])
            return
        if not self.signals:
            return
        binding = self.bindings.topics.get(topic)
        if binding is None:
            if not forbidden(topic):
                self.normalizer.unknown("topic", topic, received_now(self.clock) or system_now())
            return
        try:
            value, ts, quality = mqtt_payload(raw)
        except (ValueError, AttributeError):
            self.normalizer.unknown("payload", topic, received_now(self.clock) or system_now())
            return
        if ts is None:
            ts = received_now(self.clock)
            if ts is None:
                return
        self.normalizer.feed(binding, value, ts, quality)
        if self._flush_handle is None:
            loop = asyncio.get_running_loop()
            self._flush_handle = loop.call_later(self.join_grace_s, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        events = self.normalizer.flush(self.stamp)
        if events:
            self.emit(events)

    def close(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        with contextlib.suppress(Exception):
            self._flush()


__all__ = ["MqttSource", "OpcUaSource", "quality_of", "received_now"]
