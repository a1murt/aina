"""Constraints from free text (FR-IMP-03) and their comparison with the configuration.

Recognized sentences (as in the case's «Дополнительные вводные»)::

    "2 смены по 8 часов"                         -> shifts_per_day, shift_hours
    "OEE - не менее 85%"                         -> oee_target
    "брака - не более 2%"                        -> defect_rate_limit
    "простой критического оборудования - 60 минут" -> critical_downtime_limit_min_per_day
    "не менее 5 500 автомобилей"                 -> plant_target_per_month

Every constraint reported for an import is either parsed from the upload's text or, when the
text says nothing about it (csv/xlsx without notes), taken from the configuration; each
:class:`ConstraintCheck` records the source and whether the text agrees with the config.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from twin_core.config import TwinConfig

Number = int | float

_DASH: Final = r"\s*[-‐-―−:]?\s*"
_NUM: Final = r"(\d+(?:[.,]\d+)?)"


def _int(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def _fraction(text: str) -> float:
    number = float(text.replace(",", "."))
    return int(number) / 100 if number.is_integer() else number / 100


def _parse_shifts(match: re.Match[str]) -> dict[str, Number]:
    return {"shifts_per_day": int(match[1]), "shift_hours": int(match[2])}


_PATTERNS: Final[
    tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], dict[str, Number]]], ...]
] = (
    (
        re.compile(r"(\d+)\s*смен[ыа]?\s*по\s*(\d+)\s*час", re.IGNORECASE),
        _parse_shifts,
    ),
    (
        re.compile(rf"OEE{_DASH}не\s+менее\s*{_NUM}\s*%", re.IGNORECASE),
        lambda m: {"oee_target": _fraction(m[1])},
    ),
    (
        re.compile(rf"брака{_DASH}не\s+более\s*{_NUM}\s*%", re.IGNORECASE),
        lambda m: {"defect_rate_limit": _fraction(m[1])},
    ),
    (
        re.compile(rf"простой\s+критического\s+оборудования{_DASH}(\d+)\s*минут", re.IGNORECASE),
        lambda m: {"critical_downtime_limit_min_per_day": int(m[1])},
    ),
    (
        re.compile(r"не\s+менее\s*(\d[\d\s  ]*)\s*автомобил", re.IGNORECASE),
        lambda m: {"plant_target_per_month": _int(m[1])},
    ),
)

CONSTRAINT_KEYS: Final = (
    "shifts_per_day",
    "shift_hours",
    "oee_target",
    "defect_rate_limit",
    "critical_downtime_limit_min_per_day",
    "plant_target_per_month",
)


def parse_constraints(paragraphs: Sequence[str]) -> dict[str, Number]:
    """Constraints found in the text, in :data:`CONSTRAINT_KEYS` order."""
    text = "\n".join(paragraphs)
    found: dict[str, Number] = {}
    for pattern, convert in _PATTERNS:
        if match := pattern.search(text):
            found.update(convert(match))
    return {key: found[key] for key in CONSTRAINT_KEYS if key in found}


def _whole(value: float) -> Number:
    return int(value) if float(value).is_integer() else value


def plant_target(cfg: TwinConfig, month: str | None) -> int:
    """Plant target for ``month`` from ``plant.yaml: plan`` (else ``rules.yaml`` threshold)."""
    for entry in cfg.plant.plan:
        if entry.level == "plant_target" and entry.month == month:
            return entry.qty
    return cfg.rules.thresholds.plant_target_per_month


def configured_constraints(cfg: TwinConfig, month: str | None) -> dict[str, Number]:
    """The same constraints as the configuration defines them."""
    shifts = cfg.plant.calendar.shifts
    hours = {s.duration_min / 60 for s in shifts}
    t = cfg.rules.thresholds
    return {
        "shifts_per_day": len(shifts),
        "shift_hours": _whole(hours.pop())
        if len(hours) == 1
        else _whole(shifts[0].duration_min / 60),
        "oee_target": t.oee_target,
        "defect_rate_limit": t.defect_rate_limit,
        "critical_downtime_limit_min_per_day": _whole(t.critical_downtime_limit_min_per_day),
        "plant_target_per_month": plant_target(cfg, month),
    }


@dataclass(frozen=True, slots=True)
class ConstraintCheck:
    key: str
    value: Number
    source: Literal["text", "config"]
    configured: Number
    matches: bool

    def to_report(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "configured": self.configured,
            "matches": self.matches,
        }


def resolve_constraints(
    parsed: dict[str, Number], configured: dict[str, Number]
) -> tuple[dict[str, Number], list[ConstraintCheck]]:
    """Effective constraints (text first, config for the rest) and the comparison per key."""
    effective: dict[str, Number] = {}
    checks: list[ConstraintCheck] = []
    for key in CONSTRAINT_KEYS:
        conf = configured[key]
        if key in parsed:
            value = parsed[key]
            checks.append(ConstraintCheck(key, value, "text", conf, abs(value - conf) < 1e-9))
        else:
            value = conf
            checks.append(ConstraintCheck(key, value, "config", conf, True))
        effective[key] = value
    return effective, checks
