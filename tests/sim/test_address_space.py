"""OPC UA address space (SPEC §6.7), MQTT topics (§6.8) and the generated tag map."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from qost_sim.__main__ import main as sim_main
from qost_sim.address_space import (
    NAMESPACE_INDEX,
    build_address_space,
    load_contract,
    render_tag_map,
    state_codes,
    write_tag_map,
)
from support import CONFIG_DIR
from twin_core.config import TwinConfig, load_tag_map
from twin_core.domain import EquipmentState


def test_node_set_follows_the_plant_model(cfg: TwinConfig) -> None:
    space = build_address_space(cfg)
    by_kind = Counter(v.kind for v in space.variables)
    signals = sum(len(cfg.equipment_types[e.type].signals) for e in cfg.equipment.values())
    wearing = [e for e in cfg.equipment.values() if e.type in cfg.simulation.degradation.per_type]
    assert by_kind["equipment"] == 4 * len(cfg.equipment) + signals + len(wearing)
    assert by_kind["line"] == 7 * len(cfg.lines)
    assert by_kind["buffer"] == 2 * len(cfg.buffers)
    assert by_kind["product"] == len(cfg.products)
    assert by_kind["site"] == 3
    idents = [v.ident for v in space.variables]
    assert len(idents) == len(set(idents))
    nodes = {v.node_id: v for v in space.variables}
    state = nodes["ns=2;s=KST.ASSY.ASSY-1.CONV-03.State"]
    assert (state.datatype, state.path) == ("Int32", ("KST", "ASSY", "ASSY-1", "CONV-03", "State"))
    assert nodes["ns=2;s=KST.ASSY.ASSY-1.GoodCount"].datatype == "UInt32"
    assert nodes["ns=2;s=KST.BUFFERS.PBS.Level"].signal == "level"
    assert nodes["ns=2;s=KST.CKD.J7.Kits"].target == "J7"
    assert nodes["ns=2;s=KST.Plant.Clock"].datatype == "DateTime"
    vib = nodes["ns=2;s=KST.ASSY.ASSY-1.CONV-03.vibration_mm_s"]
    assert (vib.unit, vib.lo, vib.hi, vib.is_telemetry) == ("мм/с", 0, 15, True)
    hidden = [v for v in space.variables if v.hidden]
    assert {v.target for v in hidden} == {e.code for e in wearing}
    assert all(v.signal == "degradation" and v.path[-1] == "Degradation" for v in hidden)
    objects = {o.ident for o in space.objects}
    assert {
        "KST",
        "KST.WELD",
        "KST.WELD.WELD-1.ABB-04",
        "KST.BUFFERS",
        "KST.Plant",
        "KST.FG",
    } <= objects
    assert NAMESPACE_INDEX == 2


def test_mqtt_topics(cfg: TwinConfig) -> None:
    space = build_address_space(cfg)
    nodes = space.by_target()
    root = load_contract(cfg).mqtt.topic_root
    vib = nodes[("equipment", "CONV-03", "vibration_mm_s")]
    assert space.topic(vib, root) == "qost/v1/KST/ASSY/ASSY-1/CONV-03/vibration_mm_s"
    assert space.topic(nodes[("buffer", "PBS", "level")], root) == "qost/v1/KST/BUFFERS/PBS/level"
    assert space.topic(nodes[("site", "KST", "clock")], root) == "qost/v1/KST/Plant/clock"
    assert space.units_topic(cfg, "ASSY-1", root) == "qost/v1/KST/ASSY/ASSY-1/units"


def test_state_codes_come_from_the_contract(cfg: TwinConfig) -> None:
    codes = state_codes(load_contract(cfg))
    assert codes[EquipmentState.DOWN_UNPLANNED] == 4
    assert len(codes) == len(EquipmentState)


def test_generated_tag_map_is_valid_complete_and_without_the_oracle(
    cfg: TwinConfig, tmp_path: Path
) -> None:
    contract = load_contract(cfg)
    tag_map = write_tag_map(
        cfg,
        tmp_path / "tag_map.demo.yaml",
        endpoint="opc.tcp://sim:4840/qost/",
        mqtt_url="mqtt://mqtt:1883",
    )
    space = build_address_space(cfg)
    assert len(tag_map.opcua.nodes) == sum(1 for v in space.variables if not v.hidden)
    assert all(n.signal != "degradation" for n in tag_map.opcua.nodes)
    generated = {n.node_id: (n.target, n.signal) for n in tag_map.opcua.nodes}
    for node in contract.opcua.nodes:  # the hand-written example is a subset of the real map
        assert generated[node.node_id] == (node.target, node.signal)
    assert tag_map.opcua.state_enum == contract.opcua.state_enum
    assert tag_map.mqtt.topic_root == contract.mqtt.topic_root
    assert not list(tmp_path.glob("*.tmp"))


def test_committed_demo_tag_map_is_up_to_date(cfg: TwinConfig) -> None:
    contract = load_contract(cfg)
    text = render_tag_map(
        cfg,
        build_address_space(cfg),
        endpoint=contract.opcua.endpoint,
        mqtt_url=contract.mqtt.url,
        contract=contract,
    )
    committed = CONFIG_DIR / "tag_map.demo.yaml"
    assert committed.read_text(encoding="utf-8") == text, "run `make tagmap`"
    load_tag_map(committed, cfg.plant)


def test_tagmap_cli(tmp_path: Path, monkeypatch: object) -> None:
    out = tmp_path / "map.yaml"
    assert sim_main(["tagmap", "--out", str(out)]) == 0
    assert sim_main(["tagmap", "--out", str(out), "--check"]) == 0
    out.write_text(out.read_text("utf-8") + "# edited\n", "utf-8")
    assert sim_main(["tagmap", "--out", str(out), "--check"]) == 1
