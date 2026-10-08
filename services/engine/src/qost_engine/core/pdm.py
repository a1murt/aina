"""PdM / limit / SPC results handed to the core (SPEC §11.1–11.3, stage M7b).

The serving layer (:mod:`qost_engine.pdm`) computes a :class:`PdmTick` from the model and the
telemetry cache; :meth:`EngineCore.apply_pdm` turns it into ``prediction`` rows, live health
indices and the alerts AL-M1 / AL-M2. The core itself stays free of models and I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class UnitPrediction:
    equipment: str
    p_failure: float
    health_index: float
    horizon_h: float
    model_version: str
    source: str
    """``model`` | ``rule``."""
    factors: tuple[dict[str, Any], ...] = ()
    """Top factors as :meth:`qost_ml.explain.Factor.as_dict` (text_ru / text_kk included)."""


@dataclass(frozen=True, slots=True)
class LimitItem:
    """AL-M2 evaluation of one signal of one unit."""

    equipment: str
    signal: str
    signal_name_ru: str
    unit: str
    limit: float
    level_now: float
    slope_per_h: float
    hours_to_limit: float | None
    limit_at: datetime | None
    window: datetime | None
    saving_min: float
    saving_cars: float
    alert: bool
    """Within the look-ahead."""
    n_points: int = 0


@dataclass(frozen=True, slots=True)
class PdmTick:
    ts: datetime
    units: tuple[UnitPrediction, ...] = ()
    limits: tuple[LimitItem, ...] = ()
    lookahead_h: float = 12.0
    extra: dict[str, Any] = field(default_factory=dict)
