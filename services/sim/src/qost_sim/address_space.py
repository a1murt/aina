"""OPC UA address space (SPEC §6.7) and MQTT topics (§6.8), derived from the plant config.

One pure description (:class:`AddressSpace`) feeds both the asyncua server and ``make tagmap``,
so the generated ``tag_map.demo.yaml`` is exactly what the server publishes (minus the hidden
``Degradation`` oracle, FR-SIM-02).

NodeIds are strings in the plant namespace: ``ns=2;s=KST.{AREA}.{LINE}.{EQUIPMENT}.{VARIABLE}``;
buffers ``KST.BUFFERS.{CODE}.Level``, CKD stock ``KST.CKD.{PRODUCT}.Kits``, plant
``KST.Plant.Clock|Shift|Speed``. MQTT topics mirror the path with snake_case signal names:
``qost/v1/KST/{AREA}/{LINE}/{EQUIPMENT}/{signal}``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from twin_core.config import TwinConfig, load_tag_map
from twin_core.config.tag_map import TagMapConfig
from twin_core.domain import EquipmentState

NAMESPACE_INDEX = 2
"""The plant namespace is the first one registered after the server's own (ns=1)."""

BUFFERS_FOLDER = "BUFFERS"
PLANT_FOLDER = "Plant"
UNITS_TOPIC = "units"
EXAMPLE_TAG_MAP = "tag_map.example.yaml"

HIDDEN_DESCRIPTION = (
    "Скрытый износ симулятора (оракул). Только демо и тесты; не использовать в аналитике "
    "(FR-SIM-02) / hidden simulator wear oracle, never used by analytics"
)

# OPC UA variable name -> tag-map signal name
EQUIPMENT_VARS = {
    "State": "state",
    "StateSince": "state_since",
    "AlarmCode": "alarm_code",
    "Alarm": "alarm",
}
LINE_VARS = {
    "State": "state",
    "GoodCount": "good_count",
    "RejectCount": "reject_count",
    "ProducedCount": "produced_count",
    "LastBodyId": "last_body_id",
    "LastProduct": "last_product",
    "CycleTime_s": "cycle_time_s",
}
BUFFER_VARS = {"Level": "level", "Capacity": "capacity"}
PRODUCT_VARS = {"Kits": "kits"}
SITE_VARS = {"Clock": "clock", "Shift": "shift", "Speed": "speed"}
DEGRADATION = ("Degradation", "degradation")

_TYPES = {
    "state": "Int32",
    "state_since": "DateTime",
    "alarm_code": "String",
    "alarm": "Boolean",
    "good_count": "UInt32",
    "reject_count": "UInt32",
    "produced_count": "UInt32",
    "last_body_id": "String",
    "last_product": "String",
    "cycle_time_s": "Double",
    "level": "UInt32",
    "capacity": "UInt32",
    "kits": "UInt32",
    "clock": "DateTime",
    "shift": "String",
    "speed": "Double",
    "degradation": "Double",
}


@dataclass(frozen=True, slots=True)
class ObjectSpec:
    ident: str
    path: tuple[str, ...]
    """Browse names from ``Objects`` (without it)."""


@dataclass(frozen=True, slots=True)
class VariableSpec:
    ident: str
    """String identifier of the NodeId (``KST.ASSY.ASSY-1.CONV-03.State``)."""
    path: tuple[str, ...]
    kind: str
    """equipment | line | buffer | product | site"""
    target: str
    """Entity code."""
    signal: str
    """Tag-map signal name (``state``, ``vibration_mm_s``, ``degradation``)."""
    datatype: str
    """Int32 | UInt32 | Double | String | Boolean | DateTime"""
    unit: str | None = None
    lo: float | None = None
    hi: float | None = None
    hidden: bool = False

    @property
    def node_id(self) -> str:
        return f"ns={NAMESPACE_INDEX};s={self.ident}"

    @property
    def is_telemetry(self) -> bool:
        return self.unit is not None


@dataclass(frozen=True)
class AddressSpace:
    site: str
    objects: tuple[ObjectSpec, ...]
    variables: tuple[VariableSpec, ...]

    def by_target(self) -> dict[tuple[str, str, str], VariableSpec]:
        """(kind, code, signal) -> variable."""
        return {(v.kind, v.target, v.signal): v for v in self.variables}

    def topic(self, var: VariableSpec, topic_root: str) -> str:
        return "/".join((topic_root, *var.path[1:-1], var.signal))

    def units_topic(self, cfg: TwinConfig, line: str, topic_root: str) -> str:
        return "/".join((topic_root, cfg.area_of_line(line).code, line, UNITS_TOPIC))


