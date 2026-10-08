"""Models for ``simulation.yaml: forecast`` — calibration and fast Monte Carlo (SPEC §10).

The section is optional; every key has a default, so a missing section keeps working.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from twin_core.config.common import (
    Code,
    Ident,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    StrictModel,
)

EffBasis = Literal["net", "iso"]
"""``net``: line speed efficiency without the losses the model applies itself (B-class degraded
time, repaint passes); ``iso``: plain ISO 22400 effectiveness (counts those losses twice)."""

LeverRank = Literal["effect_kzt", "delta_p50"]


class RunCounts(StrictModel):
    default: PositiveInt = 5000
    max: PositiveInt = 20000
    levers: PositiveInt = 2000
    min: PositiveInt = 100

    @model_validator(mode="after")
    def _order(self) -> RunCounts:
        if not self.min <= self.default <= self.max or not self.min <= self.levers <= self.max:
            raise ValueError("n_runs: need min <= default <= max and min <= levers <= max")
        return self


class Priors(StrictModel):
    prior_strength: PositiveFloat = 2.0
    """Pseudo-failures of the Gamma prior on a failure rate (alpha0)."""
    min_failures: PositiveInt = 3
    """Fewer failures in the window -> Bayesian estimate with the prior (SPEC §10.1)."""
    min_repairs: PositiveInt = 3
    eff_min_shifts: PositiveInt = 5
    eff_min_apt_min: NonNegativeFloat = 60.0
    """Shifts with less production time do not enter the efficiency estimate."""
    eff_concentration: PositiveFloat = 200.0
    """alpha + beta of the prior Beta of line efficiency."""
    eff_concentration_min: PositiveFloat = 10.0
    eff_concentration_max: PositiveFloat = 10000.0
    defect_min_units: PositiveInt = 200
    defect_concentration: PositiveFloat = 100.0
    filter_min_lives: PositiveInt = 3


class PredictiveFilter(StrictModel):
    service_loss_min: NonNegativeFloat = 0.0
    """Production minutes a planned filter swap at a shift change takes from the next shift
    (absorbed by a non-working gap such as the night)."""


class ExtraShiftLever(StrictModel):
    weekday: Annotated[int, Field(ge=1, le=7)] = 6
    """ISO weekday of the "+1 shift" lever (6 = Saturday)."""
    shifts: Annotated[list[Code], Field(min_length=1)] = ["A"]


class BufferLever(StrictModel):
    code: Code = "PBS"
    add: PositiveInt = 10


class CandidateShifts(StrictModel):
    """Which non-working shifts may be added when searching for the shifts a target needs."""

    weekdays: Annotated[list[Annotated[int, Field(ge=1, le=7)]], Field(min_length=0)] = [6]
    """ISO weekdays (6 = Saturday, 7 = Sunday)."""
    shifts: list[Code] | None = None
    """Shift codes per candidate day (``None`` = every calendar shift)."""
    include_holidays: bool = False


def _default_candidates() -> dict[str, CandidateShifts]:
    return {
        "saturdays": CandidateShifts(weekdays=[6]),
        "weekends_and_holidays": CandidateShifts(weekdays=[6, 7], include_holidays=True),
    }


class ShiftsNeeded(StrictModel):
    """ "How many extra shifts make P(total >= target) exceed ``p_goal``" per candidate set."""

    p_goal: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.5
    candidates: dict[Ident, CandidateShifts] = Field(default_factory=_default_candidates)


class Levers(StrictModel):
    failure_free_multiplier: PositiveFloat = 1000.0
    """``mtbf_multiplier`` that stands for "no unplanned failures" within a month."""
    extra_shift: ExtraShiftLever = ExtraShiftLever()
    buffer: BufferLever = BufferLever()
    top: PositiveInt = 5
    rank_by: LeverRank = "effect_kzt"
    shifts_needed: ShiftsNeeded = ShiftsNeeded()


class ForecastConfig(StrictModel):
    """``simulation.yaml: forecast``."""

    window_days: PositiveInt = 20
    """Calibration window: the last N complete working days (SPEC §10.1)."""
    step_min: Annotated[int, Field(ge=5, le=60)] = 60
    """Model step, plant minutes of working time (SPEC §10.2: 1 hour)."""
    seed: int | None = None
    """Default seed of forecast runs; ``None`` = ``clock.random_seed``."""
    n_runs: RunCounts = RunCounts()
    parameter_uncertainty: bool = True
    """Draw rates, efficiencies and defect shares from their posteriors per run."""
    eff_basis: EffBasis = "net"
    priors: Priors = Priors()
    predictive_filter: PredictiveFilter = PredictiveFilter()
    histogram_bins: Annotated[int, Field(ge=5, le=200)] = 30
    levers: Levers = Levers()
    max_concurrent: PositiveInt = 2
    """Forecast computations running at the same time in one API process."""
