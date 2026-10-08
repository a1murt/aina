"""Typed inputs of the fast model (SPEC §10.1–10.2).

* :class:`CalibrationParams` — what history says about the plant (failure rates, repair times,
  line efficiency, defect shares, filters), each value with its sample size and ``source``
  (``data`` | ``prior`` | ``config``). Persisted as ``calibration_snapshot.params`` and served by
  ``GET /api/v1/calibration``.
* :class:`PlantState` — the plant "now": output month-to-date, buffer levels, open repairs,
  filter pressure drop, CKD stock. Built by adapters (database, virtual plant, later the live
  snapshot of the engine), so the model never depends on how the state was obtained.
* :class:`Targets` — the month targets: plant target and line plan (SPEC §5.8).
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["data", "prior", "config"]
RepairBasis = Literal["equipment", "type", "prior"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GammaRate(_Frozen):
    """Failure rate per operating hour: point estimate and its Gamma(shape, rate) distribution."""

    n: int
    """Failures in the window."""
    operating_h: float
    per_h: float
    """Point estimate (data: n / operating hours; prior: posterior mean)."""
    shape: float
    rate_h: float
    """Gamma rate parameter in operating hours (mean = shape / rate_h)."""
    source: Source

    @property
    def mtbf_h(self) -> float | None:
        return 1.0 / self.per_h if self.per_h > 0 else None


class LogNormalMinutes(_Frozen):
    """Lognormal duration in minutes: ``exp(mu + sigma * z)``."""

    n: int
    mu: float
    sigma: float
    source: Source
    basis: RepairBasis = "equipment"

    @property
    def median_min(self) -> float:
        return math.exp(self.mu)

    @property
    def mean_min(self) -> float:
        return math.exp(self.mu + self.sigma**2 / 2.0)


class BetaShare(_Frozen):
    """A share in [0, 1] as Beta(alpha, beta)."""

    n: int
    """Sample size: shifts (efficiency) or first-pass units (defects)."""
    alpha: float
    beta: float
    source: Source

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def sd(self) -> float:
        a, b = self.alpha, self.beta
        return math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))


class EquipmentParams(_Frozen):
    code: str
    line: str
    type: str
    criticality: Literal["A", "B", "C"]
    degraded_capacity: float
    failures: GammaRate
    repair: LogNormalMinutes


class LineParams(_Frozen):
    code: str
    area: str
    ict_seconds: float
    cycle_factor_mix: float
    """Mean ``cycle_factor`` over ``process.product_mix``."""
    efficiency: BetaShare
    """Speed efficiency per shift (``eff_basis``)."""
    rework_stations: int
    rework_mean_min: float
    repaint_share: float


class AreaDefects(_Frozen):
    area: str
    line: str
    pq: int
    defects: int
    rate: BetaShare
    scrap_share: float


class FilterParams(_Frozen):
    equipment: list[str]
    signal: str
    reason: str
    dp_start_pa: float
    dp_limit_pa: float
    rate_mean_pa_h: float
    rate_sd_pa_h: float
    n_lives: int
    rate_source: Source
    replacement: LogNormalMinutes


class BufferParams(_Frozen):
    code: str
    from_line: str
    to_line: str
    capacity: int
    initial: int
    source: Source = "config"


class CalibrationParams(_Frozen):
    """Model parameters estimated over ``window_days`` working days (SPEC §10.1)."""

    version: Literal[1] = 1
    window_from: datetime
    window_to: datetime
    working_days: int
    computed_at: datetime
    config_hash: str
    eff_basis: Literal["net", "iso"]
    equipment: dict[str, EquipmentParams]
    lines: dict[str, LineParams]
    """Flow order."""
    areas: dict[str, AreaDefects]
    filters: FilterParams | None
    buffers: dict[str, BufferParams]
    warnings: list[str] = Field(default_factory=list)


class OpenDown(_Frozen):
    """A unit that is down at ``as_of`` (an open ``DOWN_*`` interval)."""

    equipment: str
    state: Literal["DOWN_UNPLANNED", "DOWN_PLANNED"]
    reason: str | None
    since: datetime


class KitLot(_Frozen):
    """A CKD lot on its way: dispatched, not yet received."""

    product: str
    qty: int
    dispatched: datetime
    arrival: datetime | None = None
    """Known arrival time (e.g. an ERP date); ``None``: drawn from ``ckd_supply.delay_days``."""


class PlantState(_Frozen):
    """The plant at ``as_of`` as far as the forecast needs it."""

    as_of: datetime
    mtd_output: int
    """Finished cars (last flow line, ``FINISHED_RESULTS``) since the local month start."""
    buffers: dict[str, float]
    open_downs: list[OpenDown] = Field(default_factory=list)
    filter_dp: dict[str, float] = Field(default_factory=dict)
    """Filter pressure drop per filter unit, Pa; a missing unit is drawn uniformly."""
    kits: dict[str, float] = Field(default_factory=dict)
    kits_in_transit: list[KitLot] = Field(default_factory=list)
    held: dict[str, float] = Field(default_factory=dict)
    """Line -> bodies inside the line or its repair/repaint queue (work in progress that next
    goes downstream of the line)."""
    source: str = "db"
    warnings: list[str] = Field(default_factory=list)

    def digest(self) -> str:
        """Short content hash (cache key part)."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]


class Targets(_Frozen):
    """Monthly targets (SPEC §5.8): ``plant_target`` 5 500, ``line_plan`` = Σ line plans 4 800."""

    plant_target: int | None
    line_plan: int | None
    source: Literal["db", "config"] = "config"

    def as_dict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        if self.plant_target is not None:
            out["plant_target"] = self.plant_target
        if self.line_plan is not None:
            out["line_plan"] = self.line_plan
        return out
