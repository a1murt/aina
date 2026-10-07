"""OPC UA subscriptions see every state change with plant-time SourceTimestamps (for M3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from asyncua.client.client import Client

from qost_sim.address_space import build_address_space, load_contract, state_codes
from qost_sim.model import PlantModel
from qost_sim.opcua_server import OpcUaServer
from sim_support import free_port
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState

pytestmark = pytest.mark.integration


class Collector:
    def __init__(self, expected: int) -> None:
        self.changes: list[tuple[Any, datetime | None]] = []
        self.expected = expected
        self.done = asyncio.Event()

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        self.changes.append((val, data.monitored_item.Value.SourceTimestamp))
        if len(self.changes) >= self.expected:
            self.done.set()


async def test_subscription_delivers_all_transitions(cfg: TwinConfig) -> None:
    demo = cfg.simulation.clock.demo_start
    model = PlantModel(cfg, start=demo - timedelta(hours=1))
    model.run_until_time(demo + timedelta(minutes=30))
    model.drain()
    contract = load_contract(cfg)
    codes = state_codes(contract)
    url = f"opc.tcp://127.0.0.1:{free_port()}/qost/"
    server = OpcUaServer(
        build_address_space(cfg),
        endpoint=url,
        namespace_uri=contract.opcua.namespace_uri,
        state_codes=codes,
    )
    await server.start(model.now)
    try:
        await server.write_snapshot(model, model.now)
        async with Client(url) as client:
            handler = Collector(expected=3)
            sub = await client.create_subscription(100, handler)
            node = client.get_node("ns=2;s=KST.ASSY.ASSY-1.CONV-03.State")
            await sub.subscribe_data_change(node, queuesize=100)
            await asyncio.sleep(0.3)
            t_inject = model.now
            model.apply(cfg.scenarios["S1-CHAIN-BREAK"].inject)
            model.run_until(model.env.now + 56 * 60)
            # both transitions are written within one publishing interval
            await server.write_records(model, model.drain())
            async with asyncio.timeout(5):
                await handler.done.wait()
            await sub.delete()
    finally:
        await server.stop()
    values = [v for v, _ in handler.changes]
    assert values[1:] == [codes[EquipmentState.DOWN_UNPLANNED], codes[EquipmentState.RUNNING]]
    assert handler.changes[1][1] == t_inject
    assert handler.changes[2][1] == t_inject + timedelta(minutes=55)
