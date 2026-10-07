"""Referential integrity across config files (FR-DOM-01).

Every code referenced anywhere must exist where it is defined; codes and aliases must be unique.
All problems are collected (not just the first) with file, line, YAML path, the bad value and the
closest valid code.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from twin_core.aliases import closest_code, normalize_name
from twin_core.config.business import BusinessConfig
from twin_core.config.defects import ANY_AREA, DefectCodesConfig
from twin_core.config.errors import ConfigIssue
from twin_core.config.plant import PlantConfig, Point, Rect
from twin_core.config.reasons import FALLBACK_REASON_CODE, ReasonCodesConfig
from twin_core.config.rules import RulesConfig
from twin_core.config.simulation import (
    CkdInject,
    DefectMultiplierInject,
    FailureInject,
    SetStateInject,
    SimulationConfig,
)
from twin_core.config.source import Loc, YamlSource
from twin_core.config.tag_map import (
    BUFFER_VARIABLES,
    EQUIPMENT_VARIABLES,
    LINE_VARIABLES,
    PRODUCT_VARIABLES,
    SITE_VARIABLES,
    TagMapConfig,
)

PLANT = "plant"
RULES = "rules"
REASONS = "reasons"
DEFECTS = "defects"
SIMULATION = "simulation"
BUSINESS = "business"
TAG_MAP = "tag_map"

_MAX_LISTED = 10
_MINUTES_PER_DAY = 24 * 60
_DEGRADATION_KEY = "degradation"


@dataclass(frozen=True)
class Models:
    plant: PlantConfig
    rules: RulesConfig
    reasons: ReasonCodesConfig
    defects: DefectCodesConfig
    simulation: SimulationConfig
    business: BusinessConfig
    tag_map: TagMapConfig | None = None


class _Checker:
    def __init__(self, sources: Mapping[str, YamlSource]) -> None:
        self.sources = sources
        self.issues: list[ConfigIssue] = []

    def add(
        self,
        key: str,
        loc: Loc,
        message: str,
        *,
        value: object = None,
        suggestion: str | None = None,
    ) -> None:
        self.issues.append(
            self.sources[key].issue(loc, message, value=value, suggestion=suggestion)
        )

    def ref(self, key: str, loc: Loc, value: str, valid: Collection[str], kind: str) -> bool:
        """Report ``value`` unless it is one of ``valid``; suggest the closest valid code."""
        if value in valid:
            return True
        known = sorted(valid)
        listing = (
            f"known: {', '.join(known)}" if len(known) <= _MAX_LISTED else f"{len(known)} known"
        )
        self.add(
            key,
            loc,
            f"unknown {kind} '{value}' ({listing})",
            value=value,
            suggestion=closest_code(value, known),
        )
        return False

    def unique(self, key: str, items: Iterable[tuple[Loc, str]], kind: str) -> None:
        seen: dict[str, Loc] = {}
        for loc, code in items:
            if code in seen:
                first = self.sources[key].describe(seen[code])
                self.add(key, loc, f"duplicate {kind} '{code}' (already defined at {first})")
            else:
                seen[code] = loc


# --------------------------------------------------------------------------- helpers


def _flow_lines(plant: PlantConfig) -> list[str]:
    return [line.code for area in plant.areas for line in area.lines]


def _equipment_type_of(plant: PlantConfig) -> dict[str, str]:
    return {
        eq.code: eq.type for area in plant.areas for line in area.lines for eq in line.equipment
    }


def _signals_by_type(plant: PlantConfig) -> dict[str, set[str]]:
    return {
        type_code: {signal.code for signal in etype.signals}
        for type_code, etype in plant.equipment_types.items()
    }


def _shift_intervals(start_min: int, duration: int) -> list[tuple[int, int]]:
    """Minute intervals of a daily shift on a two-day timeline (handles midnight crossing)."""
    return [(start_min + day, start_min + day + duration) for day in (0, _MINUTES_PER_DAY)]


# --------------------------------------------------------------------------- plant.yaml


def _check_plant(c: _Checker, plant: PlantConfig) -> None:
    # ISA-95 asset codes are unique across all asset kinds (events carry just the code).
    assets: list[tuple[Loc, str]] = [(("site", "code"), plant.site.code)]
    for ai, area in enumerate(plant.areas):
        assets.append((("areas", ai, "code"), area.code))
        for li, line in enumerate(area.lines):
            assets.append((("areas", ai, "lines", li, "code"), line.code))
            for ei, eq in enumerate(line.equipment):
                assets.append((("areas", ai, "lines", li, "equipment", ei, "code"), eq.code))
                c.ref(
                    PLANT,
                    ("areas", ai, "lines", li, "equipment", ei, "type"),
                    eq.type,
                    plant.equipment_types,
                    "equipment type",
                )
    for bi, buffer in enumerate(plant.buffers):
        assets.append((("buffers", bi, "code"), buffer.code))
    c.unique(PLANT, assets, "asset code")
    c.unique(
        PLANT,
        ((("products", i, "code"), p.code) for i, p in enumerate(plant.products)),
        "product code",
    )
    for type_code, etype in plant.equipment_types.items():
        c.unique(
            PLANT,
            (
                (("equipment_types", type_code, "signals", i, "code"), s.code)
                for i, s in enumerate(etype.signals)
            ),
            "signal code",
        )

    # Buffers connect two different lines, upstream -> downstream.
    flow = _flow_lines(plant)
    for bi, buffer in enumerate(plant.buffers):
        ok_from = c.ref(PLANT, ("buffers", bi, "from_line"), buffer.from_line, flow, "line")
        ok_to = c.ref(PLANT, ("buffers", bi, "to_line"), buffer.to_line, flow, "line")
        if ok_from and ok_to and flow.index(buffer.from_line) >= flow.index(buffer.to_line):
            c.add(
                PLANT,
                ("buffers", bi, "to_line"),
                f"buffer must go downstream: '{buffer.to_line}' is not after "
                f"'{buffer.from_line}' in the material flow ({' → '.join(flow)})",
            )

    # Plan.
    products = [p.code for p in plant.products]
    plan_keys: list[tuple[Loc, str]] = []
    for pi, entry in enumerate(plant.plan):
        if entry.line is not None:
            c.ref(PLANT, ("plan", pi, "line"), entry.line, flow, "line")
        if entry.product is not None:
            c.ref(PLANT, ("plan", pi, "product"), entry.product, products, "product")
        key = "/".join(str(x) for x in (entry.month, entry.level, entry.line, entry.product))
        plan_keys.append((("plan", pi), key))
    c.unique(PLANT, plan_keys, "plan entry (month/level/line/product)")

    _check_calendar(c, plant)
    _check_layout(c, plant)


def _check_calendar(c: _Checker, plant: PlantConfig) -> None:
    cal = plant.calendar
    shift_codes = [s.code for s in cal.shifts]
    c.unique(
        PLANT,
        ((("calendar", "shifts", i, "code"), s.code) for i, s in enumerate(cal.shifts)),
        "shift code",
    )
    for i, a in enumerate(cal.shifts):
        a_start = a.start.hour * 60 + a.start.minute
        for j in range(i + 1, len(cal.shifts)):
            b = cal.shifts[j]
            b_start = b.start.hour * 60 + b.start.minute
            overlap = any(
                s1 < e2 and s2 < e1
                for s1, e1 in _shift_intervals(a_start, a.duration_min)
                for s2, e2 in _shift_intervals(b_start, b.duration_min)
            )
            if overlap:
                c.add(PLANT, ("calendar", "shifts", j), f"shift '{b.code}' overlaps '{a.code}'")
    c.unique(
        PLANT,
        ((("calendar", "working_weekdays", i), str(d)) for i, d in enumerate(cal.working_weekdays)),
        "weekday",
    )
    c.unique(
        PLANT,
        (
            (("calendar", "holidays", i, "date"), h.date.isoformat())
            for i, h in enumerate(cal.holidays)
        ),
        "holiday date",
    )
    holidays = {h.date for h in cal.holidays}
    c.unique(
        PLANT,
        (
            (("calendar", "extra_working_days", i, "date"), d.date.isoformat())
            for i, d in enumerate(cal.extra_working_days)
        ),
        "extra working day",
    )
    for i, extra in enumerate(cal.extra_working_days):
        loc: Loc = ("calendar", "extra_working_days", i)
        day: date = extra.date
        if day in holidays:
            c.add(
                PLANT,
                (*loc, "date"),
                f"{day.isoformat()} is both a holiday and an extra working day",
            )
        elif day.isoweekday() in cal.working_weekdays:
            c.add(
                PLANT,
                (*loc, "date"),
                f"{day.isoformat()} is already a regular working day "
                "(extra_working_days is for weekends; holidays go to 'holidays')",
            )
        for si, code in enumerate(extra.shifts or []):
            c.ref(PLANT, (*loc, "shifts", si), code, shift_codes, "shift")


def _check_layout(c: _Checker, plant: PlantConfig) -> None:
    vx, vy, vw, vh = plant.layout.viewbox

    def inside(x: float, y: float) -> bool:
        return vx <= x <= vx + vw and vy <= y <= vy + vh

    def check_rect(loc: Loc, rect: Rect) -> None:
        if not (inside(rect.x, rect.y) and inside(rect.x + rect.w, rect.y + rect.h)):
            c.add(PLANT, loc, f"layout rectangle is outside the viewbox {plant.layout.viewbox}")

    def check_point(loc: Loc, point: Point) -> None:
        if not inside(point.x, point.y):
            c.add(PLANT, loc, f"layout point is outside the viewbox {plant.layout.viewbox}")

    for ai, area in enumerate(plant.areas):
        check_rect(("areas", ai, "layout"), area.layout)
        for li, line in enumerate(area.lines):
            for ei, eq in enumerate(line.equipment):
                check_point(("areas", ai, "lines", li, "equipment", ei, "layout"), eq.layout)
    for bi, buffer in enumerate(plant.buffers):
        check_rect(("buffers", bi, "layout"), buffer.layout)
    for pi, (x, y) in enumerate(plant.layout.flow_path):
        if not inside(x, y):
            c.add(PLANT, ("layout", "flow_path", pi), "flow_path point is outside the viewbox")


# --------------------------------------------------------------------------- aliases


def _check_aliases(c: _Checker, m: Models) -> None:
    """Names used for FR-DOM-02 lookups must be unambiguous within each entity kind."""
    plant = m.plant
    groups: dict[str, list[tuple[str, Loc, str, Sequence[str | None]]]] = {
        "area": [],
        "line": [],
        "equipment": [],
        "buffer": [],
        "product": [],
        "reason": [],
        "defect": [],
    }
    for ai, area in enumerate(plant.areas):
        groups["area"].append(
            (PLANT, ("areas", ai), area.code, (area.name_ru, area.name_kk, *area.aliases))
        )
        for li, line in enumerate(area.lines):
            groups["line"].append(
                (
                    PLANT,
                    ("areas", ai, "lines", li),
                    line.code,
                    (line.name_ru, line.name_kk, *line.aliases),
                )
            )
            for ei, eq in enumerate(line.equipment):
                groups["equipment"].append(
                    (
                        PLANT,
                        ("areas", ai, "lines", li, "equipment", ei),
                        eq.code,
                        (eq.name_ru, eq.name_kk, *eq.aliases),
                    )
                )
    for bi, buffer in enumerate(plant.buffers):
        groups["buffer"].append(
            (PLANT, ("buffers", bi), buffer.code, (buffer.name_ru, buffer.name_kk, *buffer.aliases))
        )
    for pi, product in enumerate(plant.products):
        groups["product"].append(
            (PLANT, ("products", pi), product.code, (product.name, *product.aliases))
        )
    for ci, category in enumerate(m.reasons.categories):
        for ri, reason in enumerate(category.reasons):
            groups["reason"].append(
                (
                    REASONS,
                    ("categories", ci, "reasons", ri),
                    reason.code,
                    (reason.name_ru, reason.name_kk, *reason.aliases),
                )
            )
    for di, defect in enumerate(m.defects.defects):
        groups["defect"].append(
            (
                DEFECTS,
                ("defects", di),
                defect.code,
                (defect.name_ru, defect.name_kk, *defect.aliases),
            )
        )

    for kind, entries in groups.items():
        owner: dict[str, str] = {}
        for key, loc, code, names in entries:
            for name in (code, *names):
                if not name:
                    continue
                norm = normalize_name(name)
                other = owner.get(norm)
                if other is not None and other != code:
                    c.add(
                        key,
                        loc,
                        f"{kind} name/alias '{name}' of '{code}' is ambiguous: "
                        f"it already identifies {kind} '{other}'",
                    )
                else:
                    owner[norm] = code


# --------------------------------------------------------------------------- rules.yaml


def _check_rules(c: _Checker, rules: RulesConfig) -> None:
    roles = rules.roles
    c.unique(RULES, ((("roles", i), r) for i, r in enumerate(roles)), "role")
    c.unique(
        RULES,
        ((("alert_rules", i, "id"), r.id) for i, r in enumerate(rules.alert_rules)),
        "alert rule id",
    )
    c.unique(
        RULES,
        ((("data_quality_rules", i, "id"), r.id) for i, r in enumerate(rules.data_quality_rules)),
        "data quality rule id",
    )
    for ri, rule in enumerate(rules.alert_rules):
        for i, role in enumerate(rule.recipients):
            c.ref(RULES, ("alert_rules", ri, "recipients", i), role, roles, "role")
    for severity, level in rules.escalation.items():
        for i, role in enumerate(level.chain):
            c.ref(RULES, ("escalation", severity, "chain", i), role, roles, "role")


# --------------------------------------------------------------------------- codes


def _check_reasons(c: _Checker, reasons: ReasonCodesConfig) -> None:
    c.unique(
        REASONS,
        ((("categories", i, "code"), cat.code) for i, cat in enumerate(reasons.categories)),
        "reason category",
    )
    c.unique(
        REASONS,
        (
            (("categories", ci, "reasons", ri, "code"), r.code)
            for ci, cat in enumerate(reasons.categories)
            for ri, r in enumerate(cat.reasons)
        ),
        "reason code",
    )
    if FALLBACK_REASON_CODE not in {r.code for r in reasons.reasons}:
        c.add(
            REASONS,
            ("categories",),
            f"fallback reason '{FALLBACK_REASON_CODE}' is required (SPEC FR-ENG-02)",
        )


def _check_defects(c: _Checker, defects: DefectCodesConfig, plant: PlantConfig) -> None:
    c.unique(
        DEFECTS,
        ((("defects", i, "code"), d.code) for i, d in enumerate(defects.defects)),
        "defect code",
    )
    areas = [a.code for a in plant.areas] + [ANY_AREA]
    for di, defect in enumerate(defects.defects):
        c.ref(DEFECTS, ("defects", di, "area"), defect.area, areas, "area")


# --------------------------------------------------------------------------- simulation.yaml


def _check_simulation(c: _Checker, m: Models) -> None:
    plant, sim = m.plant, m.simulation
    types = plant.equipment_types
    areas = [a.code for a in plant.areas]
    lines = _flow_lines(plant)
    products = [p.code for p in plant.products]
    buffers = {b.code: b for b in plant.buffers}
    equipment_type = _equipment_type_of(plant)
    signals = _signals_by_type(plant)
    reasons = {r.code: r for r in m.reasons.reasons}
    defects = {d.code: d for d in m.defects.defects}
    shift_codes = [s.code for s in plant.calendar.shifts]

    def unplanned_reason(loc: Loc, code: str) -> None:
        if c.ref(SIMULATION, loc, code, reasons, "reason code") and reasons[code].planned:
            c.add(
                SIMULATION,
                loc,
                f"reason '{code}' is planned (planned=true) and cannot describe a failure",
            )

    # process
    for code in sim.process.product_mix:
        c.ref(SIMULATION, ("process", "product_mix", code), code, products, "product")
    for code, level in sim.process.initial_buffers.items():
        loc: Loc = ("process", "initial_buffers", code)
        if c.ref(SIMULATION, loc, code, buffers, "buffer") and level > buffers[code].capacity:
            c.add(
                SIMULATION,
                loc,
                f"initial level {level} exceeds buffer capacity {buffers[code].capacity}",
            )

    # failures
    for type_code, model in sim.failures.items():
        base: Loc = ("failures", type_code)
        c.ref(SIMULATION, base, type_code, types, "equipment type")
        for reason in model.reasons:
            unplanned_reason((*base, "reasons", reason), reason)
        allowed_wear = set(model.reasons)
        if model.chain_break is not None:
            unplanned_reason((*base, "chain_break", "reason"), model.chain_break.reason)
            allowed_wear.add(model.chain_break.reason)
        for i, reason in enumerate(model.wear_reasons):
            if (
                c.ref(SIMULATION, (*base, "wear_reasons", i), reason, reasons, "reason code")
                and reason not in allowed_wear
            ):
                c.add(
                    SIMULATION,
                    (*base, "wear_reasons", i),
                    f"wear reason '{reason}' is not one of this type's failure reasons "
                    f"({', '.join(sorted(allowed_wear))})",
                )

    # microstops (must be shorter than the microstop threshold of rules.yaml)
    threshold_s = m.rules.thresholds.microstop_threshold_s
    for type_code, micro in sim.microstops.items():
        base = ("microstops", type_code)
        c.ref(SIMULATION, base, type_code, types, "equipment type")
        unplanned_reason((*base, "reason"), micro.reason)
        if micro.duration.median * 60 >= threshold_s:
            c.add(
                SIMULATION,
                (*base, "duration", "median"),
                f"median microstop {micro.duration.median} min is not below "
                f"rules.yaml thresholds.microstop_threshold_s = {threshold_s:g} s",
            )

    for type_code in sim.degradation.per_type:
        c.ref(SIMULATION, ("degradation", type_code), type_code, types, "equipment type")

    if sim.paint_filters is not None:
        pf = sim.paint_filters
        unplanned_reason(("paint_filters", "replacement", "reason"), pf.replacement.reason)
        if c.ref(
            SIMULATION,
            ("paint_filters", "equipment_type"),
            pf.equipment_type,
            types,
            "equipment type",
        ):
            c.ref(
                SIMULATION,
                ("paint_filters", "signal"),
                pf.signal,
                signals[pf.equipment_type],
                f"signal of type '{pf.equipment_type}'",
            )

    for i, pm in enumerate(sim.planned_maintenance):
        base = ("planned_maintenance", i)
        c.ref(SIMULATION, (*base, "equipment_type"), pm.equipment_type, types, "equipment type")
        c.ref(SIMULATION, (*base, "shift"), pm.shift, shift_codes, "shift")
        if (
            c.ref(SIMULATION, (*base, "reason"), pm.reason, reasons, "reason code")
            and not reasons[pm.reason].planned
        ):
            c.add(
                SIMULATION,
                (*base, "reason"),
                f"planned maintenance needs a planned reason; '{pm.reason}' has planned=false",
            )

    # defects: per area, defect types must belong to that area (or ANY)
    area_types: dict[str, list[str]] = {
        area.code: [eq.type for line in area.lines for eq in line.equipment] for area in plant.areas
    }
    signal_defs = {
        (type_code, signal.code): signal
        for type_code, etype in types.items()
        for signal in etype.signals
    }
    for area_code, area_defects in sim.defects.per_area.items():
        base = ("defects", area_code)
        c.ref(SIMULATION, base, area_code, areas, "area")
        in_area = area_types.get(area_code, [])
        wear_type = area_defects.wear_equipment_type
        if (
            wear_type is not None
            and c.ref(
                SIMULATION, (*base, "wear_equipment_type"), wear_type, in_area, "type in this area"
            )
            and wear_type not in sim.degradation.per_type
        ):
            c.add(
                SIMULATION,
                (*base, "wear_equipment_type"),
                f"type '{wear_type}' has no degradation parameters (degradation.{wear_type})",
            )
        humidity = area_defects.humidity_signal
        if humidity is not None:
            found = [signal_defs[(t, humidity)] for t in in_area if (t, humidity) in signal_defs]
            area_signals = sorted({s for t in in_area for s in signals.get(t, set())})
            if c.ref(
                SIMULATION, (*base, "humidity_signal"), humidity, area_signals, "signal in area"
            ) and any(s.warn_lo is None or s.warn_hi is None for s in found):
                c.add(
                    SIMULATION,
                    (*base, "humidity_signal"),
                    f"signal '{humidity}' needs warn_lo and warn_hi in plant.yaml "
                    "(they define the in-spec band)",
                )
        for code in area_defects.types:
            loc = (*base, "types", code)
            if (
                c.ref(SIMULATION, loc, code, defects, "defect code")
                and defects[code].area in areas  # an unknown area is reported for defect_codes
                and defects[code].area not in (area_code, ANY_AREA)
            ):
                c.add(
                    SIMULATION,
                    loc,
                    f"defect '{code}' belongs to area '{defects[code].area}', not '{area_code}'",
                )

    # telemetry: signal models must match signals declared for the type in plant.yaml
    for type_code, models in sim.telemetry.per_type.items():
        base = ("telemetry", type_code)
        if c.ref(SIMULATION, base, type_code, types, "equipment type"):
            for signal in models:
                c.ref(
                    SIMULATION,
                    (*base, signal),
                    signal,
                    signals[type_code],
                    f"signal of type '{type_code}'",
                )

    for code in sim.ckd_supply.initial_kits:
        c.ref(SIMULATION, ("ckd_supply", "initial_kits", code), code, products, "product")

    targets = sim.calibration_targets
    for area_code in targets.defect_rate:
        c.ref(
            SIMULATION, ("calibration_targets", "defect_rate", area_code), area_code, areas, "area"
        )
    c.ref(
        SIMULATION,
        ("calibration_targets", "expected_bottleneck"),
        targets.expected_bottleneck,
        lines,
        "line",
    )

    # scenarios
    c.unique(
        SIMULATION,
        ((("scenarios", i, "id"), s.id) for i, s in enumerate(sim.scenarios)),
        "scenario id",
    )
    for si, scenario in enumerate(sim.scenarios):
        base = ("scenarios", si, "inject")
        inject = scenario.inject
        if isinstance(inject, FailureInject):
            c.ref(SIMULATION, (*base, "equipment"), inject.equipment, equipment_type, "equipment")
            unplanned_reason((*base, "reason"), inject.reason)
        elif isinstance(inject, SetStateInject):
            if c.ref(
                SIMULATION, (*base, "equipment"), inject.equipment, equipment_type, "equipment"
            ):
                etype = equipment_type[inject.equipment]
                settable = signals.get(etype, set()) | (
                    {_DEGRADATION_KEY} if etype in sim.degradation.per_type else set()
                )
                for name in inject.values:
                    c.ref(
                        SIMULATION,
                        (*base, name),
                        name,
                        settable,
                        f"state value for '{inject.equipment}' (type {etype})",
                    )
        elif isinstance(inject, DefectMultiplierInject):
            for i, area_code in enumerate(inject.areas):
                c.ref(SIMULATION, (*base, "areas", i), area_code, areas, "area")
        elif isinstance(inject, CkdInject):
            c.ref(SIMULATION, (*base, "product"), inject.product, products, "product")


# --------------------------------------------------------------------------- business.yaml


def _check_business(c: _Checker, m: Models) -> None:
    areas = [a.code for a in m.plant.areas]
    for code in m.business.params.rework_cost_kzt.value:
        c.ref(BUSINESS, ("params", "rework_cost_kzt", "value", code), code, areas, "area")
    if m.business.currency != m.plant.site.currency:
        c.add(
            BUSINESS,
            ("currency",),
            f"currency '{m.business.currency}' differs from "
            f"plant.yaml site.currency '{m.plant.site.currency}'",
        )


# --------------------------------------------------------------------------- tag map


def check_tag_map(c: _Checker, plant: PlantConfig, tag_map: TagMapConfig) -> None:
    nodes = tag_map.opcua.nodes
    c.unique(
        TAG_MAP,
        ((("opcua", "nodes", i, "node_id"), n.node_id) for i, n in enumerate(nodes)),
        "node_id",
    )
    equipment_type = _equipment_type_of(plant)
    signals = _signals_by_type(plant)
    lines = _flow_lines(plant)
    buffers = [b.code for b in plant.buffers]
    products = [p.code for p in plant.products]
    for i, node in enumerate(nodes):
        base: Loc = ("opcua", "nodes", i)
        kind, code = node.target
        allowed: Collection[str]
        if kind == "equipment":
            if not c.ref(TAG_MAP, (*base, kind), code, equipment_type, "equipment"):
                continue
            allowed = EQUIPMENT_VARIABLES | signals.get(equipment_type[code], set())
        elif kind == "line":
            if not c.ref(TAG_MAP, (*base, kind), code, lines, "line"):
                continue
            allowed = LINE_VARIABLES
        elif kind == "buffer":
            if not c.ref(TAG_MAP, (*base, kind), code, buffers, "buffer"):
                continue
            allowed = BUFFER_VARIABLES
        elif kind == "product":
            if not c.ref(TAG_MAP, (*base, kind), code, products, "product"):
                continue
            allowed = PRODUCT_VARIABLES
        else:
            if not c.ref(TAG_MAP, (*base, kind), code, [plant.site.code], "site"):
                continue
            allowed = SITE_VARIABLES
        c.ref(TAG_MAP, (*base, "signal"), node.signal, allowed, f"signal for {kind} '{code}'")


# --------------------------------------------------------------------------- entry point


def check_references(models: Models, sources: Mapping[str, YamlSource]) -> list[ConfigIssue]:
    """Run every cross-file / referential check; return all issues found."""
    c = _Checker(sources)
    _check_plant(c, models.plant)
    _check_rules(c, models.rules)
    _check_reasons(c, models.reasons)
    _check_defects(c, models.defects, models.plant)
    _check_aliases(c, models)
    _check_simulation(c, models)
    _check_business(c, models)
    if models.tag_map is not None and TAG_MAP in sources:
        check_tag_map(c, models.plant, models.tag_map)
    return c.issues


def check_tag_map_only(
    plant: PlantConfig, tag_map: TagMapConfig, sources: Mapping[str, YamlSource]
) -> list[ConfigIssue]:
    c = _Checker(sources)
    check_tag_map(c, plant, tag_map)
    return c.issues
