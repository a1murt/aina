"""Models for ``config/business.yaml`` — economic-effect assumptions (SPEC §10.5)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints

from twin_core.config.common import Code, Fraction, NonNegativeFloat, StrictModel
from twin_core.domain import FilterPolicy


class _Param(StrictModel):
    assumption: bool
    """True: a team assumption — shown with an "assumption" badge in the UI and editable."""
    source: str | None = None
    note: str | None = None


class ScalarParam(_Param):
    value: NonNegativeFloat


class PerAreaParam(_Param):
    value: dict[Code, NonNegativeFloat]
    """Keyed by area code."""


class BusinessParams(StrictModel):
    avg_price_kzt: ScalarParam
    margin_rate: ScalarParam
    rework_cost_kzt: PerAreaParam
    saturday_shift_cost_kzt: ScalarParam


class ImprovementDefaults(StrictModel):
    """Conservative "with the system" scenario used as forecast overrides."""

    mttr_reduction: Fraction
    unplanned_failure_reduction: Fraction
    filter_policy: FilterPolicy
    paint_defect_rate_target: Fraction


class Reporting(StrictModel):
    show_revenue: bool


class BusinessConfig(StrictModel):
    """Root of ``business.yaml``."""

    version: Literal[1]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    params: BusinessParams
    improvement_defaults: ImprovementDefaults
    reporting: Reporting
