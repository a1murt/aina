"""Shared domain vocabulary (SPEC §5.3, §7.2, §9.7).

These are contract-level enumerations (protocol and ISO 22400 vocabulary), not plant constants:
plant-specific codes (lines, equipment, reasons, ...) live only in config/*.yaml.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class EquipmentState(StrEnum):
    """Equipment / line state (SPEC §5.3)."""

    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STARVED = "STARVED"
    BLOCKED = "BLOCKED"
    DOWN_UNPLANNED = "DOWN_UNPLANNED"
    DOWN_PLANNED = "DOWN_PLANNED"
    CHANGEOVER = "CHANGEOVER"
    IDLE_NO_PLAN = "IDLE_NO_PLAN"


Criticality = Literal["A", "B", "C"]
"""Equipment criticality class (SPEC §5.7)."""

Severity = Literal["info", "warning", "critical"]
"""Alert / data-quality severity (SPEC §9.6, §9.7)."""

Channel = Literal["ui", "telegram"]
"""Alert delivery channel (rules.yaml)."""

AreaKind = Literal["storage", "production"]

PlanLevel = Literal["plant_target", "line_model"]

Disposition = Literal["rework", "scrap"]

ReasonBucket = Literal["own", "external"]

FilterPolicy = Literal["on_limit", "predictive_shift_change"]
