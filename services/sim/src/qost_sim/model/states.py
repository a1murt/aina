"""Equipment and line state derivation (SPEC §5.3) — moved to :mod:`twin_core.states` in M3.

Re-exported here so the simulator keeps its import paths; the engine derives line states with
the same functions when a source does not publish them.
"""

from twin_core.states import (
    DOWN_STATES,
    LineStatus,
    UnitCondition,
    derive_line_state,
    equipment_state,
)

__all__ = ["DOWN_STATES", "LineStatus", "UnitCondition", "derive_line_state", "equipment_state"]
