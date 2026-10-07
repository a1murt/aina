"""Models for ``config/simulation.yaml`` — virtual plant parameters (SPEC §6).

Several sections mix scalar settings with per-equipment-type / per-area entries in one mapping
(``degradation``, ``defects``, ``telemetry``). They are modelled with typed pydantic extras so
that validation errors keep the original YAML path (``degradation.robot.shape``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from twin_core.config.common import (
    Code,
    Fraction,
    Ident,
    LogNormal,
    Named,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    Range,
    StrictModel,
)
from twin_core.domain import CkdShortagePolicy, FilterPolicy

_SUM_TOLERANCE = 1e-3


def _check_shares(shares: dict[str, float], what: str) -> None:
    total = sum(shares.values())
    if abs(total - 1.0) > _SUM_TOLERANCE:
        raise ValueError(f"{what} must sum to 1.0, got {total:.4f}")


class _ExtrasModel(StrictModel):
    """Base for sections that hold typed per-type / per-area entries as extra keys."""

    model_config = ConfigDict(extra="allow", frozen=True)


class SimClock(StrictModel):
    demo_start: AwareDatetime
    speed: PositiveFloat
    speed_presets: Annotated[list[PositiveFloat], Field(min_length=1)]
    backfill_from: AwareDatetime
    random_seed: int

    @model_validator(mode="after")
    def _order(self) -> SimClock:
        if self.backfill_from >= self.demo_start:
            raise ValueError("backfill_from must be earlier than demo_start")
        return self


class CycleNoise(StrictModel):
    dist: Literal["lognormal"]
    median_factor: PositiveFloat
    sigma: NonNegativeFloat


class Process(StrictModel):
    cycle_noise: CycleNoise
    product_mix: Annotated[dict[Code, Fraction], Field(min_length=1)]
    """Product code -> share of the sequence; must sum to 1."""
    sequencing: Literal["heijunka"]
    shift_b_defect_factor: PositiveFloat
    """Defect-probability factor for every shift after the first one of the day."""
    initial_buffers: dict[Code, NonNegativeInt] = {}
    ckd_shortage_policy: CkdShortagePolicy = "resequence"

    @model_validator(mode="after")
    def _mix(self) -> Process:
        _check_shares(self.product_mix, "product_mix shares")
        return self


class ChainBreak(StrictModel):
    mtbf_h: PositiveFloat
    mttr: LogNormal
    reason: Code


class FailureModel(StrictModel):
    mtbf_h: PositiveFloat
    """Mean operating hours between failures over all ``reasons`` at degradation 0."""
    mttr: LogNormal
    reasons: Annotated[dict[Code, Fraction], Field(min_length=1)]
    """Reason code -> share of failures; must sum to 1."""
    wear_reasons: list[Code] = []
    """Reasons whose hazard grows with wear: lambda(d) = lambda0 * (1 + gain * d^3)."""
    chain_break: ChainBreak | None = None

    @model_validator(mode="after")
    def _shares(self) -> FailureModel:
        _check_shares(self.reasons, "failure reason shares")
        return self


class Microstop(StrictModel):
    mtbf_h: PositiveFloat
    duration: LogNormal
    """Minutes."""
    reason: Code


class DegradationParams(StrictModel):
    mean_rate_per_h: PositiveFloat
    shape: PositiveFloat
    reset_after_repair: Fraction
    pm_reduction: Fraction
    pm_floor: Fraction = 0.0
    """Wear never drops below this value after planned maintenance."""


class Degradation(_ExtrasModel):
    """``wear_hazard_gain`` plus one :class:`DegradationParams` per equipment type."""

    wear_hazard_gain: NonNegativeFloat
    __pydantic_extra__: dict[str, DegradationParams] = Field(init=False)

    @property
    def per_type(self) -> dict[str, DegradationParams]:
        return dict(self.__pydantic_extra__ or {})


class RateDist(StrictModel):
    mean: PositiveFloat
    sd: NonNegativeFloat


class Replacement(LogNormal):
    reason: Code


class PaintFilters(StrictModel):
    equipment_type: Ident
    """Equipment type that carries the filters (its units get the hidden pressure-drop state)."""
    signal: Ident
    """Telemetry signal showing the filter pressure drop; ``set_state`` on it sets the state."""
    dp_start_pa: NonNegativeFloat
    dp_rate_pa_per_h: RateDist
    dp_limit_pa: PositiveFloat
    replacement: Replacement
    policy: FilterPolicy

    @model_validator(mode="after")
    def _limit_above_start(self) -> PaintFilters:
        if self.dp_limit_pa <= self.dp_start_pa:
            raise ValueError("dp_limit_pa must be above dp_start_pa")
        return self


class PlannedMaintenance(StrictModel):
    equipment_type: Ident
    every_working_days: PositiveInt
    duration_min: PositiveFloat
    at: Literal["shift_start"]
    shift: Code
    stagger: bool
    reason: Code


class AreaDefects(StrictModel):
    base: Fraction
    types: Annotated[dict[Code, PositiveFloat], Field(min_length=1)]
    """Defect code -> relative weight."""
    robot_wear_gain: NonNegativeFloat | None = None
    """Added defect probability per unit of mean wear of ``wear_equipment_type`` units."""
    wear_equipment_type: Ident | None = None
    dp_gain: NonNegativeFloat | None = None
    dp_from_pa: NonNegativeFloat | None = None
    humidity_out_of_spec_add: Fraction | None = None
    """Added defect probability while ``humidity_signal`` is outside its warn_lo..warn_hi."""
    humidity_signal: Ident | None = None

    @model_validator(mode="after")
    def _pairs(self) -> AreaDefects:
        pairs = (
            ("dp_gain", "dp_from_pa"),
            ("robot_wear_gain", "wear_equipment_type"),
            ("humidity_out_of_spec_add", "humidity_signal"),
        )
        for first, second in pairs:
            if (getattr(self, first) is None) != (getattr(self, second) is None):
                raise ValueError(f"{first} and {second} must be given together")
        return self


class SimDefects(_ExtrasModel):
    """``scrap_share_of_defects`` plus one :class:`AreaDefects` per area code."""

    scrap_share_of_defects: Fraction
    __pydantic_extra__: dict[str, AreaDefects] = Field(init=False)

    @property
    def per_area(self) -> dict[str, AreaDefects]:
        return dict(self.__pydantic_extra__ or {})


class Telemetry(_ExtrasModel):
    """Sampling periods plus ``equipment type -> signal -> model expression``."""

    sample_period_s: PositiveFloat
    backfill_sample_period_s: PositiveFloat
    __pydantic_extra__: dict[str, dict[str, NonEmptyStr]] = Field(init=False)

    @property
    def per_type(self) -> dict[str, dict[str, str]]:
        return dict(self.__pydantic_extra__ or {})


class Triangular(StrictModel):
    dist: Literal["triangular"]
    min: NonNegativeFloat
    mode: NonNegativeFloat
    max: NonNegativeFloat

    @model_validator(mode="after")
    def _order(self) -> Triangular:
        if not self.min <= self.mode <= self.max:
            raise ValueError("triangular distribution needs min <= mode <= max")
        return self


class CkdSupply(StrictModel):
    initial_kits: dict[Code, NonNegativeInt]
    delivery_every_working_days: PositiveInt
    lot_days_of_plan: PositiveFloat
    delay_days: Triangular


class CalibrationTargets(StrictModel):
    throughput_per_shift: Range
    defect_rate: dict[Code, tuple[Fraction, Fraction]]
    """Area code -> [min, max] defect rate."""
    unplanned_downtime_min_per_area_day: Range
    expected_bottleneck: Code

    @model_validator(mode="after")
    def _ranges(self) -> CalibrationTargets:
        for area, (low, high) in self.defect_rate.items():
            if low > high:
                raise ValueError(f"defect_rate[{area}]: min {low} exceeds max {high}")
        return self


class DatasetSplit(StrictModel):
    train_months: PositiveInt
    val_months: PositiveInt
    test_months: PositiveInt


class MlDataset(StrictModel):
    months: PositiveInt
    from_: AwareDatetime = Field(alias="from")
    random_seed: int
    window_min: PositiveInt
    telemetry_period_s: PositiveFloat
    horizon_h: PositiveFloat
    split: DatasetSplit

    @model_validator(mode="after")
    def _split_total(self) -> MlDataset:
        total = self.split.train_months + self.split.val_months + self.split.test_months
        if total != self.months:
            raise ValueError(f"split adds up to {total} months but months = {self.months}")
        return self


class FailureInject(StrictModel):
    type: Literal["failure"]
    equipment: Code
    reason: Code
    duration_min: PositiveFloat


class SetStateInject(_ExtrasModel):
    """Override hidden state of one unit: ``degradation`` and/or signals of its type."""

    type: Literal["set_state"]
    equipment: Code
    __pydantic_extra__: dict[str, float] = Field(init=False)

    @property
    def values(self) -> dict[str, float]:
        return dict(self.__pydantic_extra__ or {})

    @model_validator(mode="after")
    def _has_values(self) -> SetStateInject:
        if not self.__pydantic_extra__:
            raise ValueError("set_state needs at least one state value to set")
        return self


class DefectMultiplierInject(StrictModel):
    type: Literal["defect_multiplier"]
    areas: Annotated[list[Code], Field(min_length=1)]
    factor: PositiveFloat
    duration_min: PositiveFloat


class CkdInject(StrictModel):
    type: Literal["ckd"]
    product: Code
    set_kits: NonNegativeInt | None = None
    delay_next_lot_days: NonNegativeFloat | None = None


Inject = Annotated[
    FailureInject | SetStateInject | DefectMultiplierInject | CkdInject,
    Field(discriminator="type"),
]


class Scenario(Named):
    id: Code
    inject: Inject
    at_min: NonNegativeFloat | None
    """Offset from ``clock.demo_start`` in plant minutes; null = manual trigger only."""


class SimulationConfig(StrictModel):
    """Root of ``simulation.yaml``."""

    version: Literal[1]
    clock: SimClock
    process: Process
    failures: dict[Ident, FailureModel]
    microstops: dict[Ident, Microstop] = {}
    degradation: Degradation
    paint_filters: PaintFilters | None = None
    planned_maintenance: list[PlannedMaintenance] = []
    defects: SimDefects
    telemetry: Telemetry
    ckd_supply: CkdSupply
    calibration_targets: CalibrationTargets
    ml_dataset: MlDataset
    scenarios: list[Scenario] = []
