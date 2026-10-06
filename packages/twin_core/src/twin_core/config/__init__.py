"""Plant configuration: pydantic models for ``config/*.yaml``, loader and cross-checks.

Usage::

    from twin_core.config import load_config

    cfg = load_config()                 # PLANT_CONFIG_DIR or the repo config/ directory
    cfg.lines["WELD-1"].ict_seconds
    cfg.aliases.equipment.require("Камера-02")   # -> "BOOTH-02"

Any problem raises :class:`ConfigError` listing every issue with file, line, YAML path, the bad
value and, for unknown codes, the closest valid code (FR-DOM-01).
"""

from twin_core.config.business import BusinessConfig
from twin_core.config.defects import ANY_AREA, DefectCode, DefectCodesConfig
from twin_core.config.errors import ConfigError, ConfigIssue
from twin_core.config.loader import (
    CONFIG_FILES,
    TAG_MAP_CANDIDATES,
    Aliases,
    TwinConfig,
    load_config,
    load_config_from_settings,
    load_tag_map,
)
from twin_core.config.plant import (
    Area,
    Buffer,
    Calendar,
    Equipment,
    EquipmentType,
    Line,
    PlanEntry,
    PlantConfig,
    Product,
    ShiftDef,
    Signal,
)
from twin_core.config.reasons import FALLBACK_REASON_CODE, Reason, ReasonCodesConfig
from twin_core.config.rules import AlertRule, DqRule, RulesConfig, Thresholds
from twin_core.config.simulation import Scenario, SimulationConfig
from twin_core.config.tag_map import TagMapConfig, TagNode

__all__ = [
    "ANY_AREA",
    "CONFIG_FILES",
    "FALLBACK_REASON_CODE",
    "TAG_MAP_CANDIDATES",
    "AlertRule",
    "Aliases",
    "Area",
    "Buffer",
    "BusinessConfig",
    "Calendar",
    "ConfigError",
    "ConfigIssue",
    "DefectCode",
    "DefectCodesConfig",
    "DqRule",
    "Equipment",
    "EquipmentType",
    "Line",
    "PlanEntry",
    "PlantConfig",
    "Product",
    "Reason",
    "ReasonCodesConfig",
    "RulesConfig",
    "Scenario",
    "ShiftDef",
    "Signal",
    "SimulationConfig",
    "TagMapConfig",
    "TagNode",
    "Thresholds",
    "TwinConfig",
    "load_config",
    "load_config_from_settings",
    "load_tag_map",
]
