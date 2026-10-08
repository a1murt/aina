"""twin_core.uns derives exactly the MQTT topics the simulator publishes (collector mapping)."""

from __future__ import annotations

from qost_sim.address_space import build_address_space
from twin_core.config import TwinConfig
from twin_core.uns import signal_topic, target_path, units_topic


def test_topics_match_the_simulator(cfg: TwinConfig) -> None:
    space = build_address_space(cfg)
    root = "qost/v1/KST"
    for var in space.variables:
        assert space.topic(var, root) == signal_topic(cfg, root, var.kind, var.target, var.signal)
    for line in cfg.flow_lines:
        assert space.units_topic(cfg, line, root) == units_topic(cfg, root, line)
    assert target_path(cfg, "site", "KST") == ("Plant",)
