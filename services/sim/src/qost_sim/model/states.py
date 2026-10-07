"""Equipment and line state derivation (SPEC §5.3) — pure functions, no SimPy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from twin_core.domain import Criticality, EquipmentState

DOWN_STATES = frozenset({EquipmentState.DOWN_UNPLANNED, EquipmentState.DOWN_PLANNED})


@dataclass(frozen=True, slots=True)
class UnitCondition:
    """What the line needs to know about one of its units."""

    criticality: Criticality
    degraded_capacity: float
    state: EquipmentState
    reason: str | None = None
    since: float = 0.0


@dataclass(frozen=True, slots=True)
class LineStatus:
    state: EquipmentState
    reason: str | None
    capacity: float
    """Fraction of the ideal rate the line runs at now (0 when it cannot produce)."""


def equipment_state(*, in_shift: bool, down: EquipmentState | None) -> EquipmentState:
    """Own condition of a unit: down state > outside shift > running."""
    if down is not None:
        return down
    return EquipmentState.RUNNING if in_shift else EquipmentState.IDLE_NO_PLAN


def _earliest_reason(units: Sequence[UnitCondition], state: EquipmentState) -> str | None:
    matching = [u for u in units if u.state is state and u.criticality == "A"]
    return min(matching, key=lambda u: u.since).reason if matching else None


def derive_line_state(
    *,
    in_shift: bool,
    units: Sequence[UnitCondition],
    flow: EquipmentState | None = None,
    flow_reason: str | None = None,
) -> LineStatus:
    """Line state from its units and the material flow (SPEC §5.3 priority order).

    ``flow`` is ``STARVED`` (input empty / no kits) or ``BLOCKED`` (output full) or ``None``.
    Capacity = min ``degraded_capacity`` over down class A/B units (class C never matters).
    """
    if not in_shift:
        return LineStatus(EquipmentState.IDLE_NO_PLAN, None, 0.0)
    capacity = 1.0
    for unit in units:
        if unit.criticality != "C" and unit.state in DOWN_STATES:
            capacity = min(capacity, unit.degraded_capacity)
    for down in (EquipmentState.DOWN_UNPLANNED, EquipmentState.DOWN_PLANNED):
        if any(u.criticality == "A" and u.state is down for u in units):
            return LineStatus(down, _earliest_reason(units, down), capacity)
    if any(u.criticality == "A" and u.state is EquipmentState.CHANGEOVER for u in units):
        return LineStatus(EquipmentState.CHANGEOVER, None, capacity)
    if flow is not None:
        return LineStatus(flow, flow_reason, capacity)
    if any(u.criticality == "B" and u.state not in (EquipmentState.RUNNING,) for u in units):
        return LineStatus(EquipmentState.DEGRADED, None, capacity)
    return LineStatus(EquipmentState.RUNNING, None, capacity)
