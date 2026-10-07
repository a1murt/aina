"""What the PdM models predict and from which signals — derived from config only (SPEC §11.1).

* Equipment types with PdM: types that have telemetry signals (``plant.yaml: equipment_types``) and
  wear-reason failures (``simulation.yaml: failures.<type>.wear_reasons`` + ``chain_break.reason``).
  ``robot`` and ``conveyor`` get LightGBM models (:data:`MODEL_TYPES`); the rest use the signal rule
  (:mod:`qost_ml.rule_based`).
* Label horizon — ``rules.yaml: thresholds.pdm_horizon_h``; a failure counts if it is unplanned and
  lasts at least ``thresholds.microstop_threshold_s`` (5 min).
* Feature grid — ``simulation.yaml: telemetry.backfill_sample_period_s`` (5 min, SPEC §6.5); window
  step — ``ml_dataset.window_min`` (15 min).

The simulator's hidden wear (``degradation``, the oracle) is never a signal here (FR-SIM-02,
rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass

from twin_core.config import TwinConfig

MODEL_TYPES: tuple[str, ...] = ("robot", "conveyor")
"""SPEC §11.1: separate LightGBM models for robots and conveyors; other types — rule on signals."""
FEATURE_WINDOWS_H: tuple[int, ...] = (1, 4, 24)
"""SPEC §11.1: mean / std / Theil–Sen trend / max on 1 h, 4 h and 24 h windows."""
COUNT_WINDOW_H = 4
"""SPEC §11.1: sensor errors over 4 h."""
MICROSTOP_WINDOW_H = 24
"""SPEC §11.1: microstops over 24 h."""
RATE_UNITS = frozenset({"1/ч", "1/h"})
"""Signals measured as events per hour (e.g. robot sensor errors) also get a 4 h count feature."""
FORBIDDEN_TOKENS = ("degradation", "oracle")
"""Rule 5 / FR-SIM-02: no feature may come from the simulator's hidden state."""


class OracleLeakError(AssertionError):
    """A feature or signal would expose the simulator's hidden wear (``Degradation``)."""


def assert_no_oracle(names: list[str] | tuple[str, ...] | frozenset[str]) -> None:
    """Raise :class:`OracleLeakError` if any name refers to the hidden ``Degradation`` oracle."""
    bad = sorted(n for n in names if any(tok in n.lower() for tok in FORBIDDEN_TOKENS))
    if bad:
        raise OracleLeakError(f"hidden Degradation must never be a feature (FR-SIM-02): {bad}")


@dataclass(frozen=True, slots=True)
class SignalSpec:
    code: str
    unit: str
    lo: float
    hi: float
    warn_lo: float | None
    warn_hi: float | None
    limit_hi: float | None

    @property
    def is_rate(self) -> bool:
        return self.unit in RATE_UNITS


@dataclass(frozen=True, slots=True)
class TypeSpec:
    type: str
    signals: tuple[SignalSpec, ...]
    wear_reasons: frozenset[str]

    @property
    def signal_codes(self) -> tuple[str, ...]:
        return tuple(s.code for s in self.signals)


@dataclass(frozen=True, slots=True)
class PdmSpec:
    types: dict[str, TypeSpec]
    """Equipment types with PdM (signals + wear reasons), in plant.yaml order."""
    model_types: tuple[str, ...]
    equipment: dict[str, tuple[str, str]]
    """Equipment code → (type, line code) for every unit of a PdM type."""
    horizon_h: float
    grid_s: int
    window_s: int
    min_failure_s: float
    planned_reasons: frozenset[str]
    shift_codes: tuple[str, ...]
    shift_names_ru: tuple[str, ...]
    shift_names_kk: tuple[str, ...]
    warn_p: float
    crit_p: float
    windows_h: tuple[int, ...] = FEATURE_WINDOWS_H

    @classmethod
    def from_config(cls, cfg: TwinConfig, model_types: tuple[str, ...] = MODEL_TYPES) -> PdmSpec:
        sim = cfg.simulation
        types: dict[str, TypeSpec] = {}
        for code, et in cfg.equipment_types.items():
            failures = sim.failures.get(code)
            if failures is None or not et.signals:
                continue
            wear = set(failures.wear_reasons)
            if failures.chain_break is not None:
                wear.add(failures.chain_break.reason)
            if not wear:
                continue
            signals = tuple(
                SignalSpec(s.code, s.unit, s.lo, s.hi, s.warn_lo, s.warn_hi, s.limit_hi)
                for s in et.signals
            )
            assert_no_oracle(tuple(s.code for s in signals))
            types[code] = TypeSpec(code, signals, frozenset(wear))
        unknown = [t for t in model_types if t not in types]
        if unknown:
            raise ValueError(f"model types without signals or wear reasons: {unknown}")
        equipment = {
            code: (eq.type, cfg.line_of_equipment(code).code)
            for code, eq in cfg.equipment.items()
            if eq.type in types
        }
        thresholds = cfg.rules.thresholds
        grid_s = round(sim.telemetry.backfill_sample_period_s)
        window_s = sim.ml_dataset.window_min * 60
        if window_s % grid_s:
            raise ValueError(f"ml_dataset.window_min must be a multiple of the {grid_s} s grid")
        shifts = sorted(cfg.plant.calendar.shifts, key=lambda s: s.start)
        return cls(
            types=types,
            model_types=tuple(model_types),
            equipment=equipment,
            horizon_h=float(thresholds.pdm_horizon_h),
            grid_s=grid_s,
            window_s=window_s,
            min_failure_s=float(thresholds.microstop_threshold_s),
            planned_reasons=frozenset(r.code for r in cfg.reasons.values() if r.planned),
            shift_codes=tuple(s.code for s in shifts),
            shift_names_ru=tuple(s.name_ru for s in shifts),
            shift_names_kk=tuple(s.name_kk or s.name_ru for s in shifts),
            warn_p=float(thresholds.pdm_warn_p),
            crit_p=float(thresholds.pdm_crit_p),
        )

    @property
    def max_window_h(self) -> int:
        return max(max(self.windows_h), COUNT_WINDOW_H, MICROSTOP_WINDOW_H)

    def feature_names(self, equipment_type: str) -> list[str]:
        """Model features of a type, in a fixed order (the column order of the feature table)."""
        ts = self.types[equipment_type]
        names: list[str] = []
        for sig in ts.signals:
            for w in self.windows_h:
                names.extend(f"{sig.code}_{stat}_{w}h" for stat in STATS)
            if sig.is_rate:
                names.append(f"{sig.code}_count_{COUNT_WINDOW_H}h")
        names.extend(EVENT_FEATURES)
        assert_no_oracle(names)
        return names


STATS: tuple[str, ...] = ("mean", "std", "slope", "max")
EVENT_FEATURES: tuple[str, ...] = (
    f"microstops_{MICROSTOP_WINDOW_H}h",
    "hours_since_pm",
    "hours_since_repair",
    "hours_since_wear_repair",
    "cycles_since_pm",
    "shift",
    "work_hours_ahead",
)
"""Non-telemetry features. ``shift``: index of the current working shift in calendar order
(``len(shift_codes)`` = no working shift); ``work_hours_ahead``: scheduled working hours within the
label horizon (from the calendar, known in advance)."""
CATEGORICAL: tuple[str, ...] = ("shift",)
CONTEXT_FEATURES: tuple[str, ...] = ("shift", "work_hours_ahead")
"""Calendar context: it moves the probability (no work → no failure) but is not a cause, so
explanations leave it out of the top factors."""
