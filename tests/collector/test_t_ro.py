"""T-RO (FR-ING-05, FR-SIM-02): the collector never writes to OPC UA / calls methods, and never
subscribes to the simulator's hidden Degradation oracle; the engine never reads it."""

from __future__ import annotations

import ast
from pathlib import Path

from qost_collector.tagmap import compile_bindings, forbidden, subscription_node_ids
from support import REPO_ROOT, mutate
from twin_core.config import TwinConfig, load_config

COLLECTOR_SRC = REPO_ROOT / "services" / "collector" / "src"
ENGINE_SRC = REPO_ROOT / "services" / "engine" / "src"
WRITE_NAMES = frozenset(
    {
        "write_value",
        "write_values",
        "write_attribute",
        "write_attribute_value",
        "write_array",
        "write_params",
        "set_value",
        "set_attribute",
        "set_writable",
        "call_method",
        "call",
        "add_nodes",
        "delete_nodes",
        "add_references",
        "delete_references",
        "WriteParameters",
        "WriteRequest",
        "WriteValue",
        "CallRequest",
        "CallMethodRequest",
        "AddNodesItem",
        "DeleteNodesItem",
        "history_update",
    }
)


def _sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def test_collector_has_no_write_or_call_on_the_plant_side() -> None:
    hits: list[str] = []
    for path in _sources(COLLECTOR_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.name.rsplit(".", 1)[-1]
            if name in WRITE_NAMES:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', '?')} {name}")
    assert hits == []
    assert len(_sources(COLLECTOR_SRC)) >= 8  # the scan sees the real package


def test_subscriptions_never_include_degradation(cfg_with_map: TwinConfig) -> None:
    assert cfg_with_map.tag_map is not None
    bindings = compile_bindings(cfg_with_map, cfg_with_map.tag_map)
    nodes = subscription_node_ids(bindings)
    assert len(nodes) == 86
    assert not [n for n in nodes if "degradation" in n.lower()]
    assert not [t for t in bindings.topics if "degradation" in t.lower()]
    assert bindings.rejected == []
    assert forbidden("ns=2;s=KST.WELD.WELD-1.ABB-01.Degradation")
    assert forbidden("qost/v1/KST/WELD/WELD-1/ABB-01/degradation")
    assert not forbidden("ns=2;s=KST.WELD.WELD-1.ABB-01.State")


def test_a_mapped_oracle_node_is_rejected(config_copy: Path) -> None:
    demo = (REPO_ROOT / "config" / "tag_map.demo.yaml").read_text(encoding="utf-8")
    (config_copy / "tag_map.yaml").write_text(demo, encoding="utf-8")
    mutate(
        config_copy / "tag_map.yaml",
        '"ns=2;s=KST.WELD.WELD-1.ABB-01.joint_temp_c"',
        '"ns=2;s=KST.WELD.WELD-1.ABB-01.Degradation"',
    )
    cfg = load_config(config_copy)
    assert cfg.tag_map is not None
    bindings = compile_bindings(cfg, cfg.tag_map)
    assert bindings.rejected == ["ns=2;s=KST.WELD.WELD-1.ABB-01.Degradation"]
    assert "ns=2;s=KST.WELD.WELD-1.ABB-01.Degradation" not in subscription_node_ids(bindings)


def test_engine_never_reads_the_oracle() -> None:
    for path in _sources(ENGINE_SRC):
        assert "degradation" not in path.read_text(encoding="utf-8").lower(), path
