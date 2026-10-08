"""Collector sources against the live virtual plant (in process): OPC UA subscription and the
MQTT UNS give the same events (SPEC §6.8 equivalence); the subscription is exactly the tag map
without the Degradation oracle (runtime T-RO); no OPC UA queue overflow at 300x."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest
from m3_stack import MQTT_HOST, REDIS_URL

from qost_collector.normalize import Normalizer, Stamper
from qost_collector.sources import MqttSource, OpcUaSource
from qost_collector.tagmap import compile_bindings
from qost_sim.address_space import build_address_space, load_contract, state_codes
from qost_sim.bus import RedisBus
from qost_sim.live import LiveRunner
from qost_sim.mqtt_pub import MqttPublisher
from qost_sim.opcua_server import OpcUaServer
from qost_sim.settings import SimSettings
from sim_support import free_port
from support import CONFIG_DIR
from twin_core.clock import ManualClock
from twin_core.config import load_config
from twin_core.events import AnyEvent

pytestmark = pytest.mark.integration


def _key(e: AnyEvent) -> tuple[Any, ...]:
    d: Any = e.data
    if e.kind == "state":
        return (e.kind, e.entity, e.ts, str(d.state), d.alarm_code)
    if e.kind == "buffer_level":
        return (e.kind, e.entity, e.ts, d.level)
    if e.kind == "telemetry":
        return (e.kind, e.entity, e.ts, d.signal, d.value)
    if e.kind == "alarm":
        return (e.kind, e.entity, e.ts, d.active)
    if e.kind == "ckd":
        return (e.kind, e.entity, e.ts, d.kits)
    return (e.kind, e.event_id)


def _changes(events: Sequence[AnyEvent]) -> list[AnyEvent]:
    """Telemetry reduced to value changes: OPC UA reports by exception (Status/Value trigger),
    so a rewrite of an identical value yields no notification, while MQTT republishes it."""
    previous: dict[tuple[str, str], float] = {}
    out: list[AnyEvent] = []
    for e in sorted(events, key=lambda x: x.ts):
        if e.kind == "telemetry":
            d: Any = e.data
            key = (e.entity, d.signal)
            if previous.get(key) == d.value:
                continue
            previous[key] = d.value
        out.append(e)
    return out


def _final(events: Sequence[AnyEvent], lo: datetime, hi: datetime) -> set[tuple[Any, ...]]:
    """Last statement per (kind, entity, signal, instant) inside the window."""
    last: dict[tuple[Any, ...], AnyEvent] = {}
    for e in _changes(events):
        if lo <= e.ts <= hi and e.kind in ("state", "buffer_level", "telemetry", "alarm"):
            signal = getattr(e.data, "signal", "")
            last[(e.kind, e.entity, signal, e.ts)] = e
    return {_key(e) for e in last.values()}


async def test_opcua_and_mqtt_paths_are_equivalent() -> None:
    cfg = load_config(CONFIG_DIR, tag_map=CONFIG_DIR / "tag_map.demo.yaml")
    assert cfg.tag_map is not None
    prefix = f"it:paths:{uuid.uuid4().hex[:8]}"
    settings = SimSettings(
        redis_url=REDIS_URL,
        sim_clock_key=f"{prefix}:clock",
        sim_state_key=f"{prefix}:state",
        sim_control_channel=f"{prefix}:control",
        sim_tick_s=0.05,
    )
    contract = load_contract(cfg)
    codes = state_codes(contract)
    space = build_address_space(cfg)
    url = f"opc.tcp://127.0.0.1:{free_port()}/qost/"
    root = f"{prefix.replace(':', '-')}/qost/v1/KST"
    opcua = OpcUaServer(
        space, endpoint=url, namespace_uri=contract.opcua.namespace_uri, state_codes=codes
    )
    mqtt = MqttPublisher(
        cfg,
        space,
        url=f"mqtt://{MQTT_HOST}:1883",
        topic_root=root,
        state_codes=codes,
        retain=False,
        client_id=f"qost-sim-{uuid.uuid4().hex[:6]}",
    )
    bus = RedisBus(REDIS_URL)
    runner = LiveRunner(cfg, settings, bus=bus, opcua=opcua, mqtt=mqtt, speed=300.0)
    await runner.start_up()
    clock = ManualClock(cfg.simulation.clock.demo_start)
    stamp = Stamper(clock.now)
    bindings = compile_bindings(cfg, cfg.tag_map, topic_root=root)
    enum = dict(cfg.tag_map.opcua.state_enum)
    by_opcua: list[AnyEvent] = []
    by_mqtt: list[AnyEvent] = []
    ua_source = OpcUaSource(
        endpoint=url,
        tag_map=cfg.tag_map.opcua,
        bindings=bindings,
        normalizer=Normalizer(cfg, enum, "opcua"),
        emit=by_opcua.extend,
        clock=clock,
        stamp=stamp,
        queue_size=32,
    )
    mqtt_source = MqttSource(
        url=f"mqtt://{MQTT_HOST}:1883",
        topic_root=root,
        bindings=bindings,
        normalizer=Normalizer(cfg, enum, "mqtt"),
        emit=by_mqtt.extend,
        clock=clock,
        stamp=stamp,
        signals=True,
        units=True,
    )
    stop = asyncio.Event()
    tasks = [asyncio.create_task(s.run(stop)) for s in (ua_source, mqtt_source)]
    runner_task = asyncio.create_task(runner.run())
    try:
        for _ in range(100):
            if ua_source.connected and mqtt_source.connected:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        await runner.start()
        await asyncio.sleep(20.0)
        await runner.pause()
        await asyncio.sleep(2.0)
    finally:
        stop.set()
        mqtt_source.close()
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await runner.shutdown()
        await bus.redis.delete(settings.sim_clock_key, settings.sim_state_key)
        await bus.aclose()

    # runtime T-RO: subscribed == tag map bindings, never the oracle
    assert sorted(ua_source.subscribed) == sorted(bindings.nodes)
    assert not [n for n in ua_source.subscribed if "degradation" in n.lower()]
    assert ua_source.overflows == 0
    states = [e for e in by_opcua if e.kind == "state"]
    lo = max(min(e.ts for e in by_opcua), min(e.ts for e in by_mqtt))
    hi = min(max(e.ts for e in by_opcua), max(e.ts for e in by_mqtt))
    a, b = _final(by_opcua, lo, hi), _final(by_mqtt, lo, hi)
    assert len(a) > 1000
    assert len(states) > 20
    assert a == b
    units = [e for e in by_mqtt if e.kind == "unit"]
    assert units
    assert {e.source for e in units} == {"mqtt"}
