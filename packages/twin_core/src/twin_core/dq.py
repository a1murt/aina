"""Data-quality reconciliation rules DQ-01 … DQ-07 (SPEC §9.6, ``rules.yaml: data_quality``).

Each check is a pure function returning a :class:`DqIssue` or ``None``. Thresholds come from
``rules.yaml: data_quality`` (:class:`twin_core.config.rules.DataQualityThresholds`); severities
are part of the rule definitions in SPEC §9.6. Values are compared the way SPEC §5.4 prescribes
(fractions rounded to 4 digits, percentages to 0.1 pp). The same functions serve the import path
(aggregates) and, from M3, the live engine (closed shifts).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from twin_core.config.rules import DataQualityThresholds
from twin_core.domain import Severity
from twin_core.kpi import buffer_change, fraction_as_pp, round_fraction, round_pp

DQ_LOAD_MISMATCH: Final = "DQ-01"
DQ_DOWNTIME_RECON: Final = "DQ-02"
DQ_NO_SHIFT: Final = "DQ-03"
DQ_FLOW_BALANCE: Final = "DQ-04"
DQ_PLAN_TARGET: Final = "DQ-05"
DQ_EFFECTIVENESS: Final = "DQ-06"
DQ_UNKNOWN_VALUE: Final = "DQ-07"

DOWNTIME_LOG_ENTITY: Final = "downtime_log"
PLAN_ENTITY: Final = "production_plan"

LOG_EXCEEDS_LOSS: Final = "log_exceeds_loss"
"""DQ-02: part of the logged downtime fell into another shift, or a unit had a bypass."""
UNLOGGED_LOSS: Final = "unlogged_loss"
"""DQ-02: lost time without logged stops (unregistered stops or microstops)."""


@dataclass(frozen=True, slots=True)
class DqIssue:
    """One reconciliation finding (row of ``dq_issue``)."""

    rule_id: str
    severity: Severity
    entity: str
    period_date: date | None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_report(self) -> dict[str, Any]:
        """The import-report form (``import_expected.json: data_quality_issues``)."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "entity": self.entity,
            "date": self.period_date.isoformat() if self.period_date else None,
            "details": dict(self.details),
        }


def check_load_mismatch(
    *,
    line: str,
    period_date: date,
    reported_load_pct: float | None,
    availability: float | None,
    thresholds: DataQualityThresholds,
) -> DqIssue | None:
    """DQ-01: |reported load - round(A x 100, 1)| > ``load_mismatch_pp`` -> info."""
    computed = fraction_as_pp(availability)
    if reported_load_pct is None or computed is None:
        return None
    diff = reported_load_pct - computed
    if abs(diff) <= thresholds.load_mismatch_pp:
        return None
    return DqIssue(
        DQ_LOAD_MISMATCH,
        "info",
        line,
        period_date,
        {
            "reported_load_pct": reported_load_pct,
            "computed_availability_pct": computed,
            "diff_pp": round_pp(diff),
        },
    )


def check_downtime_reconciliation(
    *,
    area: str,
    period_date: date,
    logged_min: float,
    lost_min: float,
    thresholds: DataQualityThresholds,
) -> DqIssue | None:
    """DQ-02: logged downtime (planned + unplanned) vs lost time PBT - APT of the shift.

    Checked where anything was logged or lost; |difference| >= ``downtime_recon_min`` -> warning.
    """
    if logged_min == 0 and lost_min == 0:
        return None
    diff = logged_min - lost_min
    if abs(diff) < thresholds.downtime_recon_min:
        return None
    return DqIssue(
        DQ_DOWNTIME_RECON,
        "warning",
        area,
        period_date,
        {
            "logged_downtime_min": logged_min,
            "lost_time_min": lost_min,
            "diff_min": round_fraction(diff),
            "direction": LOG_EXCEEDS_LOSS if diff > 0 else UNLOGGED_LOSS,
        },
    )


def check_records_without_shift(count: int) -> DqIssue | None:
    """DQ-03: downtime records without shift/time -> one info issue per import with the count."""
    if count <= 0:
        return None
    return DqIssue(DQ_NO_SHIFT, "info", DOWNTIME_LOG_ENTITY, None, {"records_without_shift": count})


def check_flow_balance(
    *,
    upstream: str,
    downstream: str,
    upstream_produced: int,
    downstream_produced: int,
    thresholds: DataQualityThresholds,
) -> DqIssue | None:
    """DQ-04: downstream produced more than it received over the period (buffer drained).

    info when the buffer change is negative; warning when |change| >= ``flow_balance_warn_units``.
    """
    delta = int(buffer_change(upstream_produced, downstream_produced))
    if delta >= 0:
        return None
    severity: Severity = "warning" if abs(delta) >= thresholds.flow_balance_warn_units else "info"
    return DqIssue(
        DQ_FLOW_BALANCE,
        severity,
        f"{upstream}->{downstream}",
        None,
        {
            "upstream_produced": upstream_produced,
            "downstream_produced": downstream_produced,
            "buffer_change_units": delta,
        },
    )


def check_plan_vs_target(*, line_model_plan: int, plant_target: int) -> DqIssue | None:
    """DQ-05: sum of line/model plans differs from the plant target -> warning with the gap."""
    if line_model_plan == plant_target:
        return None
    return DqIssue(
        DQ_PLAN_TARGET,
        "warning",
        PLAN_ENTITY,
        None,
        {
            "line_model_plan": line_model_plan,
            "plant_target": plant_target,
            "gap": line_model_plan - plant_target,
        },
    )


def check_effectiveness(
    *, line: str, period_date: date, effectiveness: float | None
) -> DqIssue | None:
    """DQ-06: E > 1.0 means the ideal cycle time (ICT) is set too slow -> warning."""
    value = round_fraction(effectiveness)
    if value is None or value <= 1.0:
        return None
    return DqIssue(DQ_EFFECTIVENESS, "warning", line, period_date, {"effectiveness": value})


def unknown_value(
    *,
    kind: str,
    value: str,
    suggestion: str | None,
    period_date: date | None = None,
    **context: Any,
) -> DqIssue:
    """DQ-07: a value that maps to no known code (import cell, tag) -> warning.

    ``kind`` is what was expected (``equipment``, ``reason``, ``number``, ``date``, …);
    ``suggestion`` the closest known code by Levenshtein distance (FR-IMP-02), if any;
    ``context`` locates the value (``table``, ``row``, ``column``, ``tag``, …).
    """
    details: dict[str, Any] = {"kind": kind, "value": value, "suggestion": suggestion}
    details.update(context)
    return DqIssue(DQ_UNKNOWN_VALUE, "warning", kind, period_date, details)
