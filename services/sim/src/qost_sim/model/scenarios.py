"""Scenario injects (SPEC §6.9, simulation.yaml ``scenarios``): parsing and validation.

The YAML scenarios are validated by ``twin_core.config`` cross-checks; ad-hoc injects from the
demo console go through :func:`parse_inject`, which applies the same checks plus what the model
supports (``set_state`` may set ``degradation`` of a wearing unit and the filter signal of a
filter unit — other telemetry values are derived and cannot be set).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from qost_sim.model.plant import Inject
from twin_core.config import TwinConfig
from twin_core.config.simulation import (
    CkdInject,
    DefectMultiplierInject,
    FailureInject,
    SetStateInject,
)
from twin_core.config.simulation import Inject as InjectUnion

_ADAPTER: TypeAdapter[Inject] = TypeAdapter(InjectUnion)


class InjectError(ValueError):
    """An inject that the model cannot apply; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def settable_keys(cfg: TwinConfig, equipment: str) -> list[str]:
    eq = cfg.equipment[equipment]
    keys: list[str] = []
    if eq.type in cfg.simulation.degradation.per_type:
        keys.append("degradation")
    pf = cfg.simulation.paint_filters
    if pf is not None and pf.equipment_type == eq.type:
        keys.append(pf.signal)
    return keys


def check_inject(cfg: TwinConfig, inject: Inject) -> list[str]:
    """Every reason why ``inject`` cannot be applied (empty list = fine)."""
    problems: list[str] = []
    if isinstance(inject, FailureInject | SetStateInject) and inject.equipment not in cfg.equipment:
        return [f"unknown equipment '{inject.equipment}'"]
    if isinstance(inject, FailureInject):
        reason = cfg.reasons.get(inject.reason)
        if reason is None:
            problems.append(f"unknown reason code '{inject.reason}'")
        elif reason.planned:
            problems.append(f"reason '{inject.reason}' is planned and cannot describe a failure")
    elif isinstance(inject, SetStateInject):
        allowed = settable_keys(cfg, inject.equipment)
        for key in inject.values:
            if key not in allowed:
                problems.append(
                    f"'{key}' cannot be set on {inject.equipment} "
                    f"(settable: {', '.join(allowed) or 'nothing'})"
                )
    elif isinstance(inject, DefectMultiplierInject):
        for area in inject.areas:
            if area not in cfg.areas or not cfg.areas[area].lines:
                problems.append(f"unknown production area '{area}'")
    elif isinstance(inject, CkdInject):
        if inject.product not in cfg.products:
            problems.append(f"unknown product '{inject.product}'")
        if inject.set_kits is None and inject.delay_next_lot_days is None:
            problems.append("ckd inject needs set_kits and/or delay_next_lot_days")
    return problems


def parse_inject(cfg: TwinConfig, raw: Mapping[str, Any]) -> Inject:
    """Validate an ad-hoc inject document (raises InjectError)."""
    try:
        inject = _ADAPTER.validate_python(dict(raw))
    except ValidationError as exc:
        raise InjectError(
            [f"{'.'.join(str(p) for p in e['loc']) or 'inject'}: {e['msg']}" for e in exc.errors()]
        ) from None
    problems = check_inject(cfg, inject)
    if problems:
        raise InjectError(problems)
    return inject


def check_configured_scenarios(cfg: TwinConfig) -> list[str]:
    """Problems of simulation.yaml scenarios the model cannot apply (checked at start)."""
    problems: list[str] = []
    for scenario in cfg.simulation.scenarios:
        problems.extend(f"scenario {scenario.id}: {p}" for p in check_inject(cfg, scenario.inject))
    return problems
