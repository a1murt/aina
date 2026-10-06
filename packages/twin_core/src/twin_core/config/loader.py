"""Load and validate ``config/*.yaml`` into one typed :class:`TwinConfig` (FR-DOM-01/02)."""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from twin_core.aliases import AliasIndex, closest_code
from twin_core.config.business import BusinessConfig
from twin_core.config.crossref import (
    BUSINESS,
    DEFECTS,
    PLANT,
    REASONS,
    RULES,
    SIMULATION,
    TAG_MAP,
    Models,
    check_references,
    check_tag_map_only,
)
from twin_core.config.defects import DefectCode, DefectCodesConfig
from twin_core.config.errors import ConfigError, ConfigIssue
from twin_core.config.plant import (
    Area,
    Buffer,
    Equipment,
    EquipmentType,
    Line,
    PlantConfig,
    Product,
)
from twin_core.config.reasons import Reason, ReasonCodesConfig
from twin_core.config.rules import AlertRule, DqRule, RulesConfig
from twin_core.config.simulation import Scenario, SimulationConfig
from twin_core.config.source import YamlSource, read_yaml
from twin_core.config.tag_map import TagMapConfig

if TYPE_CHECKING:
    from twin_core.calendar import PlantCalendar
    from twin_core.settings import TwinSettings

CONFIG_FILES: Mapping[str, str] = MappingProxyType(
    {
        PLANT: "plant.yaml",
        RULES: "rules.yaml",
        REASONS: "reason_codes.yaml",
        DEFECTS: "defect_codes.yaml",
        SIMULATION: "simulation.yaml",
        BUSINESS: "business.yaml",
    }
)
TAG_MAP_CANDIDATES: tuple[str, ...] = ("tag_map.yaml", "tag_map.demo.yaml")
"""Searched in this order when no tag map is given explicitly (pilot file wins over demo)."""

_ROOT_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        PLANT: PlantConfig,
        RULES: RulesConfig,
        REASONS: ReasonCodesConfig,
        DEFECTS: DefectCodesConfig,
        SIMULATION: SimulationConfig,
        BUSINESS: BusinessConfig,
        TAG_MAP: TagMapConfig,
    }
)


# --------------------------------------------------------------------------- pydantic errors


def _unwrap(tp: Any) -> Any:
    while typing.get_origin(tp) is typing.Annotated:
        tp = typing.get_args(tp)[0]
    return tp


def _known_keys(root: type[BaseModel], loc: Sequence[str | int]) -> list[str] | None:
    """Field names allowed at ``loc`` (to suggest a fix for an unknown key)."""
    tp: Any = root
    parts = list(loc)
    while parts:
        part = parts.pop(0)
        tp = _unwrap(tp)
        origin = typing.get_origin(tp)
        if origin in (typing.Union, types.UnionType):
            members = [_unwrap(a) for a in typing.get_args(tp)]
            chosen = None
            for member in members:
                if isinstance(member, type) and issubclass(member, BaseModel):
                    tag = member.model_fields.get("type")
                    if tag is not None and part in typing.get_args(_unwrap(tag.annotation)):
                        chosen = member
            if chosen is None:
                return None
            tp = chosen
            continue
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            field = next(
                (f for name, f in tp.model_fields.items() if (f.alias or name) == part), None
            )
            if field is not None:
                tp = field.annotation
                continue
            extra = tp.__annotations__.get("__pydantic_extra__")
            if extra is None:
                return None
            hints = typing.get_type_hints(tp)
            tp = typing.get_args(hints["__pydantic_extra__"])[1]
            continue
        if origin in (list, tuple) and isinstance(part, int):
            tp = typing.get_args(tp)[0]
            continue
        if origin is dict:
            tp = typing.get_args(tp)[1]
            continue
        return None
    tp = _unwrap(tp)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return [f.alias or name for name, f in tp.model_fields.items()]
    return None


def _pydantic_issues(
    source: YamlSource, root: type[BaseModel], error: ValidationError
) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for err in error.errors(include_url=False):
        loc = tuple(err["loc"])
        kind = err["type"]
        message = str(err["msg"])
        for prefix in ("Value error, ", "Assertion failed, "):
            message = message.removeprefix(prefix)
        value = err.get("input")
        suggestion: str | None = None
        if kind == "missing":
            message = f"required key '{loc[-1]}' is missing"
            value = None
        elif kind == "extra_forbidden":
            message = f"unknown key '{loc[-1]}'"
            known = _known_keys(root, loc[:-1])
            if known:
                suggestion = closest_code(str(loc[-1]), known)
                if suggestion is None:
                    message += f" (allowed: {', '.join(known)})"
        elif not isinstance(value, dict | list):
            message += f" (got {value!r})"
        issues.append(source.issue(loc, message, value=value, suggestion=suggestion))
    return issues


# --------------------------------------------------------------------------- TwinConfig


