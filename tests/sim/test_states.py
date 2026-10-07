"""Line state from its units and the material flow (SPEC §5.3)."""

from __future__ import annotations

import pytest

from qost_sim.model.states import UnitCondition, derive_line_state, equipment_state
from twin_core.domain import Criticality, EquipmentState

S = EquipmentState


def u(
    criticality: Criticality,
    state: EquipmentState = S.RUNNING,
    *,
    capacity: float | None = None,
    reason: str | None = None,
    since: float = 0.0,
) -> UnitCondition:
    default = {"A": 0.0, "B": 0.5, "C": 1.0}[criticality]
    return UnitCondition(
        criticality, default if capacity is None else capacity, state, reason, since
    )


@pytest.mark.parametrize(
    ("units", "flow", "expected", "capacity"),
    [
        ([u("A"), u("B"), u("C")], None, S.RUNNING, 1.0),
        ([u("A", S.DOWN_UNPLANNED, reason="ME-CHAIN"), u("B")], None, S.DOWN_UNPLANNED, 0.0),
        ([u("A", S.DOWN_PLANNED, reason="PM-CLEANING")], None, S.DOWN_PLANNED, 0.0),
        (
            [u("A", S.DOWN_PLANNED, reason="PM"), u("A", S.DOWN_UNPLANNED, reason="EL-DRIVE")],
            None,
            S.DOWN_UNPLANNED,
            0.0,
        ),
        ([u("A", S.CHANGEOVER)], None, S.CHANGEOVER, 1.0),
        ([u("A"), u("B", S.DOWN_UNPLANNED)], S.STARVED, S.STARVED, 0.5),
        ([u("A"), u("B")], S.BLOCKED, S.BLOCKED, 1.0),
        ([u("A"), u("B", S.DOWN_UNPLANNED)], None, S.DEGRADED, 0.5),
        ([u("B", S.DOWN_PLANNED)], None, S.DEGRADED, 0.5),
        (
            [u("B", S.DOWN_UNPLANNED, capacity=0.7), u("B", S.DOWN_UNPLANNED, capacity=0.4)],
            None,
            S.DEGRADED,
            0.4,
        ),
        ([u("A"), u("C", S.DOWN_UNPLANNED)], None, S.RUNNING, 1.0),
        ([u("A", S.DOWN_UNPLANNED), u("A", S.DOWN_UNPLANNED)], S.STARVED, S.DOWN_UNPLANNED, 0.0),
    ],
)
def test_priority(
    units: list[UnitCondition],
    flow: EquipmentState | None,
    expected: EquipmentState,
    capacity: float,
) -> None:
    status = derive_line_state(in_shift=True, units=units, flow=flow)
    assert status.state is expected
    assert status.capacity == pytest.approx(capacity)


def test_outside_shift_is_idle_whatever_the_units() -> None:
    status = derive_line_state(
        in_shift=False, units=[u("A", S.DOWN_UNPLANNED, reason="X")], flow=S.STARVED
    )
    assert (status.state, status.reason, status.capacity) == (S.IDLE_NO_PLAN, None, 0.0)


def test_reason_is_the_earliest_class_a_stop() -> None:
    units = [
        u("A", S.DOWN_UNPLANNED, reason="EL-DRIVE", since=20.0),
        u("A", S.DOWN_UNPLANNED, reason="ME-CHAIN", since=10.0),
        u("B", S.DOWN_UNPLANNED, reason="RB-TOOL", since=1.0),
    ]
    assert derive_line_state(in_shift=True, units=units).reason == "ME-CHAIN"
    starved = derive_line_state(
        in_shift=True, units=[u("A")], flow=S.STARVED, flow_reason="MAT-SHORTAGE"
    )
    assert starved.reason == "MAT-SHORTAGE"


def test_equipment_own_condition() -> None:
    assert equipment_state(in_shift=True, down=None) is S.RUNNING
    assert equipment_state(in_shift=False, down=None) is S.IDLE_NO_PLAN
    assert equipment_state(in_shift=False, down=S.DOWN_UNPLANNED) is S.DOWN_UNPLANNED
