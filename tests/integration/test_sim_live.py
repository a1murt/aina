"""Live virtual plant end to end on the compose infrastructure (AC M2).

Redis (plant clock, run state, reset handshake), OPC UA server (asyncua client), MQTT UNS and the
demo console. Keys, channels and topics are unique per run, so a running demo is not disturbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import aiomqtt
import httpx2
import pytest
from asyncua.client.client import Client
from redis.asyncio import Redis

from qost_sim.address_space import build_address_space, load_contract, state_codes
from qost_sim.bus import RedisBus
from qost_sim.control_api import create_app
from qost_sim.live import LiveRunner
from qost_sim.mqtt_pub import MqttPublisher
from qost_sim.opcua_server import OpcUaServer
from qost_sim.settings import SimSettings
from sim_support import free_port
from twin_core.clock import RedisKV, SimClock
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0")
MQTT_HOST = os.environ.get("TEST_MQTT_HOST", "localhost")
SPEED = 300.0


@pytest.fixture
async def stack(cfg: TwinConfig) -> AsyncIterator[dict[str, object]]:
    prefix = f"test:sim:{uuid.uuid4().hex[:8]}"
    settings = SimSettings(
        sim_clock_key=f"{prefix}:clock",
        sim_state_key=f"{prefix}:state",
        sim_control_channel=f"{prefix}:control",
        sim_reset_flush_s=0.1,
        sim_reset_ack_timeout_s=5,
        sim_tick_s=0.05,
    )
    contract = load_contract(cfg)
    codes = state_codes(contract)
    space = build_address_space(cfg)
    url = f"opc.tcp://127.0.0.1:{free_port()}/qost/"
    topic_root = f"test-{uuid.uuid4().hex[:8]}/qost/v1/KST"
    opcua = OpcUaServer(
        space, endpoint=url, namespace_uri=contract.opcua.namespace_uri, state_codes=codes
    )
    mqtt = MqttPublisher(
        cfg,
        space,
        url=f"mqtt://{MQTT_HOST}:1883",
        topic_root=topic_root,
        state_codes=codes,
        retain=False,
        client_id=f"qost-sim-test-{uuid.uuid4().hex[:6]}",
    )
    bus = RedisBus(REDIS_URL)
    runner = LiveRunner(cfg, settings, bus=bus, opcua=opcua, mqtt=mqtt, speed=SPEED)
    await runner.start_up()
    task = asyncio.create_task(runner.run())
    try:
        yield {
            "runner": runner,
            "settings": settings,
            "url": url,
            "codes": codes,
            "topic_root": topic_root,
            "app": create_app(runner),
        }
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await runner.shutdown()
        await bus.redis.delete(settings.sim_clock_key, settings.sim_state_key)
        await bus.aclose()


async def test_live_plant_end_to_end(stack: dict[str, object]) -> None:
    runner = stack["runner"]
    settings = stack["settings"]
    codes = stack["codes"]
    assert isinstance(runner, LiveRunner)
    assert isinstance(settings, SimSettings)
    assert isinstance(codes, dict)
    redis = Redis.from_url(REDIS_URL)
    clock = SimClock(RedisKV(redis), key=settings.sim_clock_key)
    transport = httpx2.ASGITransport(app=stack["app"])  # type: ignore[arg-type]
    try:
        # plant clock in Redis advances at the configured speed (M0 v1 format)
        state = await clock.refresh()
        assert state is not None
        assert (state.sim_mode, state.paused, state.speed) == ("live", False, SPEED)
        first = clock.now()
        await asyncio.sleep(1.0)
        await clock.refresh()
        advanced = (clock.now() - first).total_seconds()
        assert SPEED * 0.7 <= advanced <= SPEED * 1.3

        async with (
            Client(str(stack["url"])) as ua_client,
            aiomqtt.Client(MQTT_HOST, 1883) as sub,
            httpx2.AsyncClient(transport=transport, base_url="http://sim") as http,
        ):
            root = stack["topic_root"]
            await sub.subscribe(f"{root}/ASSY/ASSY-1/CONV-03/state", qos=1)
            node = ua_client.get_node("ns=2;s=KST.ASSY.ASSY-1.CONV-03.State")
            plant_clock = ua_client.get_node("ns=2;s=KST.Plant.Clock")
            assert await plant_clock.read_value() is not None

            began = time.perf_counter()
            response = await http.post("/inject", json={"scenario_id": "S1-CHAIN-BREAK"})
            assert response.status_code == 200
            down = codes[EquipmentState.DOWN_UNPLANNED]
            while await node.read_value() != down:
                assert time.perf_counter() - began < 1.0, "OPC UA did not show S1 within 1 s"
                await asyncio.sleep(0.02)
            alarm = ua_client.get_node("ns=2;s=KST.ASSY.ASSY-1.CONV-03.AlarmCode")
            assert await alarm.read_value() == "ME-CHAIN"

            async with asyncio.timeout(5):
                async for message in sub.messages:
                    payload = message.payload
                    assert isinstance(payload, bytes | bytearray)
                    if json.loads(payload)["value"] == down:
                        break

            # reset handshake: a stand-in for the engine acknowledges the cleanup request
            listener = redis.pubsub()
            await listener.subscribe(settings.sim_control_channel)

            async def engine() -> None:
                async for msg in listener.listen():
                    if msg["type"] == "message":
                        request = json.loads(msg["data"])
                        await redis.publish(
                            f"{settings.sim_control_channel}:ack",
                            json.dumps({"epoch": request["epoch"]}),
                        )
                        return

            acker = asyncio.create_task(engine())
            reset = await http.post("/reset", json={"to": "demo_start"})
            await acker
            await listener.aclose()  # type: ignore[no-untyped-call]
            assert reset.status_code == 200
            assert reset.json()["cleanup"] == "acked"
            assert reset.json()["status"]["epoch"] == runner.epoch == 1
            assert runner.plant_time <= runner.demo_start + timedelta(minutes=5)
            assert await node.read_value() != down

            assert (await http.post("/pause")).json()["paused"] is True
            await clock.refresh()
            assert clock.state is not None
            assert clock.state.paused
            raw = await redis.get(settings.sim_state_key)
            assert raw is not None
            saved = json.loads(raw)
            assert saved["epoch"] == 1
    finally:
        await redis.aclose()