class Aliases:
    """Per-kind external name -> code indexes (FR-DOM-02)."""

    def __init__(self, plant: PlantConfig, reasons: ReasonCodesConfig, defects: DefectCodesConfig):
        self.areas = AliasIndex("area")
        self.lines = AliasIndex("line")
        self.equipment = AliasIndex("equipment")
        self.buffers = AliasIndex("buffer")
        self.products = AliasIndex("product")
        self.reasons = AliasIndex("reason")
        self.defects = AliasIndex("defect")
        for area in plant.areas:
            self.areas.add(area.code, area.name_ru, area.name_kk, *area.aliases)
            for line in area.lines:
                self.lines.add(line.code, line.name_ru, line.name_kk, *line.aliases)
                for eq in line.equipment:
                    self.equipment.add(eq.code, eq.name_ru, eq.name_kk, *eq.aliases)
        for buffer in plant.buffers:
            self.buffers.add(buffer.code, buffer.name_ru, buffer.name_kk, *buffer.aliases)
        for product in plant.products:
            self.products.add(product.code, product.name, *product.aliases)
        for reason in reasons.reasons:
            self.reasons.add(reason.code, reason.name_ru, reason.name_kk, *reason.aliases)
        for defect in defects.defects:
            self.defects.add(defect.code, defect.name_ru, defect.name_kk, *defect.aliases)

    def by_kind(self) -> dict[str, AliasIndex]:
        return {
            "area": self.areas,
            "line": self.lines,
            "equipment": self.equipment,
            "buffer": self.buffers,
            "product": self.products,
            "reason": self.reasons,
            "defect": self.defects,
        }

    def resolve_asset(self, name: str) -> tuple[str, str] | None:
        """Resolve a name to ``(kind, code)`` over areas, lines, equipment and buffers."""
        for kind, index in (
            ("equipment", self.equipment),
            ("line", self.lines),
            ("area", self.areas),
            ("buffer", self.buffers),
        ):
            code = index.resolve(name)
            if code is not None:
                return kind, code
        return None


class TwinConfig:
    """All validated plant configuration plus lookup helpers by code."""

    def __init__(
        self,
        config_dir: Path,
        *,
        plant: PlantConfig,
        rules: RulesConfig,
        reasons: ReasonCodesConfig,
        defects: DefectCodesConfig,
        simulation: SimulationConfig,
        business: BusinessConfig,
        tag_map: TagMapConfig | None = None,
        tag_map_path: Path | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.plant = plant
        self.rules = rules
        self.reason_codes = reasons
        self.defect_codes = defects
        self.simulation = simulation
        self.business = business
        self.tag_map = tag_map
        self.tag_map_path = tag_map_path

        line_area: dict[str, str] = {}
        equipment_line: dict[str, str] = {}
        lines: dict[str, Line] = {}
        equipment: dict[str, Equipment] = {}
        for area in plant.areas:
            for line in area.lines:
                lines[line.code] = line
                line_area[line.code] = area.code
                for eq in line.equipment:
                    equipment[eq.code] = eq
                    equipment_line[eq.code] = line.code

        self.areas: Mapping[str, Area] = MappingProxyType({a.code: a for a in plant.areas})
        self.lines: Mapping[str, Line] = MappingProxyType(lines)
        self.equipment: Mapping[str, Equipment] = MappingProxyType(equipment)
        self.buffers: Mapping[str, Buffer] = MappingProxyType({b.code: b for b in plant.buffers})
        self.products: Mapping[str, Product] = MappingProxyType({p.code: p for p in plant.products})
        self.equipment_types: Mapping[str, EquipmentType] = MappingProxyType(
            dict(plant.equipment_types)
        )
        self.reasons: Mapping[str, Reason] = MappingProxyType({r.code: r for r in reasons.reasons})
        self.defects: Mapping[str, DefectCode] = MappingProxyType(
            {d.code: d for d in defects.defects}
        )
        self.alert_rules: Mapping[str, AlertRule] = MappingProxyType(
            {r.id: r for r in rules.alert_rules}
        )
        self.dq_rules: Mapping[str, DqRule] = MappingProxyType(
            {r.id: r for r in rules.data_quality_rules}
        )
        self.scenarios: Mapping[str, Scenario] = MappingProxyType(
            {s.id: s for s in simulation.scenarios}
        )
        self.flow_lines: tuple[str, ...] = tuple(lines)
        """Line codes in material-flow order (order of areas, then lines, in plant.yaml)."""
        self._line_area = line_area
        self._equipment_line = equipment_line
        self.aliases = Aliases(plant, reasons, defects)

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.plant.site.timezone)

    @cached_property
    def calendar(self) -> PlantCalendar:
        from twin_core.calendar import PlantCalendar

        return PlantCalendar.from_plant(self.plant)

    def area_of_line(self, line_code: str) -> Area:
        return self.areas[self._line_area[line_code]]

    def line_of_equipment(self, equipment_code: str) -> Line:
        return self.lines[self._equipment_line[equipment_code]]

    def area_of_equipment(self, equipment_code: str) -> Area:
        return self.area_of_line(self._equipment_line[equipment_code])

    def equipment_of_line(self, line_code: str) -> tuple[Equipment, ...]:
        return tuple(self.lines[line_code].equipment)

    def __repr__(self) -> str:
        return (
            f"TwinConfig({self.config_dir}, site={self.plant.site.code}, "
            f"areas={len(self.areas)}, lines={len(self.lines)}, "
            f"equipment={len(self.equipment)}, buffers={len(self.buffers)}, "
            f"tag_map={self.tag_map_path.name if self.tag_map_path else None})"
        )


