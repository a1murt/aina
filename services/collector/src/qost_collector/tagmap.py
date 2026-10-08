"""Tag map -> bindings (FR-ING-01): which OPC UA node / MQTT topic feeds which entity and signal.

Only the signals the pipeline needs are subscribed (``USED``); counters, ``last_*``,
``StateSince`` and ``Plant.*`` are mapped but unused. A node or topic whose last segment is the
simulator's hidden oracle (``Degradation``) is never bound, whatever the map says (FR-SIM-02,
T-RO).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from twin_core.config import TwinConfig
from twin_core.config.tag_map import FORBIDDEN_SIGNALS, TagMapConfig
from twin_core.uns import UNITS_TOPIC, signal_topic, units_topic

EQUIPMENT_USED = frozenset({"state", "alarm_code", "alarm"})
LINE_USED = frozenset({"state"})
BUFFER_USED = frozenset({"level", "capacity"})
PRODUCT_USED = frozenset({"kits"})


def forbidden(name: str) -> bool:
    """True for the hidden oracle in any spelling (``Degradation``, ``degradation``)."""
    last = name.replace("/", ".").rsplit(".", 1)[-1].strip().lower()
    return last in FORBIDDEN_SIGNALS


@dataclass(frozen=True, slots=True)
class Binding:
    key: str
    """OPC UA node id or MQTT topic."""
    kind: str
    """``equipment`` | ``line`` | ``buffer`` | ``product`` | ``site``."""
    code: str
    signal: str
    unit: str | None = None
    """Engineering unit of a telemetry signal (from the equipment type)."""

    @property
    def telemetry(self) -> bool:
        return self.unit is not None


@dataclass
class Bindings:
    nodes: dict[str, Binding] = field(default_factory=dict)
    topics: dict[str, Binding] = field(default_factory=dict)
    units_topics: dict[str, str] = field(default_factory=dict)
    """``…/{AREA}/{LINE}/units`` -> line."""
    unused: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _telemetry_units(cfg: TwinConfig) -> dict[tuple[str, str], str]:
    units: dict[tuple[str, str], str] = {}
    for code, eq in cfg.equipment.items():
        for sig in cfg.equipment_types[eq.type].signals:
            units[(code, sig.code)] = sig.unit
    return units


def used(kind: str, code: str, signal: str, tele: dict[tuple[str, str], str]) -> bool:
    if kind == "equipment":
        return signal in EQUIPMENT_USED or (code, signal) in tele
    if kind == "line":
        return signal in LINE_USED
    if kind == "buffer":
        return signal in BUFFER_USED
    if kind == "product":
        return signal in PRODUCT_USED
    return False


def compile_bindings(
    cfg: TwinConfig, tag_map: TagMapConfig, *, topic_root: str | None = None
) -> Bindings:
    root = (topic_root or tag_map.mqtt.topic_root).rstrip("/")
    tele = _telemetry_units(cfg)
    out = Bindings()
    for node in tag_map.opcua.nodes:
        kind, code = node.target
        if forbidden(node.node_id) or forbidden(node.signal):
            out.rejected.append(node.node_id)
            continue
        if not used(kind, code, node.signal, tele):
            out.unused.append(node.node_id)
            continue
        unit = tele.get((code, node.signal)) if kind == "equipment" else None
        out.nodes[node.node_id] = Binding(node.node_id, kind, code, node.signal, unit)
        topic = signal_topic(cfg, root, kind, code, node.signal)
        out.topics[topic] = Binding(topic, kind, code, node.signal, unit)
    for line in cfg.flow_lines:
        out.units_topics[units_topic(cfg, root, line)] = line
    return out


def subscription_node_ids(bindings: Bindings) -> list[str]:
    """Node ids the OPC UA source subscribes to (checked by T-RO: never Degradation)."""
    return list(bindings.nodes)


def is_units_topic(topic: str) -> bool:
    return topic.rsplit("/", 1)[-1] == UNITS_TOPIC


__all__ = [
    "Binding",
    "Bindings",
    "compile_bindings",
    "forbidden",
    "is_units_topic",
    "subscription_node_ids",
]
