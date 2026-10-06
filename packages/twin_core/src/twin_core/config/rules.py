"""Models for ``config/rules.yaml``: thresholds, alert rules, escalation, DQ (SPEC §9.6-9.7)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from twin_core.config.common import (
    Fraction,
    Ident,
    NonEmptyStr,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    StrictModel,
)
from twin_core.domain import Channel, Criticality, Severity

AlertRuleId = Annotated[str, StringConstraints(pattern=r"^AL-[A-Z0-9]+$")]
DqRuleId = Annotated[str, StringConstraints(pattern=r"^DQ-\d{2}$")]


class Thresholds(StrictModel):
    oee_target: Fraction
    oee_near_margin_pp: NonNegativeFloat
    defect_rate_limit: Fraction
    defect_rate_critical: Fraction
    critical_downtime_limit_min_per_day: PositiveFloat
    critical_downtime_warn_ratio: Fraction
    plant_target_per_month: PositiveInt
    microstop_threshold_s: PositiveFloat
    buffer_low_ratio: Fraction
    buffer_high_ratio: Fraction
    plan_risk_warn_p: Fraction
    plan_risk_crit_p: Fraction
    pdm_horizon_h: PositiveFloat
    pdm_warn_p: Fraction
    pdm_crit_p: Fraction
    telemetry_limit_lookahead_h: PositiveFloat

    @model_validator(mode="after")
    def _ordering(self) -> Thresholds:
        pairs = [
            ("defect_rate_limit", "defect_rate_critical"),
            ("buffer_low_ratio", "buffer_high_ratio"),
            ("plan_risk_crit_p", "plan_risk_warn_p"),
            ("pdm_warn_p", "pdm_crit_p"),
        ]
        for low, high in pairs:
            if getattr(self, low) >= getattr(self, high):
                raise ValueError(
                    f"{low} ({getattr(self, low)}) must be below {high} ({getattr(self, high)})"
                )
        return self


class DataQualityThresholds(StrictModel):
    load_mismatch_pp: NonNegativeFloat
    downtime_recon_min: NonNegativeFloat
    flow_balance_warn_units: PositiveInt


class AlertRule(StrictModel):
    id: AlertRuleId
    name_ru: NonEmptyStr
    name_kk: NonEmptyStr | None = None
    when: NonEmptyStr
    """Human-readable condition; the logic itself lives in the engine (by rule id)."""
    severity: Severity | dict[Criticality, Severity] | None = None
    """Fixed severity, per-criticality severity, or None when derived from thresholds."""
    recipients: Annotated[list[Ident], Field(min_length=1)]
    channels: Annotated[list[Channel], Field(min_length=1)]


class EscalationLevel(StrictModel):
    timeout_min: PositiveFloat
    chain: Annotated[list[Ident], Field(min_length=1)]


class DqRule(StrictModel):
    id: DqRuleId
    name_ru: NonEmptyStr
    name_kk: NonEmptyStr | None = None


class RulesConfig(StrictModel):
    """Root of ``rules.yaml``."""

    version: Literal[1]
    thresholds: Thresholds
    data_quality: DataQualityThresholds
    roles: Annotated[list[Ident], Field(min_length=1)]
    alert_rules: list[AlertRule] = []
    escalation: dict[Severity, EscalationLevel] = {}
    data_quality_rules: list[DqRule] = []