# --------------------------------------------------------------------------- loading


def _resolve_tag_map(config_dir: Path, tag_map: Path | str | Literal[False] | None) -> Path | None:
    if tag_map is False:
        return None
    if tag_map is None:
        for name in TAG_MAP_CANDIDATES:
            candidate = config_dir / name
            if candidate.is_file():
                return candidate
        return None
    path = Path(tag_map)
    return path if path.is_absolute() else config_dir / path


def load_config(
    config_dir: Path | str | None = None,
    *,
    tag_map: Path | str | Literal[False] | None = None,
) -> TwinConfig:
    """Load, validate and cross-check every config file.

    Args:
        config_dir: directory with the YAML files; default: ``PLANT_CONFIG_DIR`` / repo ``config``.
        tag_map: ``None`` — auto-detect (:data:`TAG_MAP_CANDIDATES`, may be absent);
            a file name (relative to ``config_dir``) or path — load exactly that file;
            ``False`` — do not load a tag map.

    Raises:
        ConfigError: with every problem found (syntax, schema, broken references).
    """
    if config_dir is None:
        from twin_core.settings import TwinSettings

        config_dir = TwinSettings().plant_config_dir
    directory = Path(config_dir)
    files = dict(CONFIG_FILES)
    tag_map_path = _resolve_tag_map(directory, tag_map)

    issues: list[ConfigIssue] = []
    if not directory.is_dir():
        raise ConfigError(
            directory, [ConfigIssue("", "", f"config directory not found: {directory}")]
        )

    sources: dict[str, YamlSource] = {}
    paths = {key: directory / name for key, name in files.items()}
    if tag_map_path is not None:
        paths[TAG_MAP] = tag_map_path
    for key, path in paths.items():
        source, read_issues = read_yaml(path, name=path.name)
        issues.extend(read_issues)
        if source is not None:
            sources[key] = source

    validated: dict[str, BaseModel] = {}
    for key, source in sources.items():
        if not isinstance(source.data, dict):
            continue
        root = _ROOT_MODELS[key]
        try:
            validated[key] = root.model_validate(source.data)
        except ValidationError as exc:
            issues.extend(_pydantic_issues(source, root, exc))
    if issues:
        raise ConfigError(directory, issues)

    models = Models(
        plant=_as(validated[PLANT], PlantConfig),
        rules=_as(validated[RULES], RulesConfig),
        reasons=_as(validated[REASONS], ReasonCodesConfig),
        defects=_as(validated[DEFECTS], DefectCodesConfig),
        simulation=_as(validated[SIMULATION], SimulationConfig),
        business=_as(validated[BUSINESS], BusinessConfig),
        tag_map=_as(validated[TAG_MAP], TagMapConfig) if TAG_MAP in validated else None,
    )
    issues = check_references(models, sources)
    if issues:
        raise ConfigError(directory, issues)
    return TwinConfig(
        directory,
        plant=models.plant,
        rules=models.rules,
        reasons=models.reasons,
        defects=models.defects,
        simulation=models.simulation,
        business=models.business,
        tag_map=models.tag_map,
        tag_map_path=tag_map_path if models.tag_map is not None else None,
    )


def load_tag_map(path: Path | str, plant: PlantConfig) -> TagMapConfig:
    """Validate a single tag map file against an already loaded plant model."""
    file = Path(path)
    source, issues = read_yaml(file, name=file.name)
    if source is None or issues:
        raise ConfigError(file.parent, issues)
    try:
        tag_map = TagMapConfig.model_validate(source.data)
    except ValidationError as exc:
        raise ConfigError(file.parent, _pydantic_issues(source, TagMapConfig, exc)) from None
    ref_issues = check_tag_map_only(plant, tag_map, {TAG_MAP: source})
    if ref_issues:
        raise ConfigError(file.parent, ref_issues)
    return tag_map


def load_config_from_settings(settings: TwinSettings | None = None) -> TwinConfig:
    """``load_config`` using ``PLANT_CONFIG_DIR`` / ``PLANT_TAG_MAP`` from the environment."""
    from twin_core.settings import TwinSettings

    resolved = settings or TwinSettings()
    return load_config(resolved.plant_config_dir, tag_map=resolved.plant_tag_map)


def _as[T: BaseModel](model: BaseModel, cls: type[T]) -> T:
    if not isinstance(model, cls):  # pragma: no cover - guarded by _ROOT_MODELS
        raise TypeError(f"expected {cls.__name__}, got {type(model).__name__}")
    return model
