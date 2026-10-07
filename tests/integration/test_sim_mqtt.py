"""MQTT Unified Namespace of the virtual plant (SPEC §6.8) against the compose broker."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta

import aiomqtt
import pytest

from qost_sim.address_space import build_address_space, load_contract, state_codes
from qost_sim.model import EventFactory, PlantModel
from qost_sim.mqtt_pub import MqttPublisher
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState
from twin_core.events import parse_event

pytestmark = pytest.mark.integration

MQTT_HOST = os.environ.get("TEST_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("TEST_MQTT_PORT", "1883"))


async def test_signals_and_unit_events_are_published(cfg: TwinConfig) -> None:
    root = f"test-{uuid.uuid4().hex[:8]}/qost/v1/KST"
    contract = load_contract(cfg)
    codes = state_codes(contract)
    space = build_address_space(cfg)
    demo = cfg.simulation.clock.demo_start
    model = PlantModel(cfg, start=demo - timedelta(hours=2), telemetry_period_s=300)
    model.run_until_time(demo + timedelta(minutes=20))
    model.drain()
    factory = EventFactory.deterministic(
        site="KST", t0=model.t0, seed=model.seed, nonce="mqtt-test"
    )
    publisher = MqttPublisher(
        cfg,
        space,
        url=f"mqtt://{MQTT_HOST}:{MQTT_PORT}",
        topic_root=root,
        state_codes=codes,
        retain=False,
        client_id=f"qost-sim-test-{uuid.uuid4().hex[:6]}",
    )
    messages: dict[str, list[str]] = {}
    async with aiomqtt.Client(MQTT_HOST, MQTT_PORT) as sub:
        await sub.subscribe(f"{root}/#", qos=1)
        publisher.start()
        try:
            publisher.publish_snapshot(model, model.now)
            model.apply(cfg.scenarios["S1-CHAIN-BREAK"].inject)
            model.run_until(model.env.now + 3600)
            publisher.publish_records(model, model.drain(), factory)
            wanted = {
                f"{root}/ASSY/ASSY-1/CONV-03/state",
                f"{root}/ASSY/ASSY-1/units",
                f"{root}/BUFFERS/PBS/level",
                f"{root}/CKD/J7/kits",
                f"{root}/PAINT/PAINT-1/BOOTH-02/filter_dp_pa",
            }
            async with asyncio.timeout(15):
                async for message in sub.messages:
                    payload = message.payload
                    assert isinstance(payload, bytes | bytearray)
                    messages.setdefault(str(message.topic), []).append(payload.decode())
                    if wanted <= set(messages) and publisher.queue.empty():
                        break
        finally:
            await publisher.stop()

    states = [json.loads(p) for p in messages[f"{root}/ASSY/ASSY-1/CONV-03/state"]]
    assert {"ts", "value", "quality"} == set(states[0])
    assert states[0]["ts"].endswith("Z")
    assert codes[EquipmentState.DOWN_UNPLANNED] in [s["value"] for s in states]
    units = [parse_event(p) for p in messages[f"{root}/ASSY/ASSY-1/units"]]
    assert {e.kind for e in units} <= {"unit", "defect"}
    assert all(e.entity == "ASSY-1" for e in units)
    assert not [t for t in messages if "egradation" in t]
    assert publisher.dropped == 0
