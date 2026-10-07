"""AC M2: an asyncua client sees the plant tree (in-process server on a free localhost port).

A fast smoke test kept in ``make check``; the fuller live test is tests/integration.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua.uaerrors import UaStatusCodeError

from qost_sim.address_space import build_address_space, load_contract, state_codes
from qost_sim.model import PlantModel
from qost_sim.opcua_server import OpcUaServer
from sim_support import free_port
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState


async def test_client_browses_the_tree_and_reads_live_values(cfg: TwinConfig) -> None:
    began = time.perf_counter()
    demo = cfg.simulation.clock.demo_start
    model = PlantModel(cfg, start=demo - timedelta(hours=6), telemetry_period_s=300)
    model.run_until_time(demo + timedelta(minutes=30))
    model.drain()
    contract = load_contract(cfg)
    space = build_address_space(cfg)
    codes = state_codes(contract)
    url = f"opc.tcp://127.0.0.1:{free_port()}/qost/"
    server = OpcUaServer(
        space, endpoint=url, namespace_uri=contract.opcua.namespace_uri, state_codes=codes
    )
    await server.start(model.now)
    try:
        await server.write_snapshot(model, model.now)
        model.apply(cfg.scenarios["S1-CHAIN-BREAK"].inject)
        inject_time = model.now
        model.run_until(model.env.now + 60)
        await server.write_records(model, model.drain())

        async with Client(url) as client:
            assert await client.get_namespace_index(contract.opcua.namespace_uri) == 2
            kst = await client.nodes.objects.get_child(["2:KST"])
            children = {(await n.read_browse_name()).Name for n in await kst.get_children()}
            assert {a.code for a in cfg.plant.areas} | {"BUFFERS", "Plant"} <= children
            conv = await kst.get_child(["2:ASSY", "2:ASSY-1", "2:CONV-03"])
            names = {(await n.read_browse_name()).Name for n in await conv.get_children()}
            assert {"State", "StateSince", "AlarmCode", "Alarm", "Degradation"} <= names
            assert {"motor_current_a", "vibration_mm_s", "chain_elongation_pct"} <= names

            state = client.get_node("ns=2;s=KST.ASSY.ASSY-1.CONV-03.State")
            value = await state.read_data_value()
            assert value.Value is not None
            assert value.Value.Value == codes[EquipmentState.DOWN_UNPLANNED]
            assert value.SourceTimestamp == inject_time
            alarm = client.get_node("ns=2;s=KST.ASSY.ASSY-1.CONV-03.AlarmCode")
            assert await alarm.read_value() == "ME-CHAIN"
            line = client.get_node("ns=2;s=KST.ASSY.ASSY-1.State")
            assert await line.read_value() == codes[EquipmentState.DOWN_UNPLANNED]
            pbs = client.get_node("ns=2;s=KST.BUFFERS.PBS.Capacity")
            assert await pbs.read_value() == 30

            hidden = client.get_node("ns=2;s=KST.WELD.WELD-1.ABB-04.Degradation")
            assert "FR-SIM-02" in ((await hidden.read_description()).Text or "")
            vib = client.get_node("ns=2;s=KST.ASSY.ASSY-1.CONV-03.vibration_mm_s")
            props = {
                str((await p.read_browse_name()).Name): await p.read_value()
                for p in await vib.get_properties()
            }
            assert props["EURange"] == ua.Range(Low=0.0, High=15.0)
            assert props["EngineeringUnits"].DisplayName.Text == "мм/с"

            nodes = [client.get_node(n.node_id) for n in space.variables if not n.hidden]
            values = await client.read_values(nodes)
            assert len(values) == len(nodes)

            with pytest.raises(UaStatusCodeError, match="Denied"):
                await state.write_value(ua.DataValue(ua.Variant(1, ua.VariantType.Int32)))
    finally:
        await server.stop()
    assert time.perf_counter() - began < 6.0
