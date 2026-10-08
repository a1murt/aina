"""Models for ``config/rules.yaml``: thresholds, alert rules, escalation, DQ (SPEC §9.6-9.7)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from twin_core.config.common import (
    Fraction,
    Ident,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StrictModel,
)
from twin_core.domain import Channel, Criticality, Severity

AlertRuleId = Annotated[str, StringConstraints(pattern=r"^AL-[A-Z0-9]+$")]
DqRuleId = Annotated[str, StringConstraints(pattern=r"^DQ-\d{2}$")]
PlanRiskTarget = Literal["line_plan", "plant_target"]


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
    plan_risk_target: PlanRiskTarget = "line_plan"
    """Which monthly target AL-P1 watches: the line plan (4 800) or the plant target (5 500)."""
    pdm_horizon_h: PositiveFloat
    pdm_warn_p: Fraction
    pdm_crit_p: Fraction
    telemetry_limit_lookahead_h: PositiveFloat
    ckd_coverage_min_days: PositiveFloat = 2.0
    """AL-L1: kits of a model / its daily plan below this many days -> warning."""

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


class EngineParams(StrictModel):
    """Live engine parameters (SPEC §9; defaults are the SPEC values). All times are plant time."""

    kpi_tick_s: PositiveFloat = 5.0
    """FR-KPI-03: live KPIs are recomputed at least this often."""
    bottleneck_window_h: PositiveFloat = 4.0
    """§9.5: rolling window of the live bottleneck (besides the current shift)."""
    buffer_balance_window_min: PositiveFloat = 30.0
    """§9.4: window of the in/out balance for time to empty / full."""
    unclassified_after_min: PositiveFloat = 10.0
    """FR-ENG-04: stops without a reason older than this need classification."""
    alert_debounce_s: NonNegativeFloat = 300.0
    """AL-B1 / AL-L1: a condition must hold (and clear) this long before it raises (resolves)."""
    bottleneck_rate_shifts: PositiveInt = 5
    """FR-ENG-06: bottleneck rate = mean PQ of this many last closed shifts."""
    live_oee_min_elapsed_min: NonNegativeFloat = 120.0
    """AL-O1/O2 on the running shift only after this much of it has elapsed (projection)."""
    live_kpi_min_apt_min: NonNegativeFloat = 15.0
    """Live E and OEE are published as ``null`` while the running shift has less APT than this:
    a body started before the shift boundary makes E > 1 on a tiny APT (display only; stored
    ``kpi_shift`` rows keep full values)."""
    s1_resolve_microstops: bool = True
    """AL-S1 for class A fires at once; resolve it if the stop ends as a microstop."""
    dq_live_lost: Literal["downtime", "literal"] = "downtime"
    """DQ-02 live: compare the journal with PDOT + ADOT (``downtime``) or with PBT - APT
    (``literal``, SPEC wording; it also counts flow delays and misses planned time)."""
    flow_wip_tolerance: NonNegativeInt = 2
    """DQ-04 live: residual units tolerated at shift boundaries — one body held by a blocked
    upstream line plus one in process in the downstream line."""
    pdm_tick_min: PositiveFloat = 15.0
    """SPEC §11.1: PdM serving (p_failure, health index, AL-M1/AL-M2) every N plant minutes."""
    pdm_resolve_ratio: Annotated[float, Field(gt=0, le=1)] = 0.8
    """AL-M1 resolves when p_failure falls below ``ratio x pdm_warn_p`` (hysteresis)."""
    pdm_lookahead_clear: PositiveFloat = 1.0
    """AL-M2 resolves when the time to the limit exceeds the look-ahead by this many hours."""
    pdm_fresh_s: PositiveFloat = 150.0
    """AL-M2 ignores a signal whose newest sample is older than this (stale data raises nothing)."""
    pdm_stops_days: PositiveInt = 60
    """Stop history given to the PdM features (hours since maintenance / repair, cycles)."""
    spc_history_shifts: PositiveInt = 40
    """Closed shifts per area kept for the p-chart (baseline is the last 20, SPEC §11.3)."""


class RulesConfig(StrictModel):
    """Root of ``rules.yaml``."""

    version: Literal[1]
    thresholds: Thresholds
    data_quality: DataQualityThresholds
    roles: Annotated[list[Ident], Field(min_length=1)]
    alert_rules: list[AlertRule] = []
    escalation: dict[Severity, EscalationLevel] = {}
    data_quality_rules: list[DqRule] = []
    engine: EngineParams = EngineParams()
