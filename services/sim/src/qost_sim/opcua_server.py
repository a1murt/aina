"""asyncua OPC UA server of the virtual plant (SPEC §6.7).

Every value is written with ``SourceTimestamp`` = plant time of the change, so a client (the
collector) gets event times in plant time regardless of the wall clock. Nodes are read-only for
clients; only this process writes (rule 4: no writes outside ``services/sim``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, cast

from asyncua import ua
from asyncua.common.node import Node
from asyncua.server.server import Server
from asyncua.ua.attribute_ids import AttributeIds

from qost_sim.address_space import (
    HIDDEN_DESCRIPTION,
    NAMESPACE_INDEX,
    AddressSpace,
    VariableSpec,
)
from qost_sim.model import PlantModel, Rec
from qost_sim.model.records import ORACLE
from twin_core.domain import EquipmentState

_VARIANT = {
    "Int32": ua.VariantType.Int32,
    "UInt32": ua.VariantType.UInt32,
    "Double": ua.VariantType.Double,
    "String": ua.VariantType.String,
    "Boolean": ua.VariantType.Boolean,
    "DateTime": ua.VariantType.DateTime,
}
_UNITS_NS = "http://www.opcfoundation.org/UA/units/un/cefact"

for _name in ("asyncua", "asyncua.server", "asyncua.client"):
    logging.getLogger(_name).setLevel(logging.ERROR)


class OpcUaError(RuntimeError):
    pass


def _nid(ident: str, ns: int) -> ua.NodeId:
    return ua.NodeId(ua.String(ident), ua.Int16(ns))


def _dt(value: datetime) -> ua.DateTime:
    return cast(ua.DateTime, value)


def _default(var: VariableSpec, start: datetime) -> Any:
    return {
        "Int32": 0,
        "UInt32": 0,
        "Double": 0.0,
        "String": "",
        "Boolean": False,
        "DateTime": start,
    }[var.datatype]


class OpcUaServer:
    def __init__(
        self,
        space: AddressSpace,
        *,
        endpoint: str,
        namespace_uri: str,
        state_codes: Mapping[EquipmentState, int],
        server_name: str = "Aina virtual plant",
    ) -> None:
        self.space = space
        self.endpoint = endpoint
        self.namespace_uri = namespace_uri
        self.state_codes = state_codes
        self.server_name = server_name
        self.server: Server | None = None
        self._vars = space.by_target()
        self._nodeids: dict[str, ua.NodeId] = {}
        self.writes = 0

    # ------------------------------------------------------------------ lifecycle

    async def start(self, start_time: datetime) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name(self.server_name)
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        idx = await server.register_namespace(self.namespace_uri)
        if idx != NAMESPACE_INDEX:
            raise OpcUaError(f"namespace {self.namespace_uri} got index {idx}, expected 2")
        parents: dict[tuple[str, ...], Node] = {}
        root = server.nodes.objects
        for obj in self.space.objects:
            parent = parents.get(obj.path[:-1], root)
            parents[obj.path] = await parent.add_object(
                _nid(obj.ident, idx), ua.QualifiedName(obj.path[-1], idx)
            )
        for var in self.space.variables:
            parent = parents[var.path[:-1]]
            nodeid = _nid(var.ident, idx)
            node = await parent.add_variable(
                nodeid,
                ua.QualifiedName(var.path[-1], idx),
                ua.Variant(_default(var, start_time), _VARIANT[var.datatype]),
            )
            # initial value stamped with plant time, not the server's wall clock
            await server.write_attribute_value(
                nodeid,
                ua.DataValue(
                    ua.Variant(_default(var, start_time), _VARIANT[var.datatype]),
                    SourceTimestamp=_dt(start_time),
                    ServerTimestamp=_dt(start_time),
                ),
            )
            self._nodeids[var.ident] = nodeid
            if var.hidden:
                await node.write_attribute(
                    AttributeIds.Description,
                    ua.DataValue(
                        ua.Variant(
                            ua.LocalizedText(HIDDEN_DESCRIPTION), ua.VariantType.LocalizedText
                        )
                    ),
                )
            if var.lo is not None and var.hi is not None:
                await node.add_property(
                    _nid(f"{var.ident}.EURange", idx),
                    ua.QualifiedName("EURange", 0),
                    ua.Range(Low=var.lo, High=var.hi),
                )
            if var.unit is not None:
                await node.add_property(
                    _nid(f"{var.ident}.EngineeringUnits", idx),
                    ua.QualifiedName("EngineeringUnits", 0),
                    ua.EUInformation(
                        NamespaceUri=_UNITS_NS,
                        UnitId=-1,
                        DisplayName=ua.LocalizedText(var.unit),
                        Description=ua.LocalizedText(var.unit),
                    ),
                )
        await server.start()
        self.server = server

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None

    # ------------------------------------------------------------------ writes

    async def write(self, kind: str, target: str, signal: str, value: Any, ts: datetime) -> None:
        var = self._vars.get((kind, target, signal))
        server = self.server
        if var is None or server is None:
            return
        if var.datatype == "Int32" and isinstance(value, EquipmentState):
            value = self.state_codes[value]
        dv = ua.DataValue(
            ua.Variant(value, _VARIANT[var.datatype]),
            SourceTimestamp=_dt(ts),
            ServerTimestamp=_dt(ts),
        )
        await server.write_attribute_value(self._nodeids[var.ident], dv)
        self.writes += 1

    async def write_clock(self, site: str, plant_time: datetime, shift: str, speed: float) -> None:
        await self.write("site", site, "clock", plant_time, plant_time)
        await self.write("site", site, "shift", shift, plant_time)
        await self.write("site", site, "speed", float(speed), plant_time)

    async def write_snapshot(self, model: PlantModel, ts: datetime) -> None:
        """(Re)write every value from the model state with SourceTimestamp ``ts``."""
        snap = model.snapshot()
        for code, u in snap["units"].items():
            await self.write("equipment", code, "state", u["state"], ts)
            await self.write("equipment", code, "state_since", model.at(u["since"]), ts)
            await self.write("equipment", code, "alarm_code", u["reason"] or "", ts)
            await self.write("equipment", code, "alarm", bool(u["alarm"]), ts)
            if u["degradation"] is not None:
                await self.write("equipment", code, "degradation", float(u["degradation"]), ts)
            for signal, value in u["values"].items():
                await self.write("equipment", code, signal, float(value), ts)
        for code, ln in snap["lines"].items():
            await self.write("line", code, "state", ln["state"], ts)
            await self.write("line", code, "good_count", ln["good"], ts)
            await self.write("line", code, "reject_count", ln["reject"], ts)
            await self.write("line", code, "produced_count", ln["produced"], ts)
            await self.write("line", code, "last_body_id", ln["last_body"], ts)
            await self.write("line", code, "last_product", ln["last_product"], ts)
            await self.write("line", code, "cycle_time_s", float(ln["cycle_time_s"]), ts)
        for code, b in snap["buffers"].items():
            await self.write("buffer", code, "level", b["level"], ts)
            await self.write("buffer", code, "capacity", b["capacity"], ts)
        for product, kits in snap["kits"].items():
            await self.write("product", product, "kits", max(0, kits), ts)

    async def write_records(self, model: PlantModel, records: Iterable[Rec]) -> None:
        """Apply the model's changes to the address space, in order."""
        for rec in records:
            ts = model.at(rec.t)
            data = rec.data
            kind = rec.kind
            if kind == "state":
                state = EquipmentState(data["state"])
                await self.write(rec.entity_type, rec.entity, "state", state, ts)
                if rec.entity_type == "equipment":
                    await self.write("equipment", rec.entity, "state_since", ts, ts)
                    await self.write(
                        "equipment", rec.entity, "alarm_code", data.get("alarm_code") or "", ts
                    )
            elif kind == "alarm":
                await self.write("equipment", rec.entity, "alarm", bool(data["active"]), ts)
            elif kind == "telemetry":
                await self.write("equipment", rec.entity, data["signal"], float(data["value"]), ts)
            elif kind == ORACLE:
                await self.write(
                    "equipment", rec.entity, "degradation", float(data["degradation"]), ts
                )
            elif kind == "unit" and rec.extra is not None:
                line = rec.entity
                extra = rec.extra
                await self.write("line", line, "produced_count", int(extra["produced"]), ts)
                await self.write("line", line, "good_count", int(extra["good"]), ts)
                await self.write("line", line, "reject_count", int(extra["reject"]), ts)
                if data["result"] != "rework_pass":
                    await self.write("line", line, "last_body_id", data["body_id"], ts)
                    await self.write("line", line, "last_product", data["product"], ts)
                    await self.write("line", line, "cycle_time_s", float(extra["cycle_time_s"]), ts)
            elif kind == "buffer_level":
                await self.write("buffer", rec.entity, "level", int(data["level"]), ts)
            elif kind == "ckd":
                await self.write("product", rec.entity, "kits", max(0, int(data["kits"])), ts)