def build_address_space(cfg: TwinConfig) -> AddressSpace:
    site = cfg.plant.site.code
    objects: list[ObjectSpec] = [ObjectSpec(site, (site,))]
    variables: list[VariableSpec] = []
    wear_types = set(cfg.simulation.degradation.per_type)

    def var(path: tuple[str, ...], kind: str, target: str, name: str, signal: str) -> None:
        variables.append(
            VariableSpec(
                ".".join((*path, name)), (*path, name), kind, target, signal, _TYPES[signal]
            )
        )

    for area in cfg.plant.areas:
        area_path = (site, area.code)
        objects.append(ObjectSpec(".".join(area_path), area_path))
        for line in area.lines:
            line_path = (*area_path, line.code)
            objects.append(ObjectSpec(".".join(line_path), line_path))
            for name, signal in LINE_VARS.items():
                var(line_path, "line", line.code, name, signal)
            for eq in line.equipment:
                eq_path = (*line_path, eq.code)
                objects.append(ObjectSpec(".".join(eq_path), eq_path))
                for name, signal in EQUIPMENT_VARS.items():
                    var(eq_path, "equipment", eq.code, name, signal)
                if eq.type in wear_types:
                    name, signal = DEGRADATION
                    variables.append(
                        VariableSpec(
                            ".".join((*eq_path, name)),
                            (*eq_path, name),
                            "equipment",
                            eq.code,
                            signal,
                            "Double",
                            lo=0.0,
                            hi=1.0,
                            hidden=True,
                        )
                    )
                modelled = cfg.simulation.telemetry.per_type.get(eq.type, {})
                for sig in cfg.equipment_types[eq.type].signals:
                    if sig.code not in modelled:
                        continue
                    variables.append(
                        VariableSpec(
                            ".".join((*eq_path, sig.code)),
                            (*eq_path, sig.code),
                            "equipment",
                            eq.code,
                            sig.code,
                            "Double",
                            unit=sig.unit,
                            lo=sig.lo,
                            hi=sig.hi,
                        )
                    )
    ckd_area = next((a.code for a in cfg.plant.areas if a.kind == "storage"), "CKD")
    for product in cfg.plant.products:
        path = (site, ckd_area, product.code)
        objects.append(ObjectSpec(".".join(path), path))
        for name, signal in PRODUCT_VARS.items():
            var(path, "product", product.code, name, signal)
    objects.append(ObjectSpec(f"{site}.{BUFFERS_FOLDER}", (site, BUFFERS_FOLDER)))
    for buffer in cfg.plant.buffers:
        path = (site, BUFFERS_FOLDER, buffer.code)
        objects.append(ObjectSpec(".".join(path), path))
        for name, signal in BUFFER_VARS.items():
            var(path, "buffer", buffer.code, name, signal)
    plant_path = (site, PLANT_FOLDER)
    objects.append(ObjectSpec(".".join(plant_path), plant_path))
    for name, signal in SITE_VARS.items():
        var(plant_path, "site", site, name, signal)
    return AddressSpace(site, tuple(objects), tuple(variables))


# --------------------------------------------------------------------------- contract


def load_contract(cfg: TwinConfig) -> TagMapConfig:
    """``tag_map.example.yaml``: namespace URI, state enum, publishing interval, topic root."""
    return load_tag_map(cfg.config_dir / EXAMPLE_TAG_MAP, cfg.plant)


def state_codes(contract: TagMapConfig) -> Mapping[EquipmentState, int]:
    """Model state -> raw Int32 of the ``State`` tag."""
    return {state: raw for raw, state in contract.opcua.state_enum.items()}


def render_tag_map(
    cfg: TwinConfig,
    space: AddressSpace,
    *,
    endpoint: str,
    mqtt_url: str,
    contract: TagMapConfig | None = None,
) -> str:
    """YAML text of ``tag_map.demo.yaml`` (deterministic; Degradation excluded)."""
    base = contract or load_contract(cfg)
    op, mq = base.opcua, base.mqtt
    out = [
        "# GENERATED by `make tagmap` (python -m qost_sim tagmap) from the virtual plant's",
        "# OPC UA address space — do not edit. Pilot: fill config/tag_map.yaml instead",
        "# (SPEC §6.7).",
        "# The hidden Degradation oracle is intentionally absent (FR-SIM-02).",
        "version: 1",
        "opcua:",
        f'  endpoint: "{endpoint}"',
        f"  security: {{ mode: {op.security.mode}, policy: {op.security.policy} }}",
        f'  namespace_uri: "{op.namespace_uri}"',
        f"  publishing_interval_ms: {op.publishing_interval_ms:g}",
        "  nodes:",
    ]
    for v in space.variables:
        if v.hidden:
            continue
        out.append(f'    - {{ node_id: "{v.node_id}", {v.kind}: {v.target}, signal: {v.signal} }}')
    out.append("  state_enum:")
    for raw, state in sorted(op.state_enum.items()):
        out.append(f"    {raw}: {state.value}")
    out += [
        "mqtt:",
        f'  url: "{mqtt_url}"',
        f'  topic_root: "{mq.topic_root}"',
        f"  payload: {mq.payload}",
        "",
    ]
    return "\n".join(out)


def write_tag_map(cfg: TwinConfig, path: Path, *, endpoint: str, mqtt_url: str) -> TagMapConfig:
    """Render, validate against the plant model and write the demo tag map."""
    text = render_tag_map(cfg, build_address_space(cfg), endpoint=endpoint, mqtt_url=mqtt_url)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tag_map = load_tag_map(tmp, cfg.plant)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    return tag_map
