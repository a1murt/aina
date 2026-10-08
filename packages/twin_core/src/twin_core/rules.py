"""Alert rules ``AL-*`` evaluated on aggregates (SPEC §9.7, ``rules.yaml: alert_rules``).

:class:`AlertEvaluator` holds the thresholds and the rule definitions; each method checks one
rule for one entity and period and returns an :class:`Alert` or ``None``. A rule missing from
``rules.yaml`` is disabled. Fixed severities come from the rule (``severity: warning``);
threshold-derived ones (AL-Q1, AL-D1) from ``thresholds``. Values are compared rounded to
4 digits (SPEC §5.4). The import path (closed imported shifts) uses it now, the engine (live
closed shifts) from M3.

Deduplication key = ``rule_id + entity + period`` (:attr:`Alert.dedup_key`): repeating the
same finding updates the existing alert instead of creating a new one (:func:`deduplicate`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from twin_core.config.rules import AlertRule, RulesConfig, Thresholds
from twin_core.domain import Criticality, Severity
from twin_core.kpi import round_fraction

AL_EQUIPMENT_STOP: Final = "AL-S1"
AL_DOWNTIME_LIMIT: Final = "AL-D1"
AL_OEE_BELOW: Final = "AL-O1"
AL_OEE_NEAR: Final = "AL-O2"
AL_DEFECT_RATE: Final = "AL-Q1"
AL_SYSTEMIC_DEFECTS: Final = "AL-Q3"
AL_BUFFER: Final = "AL-B1"
AL_CKD_COVERAGE: Final = "AL-L1"
AL_PLAN_RISK: Final = "AL-P1"

PLANT_ENTITY: Final = "PLANT"
"""Entity of plant-wide alerts (AL-Q3)."""

AlertValue = float | dict[str, Any]
"""A number (rate, minutes) or a JSON object (live rules: elapsed, reason, impact, ...)."""


@dataclass(frozen=True, slots=True)
class Alert:
    """An alert finding before persistence (row of ``alert``)."""

    rule_id: str
    severity: Severity
    entity_type: str
    """``area`` | ``line`` | ``equipment`` | ``site``."""
    entity: str
    period_date: date
    value: AlertValue
    shift: str | None = None
    """Shift code for per-shift rules; ``None`` for daily / plant-wide periods."""
    period_key: str | None = None
    """Explicit period of event-like rules (AL-S1/B1/L1: start of the stop or excursion, ISO UTC);
    overrides the day/shift period in the deduplication key."""

    @property
    def period(self) -> str:
        """``2026-10-02`` for a day, ``2026-10-02/A`` for a shift, or :attr:`period_key`."""
        if self.period_key:
            return self.period_key
        day = self.period_date.isoformat()
        return f"{day}/{self.shift}" if self.shift else day

    @property
    def dedup_key(self) -> str:
        return f"{self.rule_id}|{self.entity}|{self.period}"

    def to_report(self) -> dict[str, Any]:
        """The import-report form (``import_expected.json: alerts``)."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "entity": self.entity,
            "date": self.period_date.isoformat(),
            "value": dict(self.value) if isinstance(self.value, dict) else self.value,
        }


def deduplicate(alerts: Iterable[Alert]) -> list[Alert]:
    """Keep one alert per dedup key (first position, latest value and severity)."""
    merged: dict[str, Alert] = {}
    for alert in alerts:
        merged[alert.dedup_key] = alert
    return list(merged.values())


class AlertEvaluator:
    """Evaluates aggregate alert rules with thresholds and rule definitions from ``rules.yaml``.

    ``thresholds`` may be passed separately to apply UI overrides (``settings`` table, M4).
    """

    def __init__(self, rules: RulesConfig, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or rules.thresholds
        self._rules = {rule.id: rule for rule in rules.alert_rules}

    def enabled(self, rule_id: str) -> bool:
        return rule_id in self._rules

    def rule(self, rule_id: str) -> AlertRule | None:
        return self._rules.get(rule_id)

    def fixed_severity(self, rule_id: str) -> Severity | None:
        """Severity configured as a single value for the rule (``None`` if derived/absent)."""
        rule = self._rules.get(rule_id)
        if rule is None or not isinstance(rule.severity, str):
            return None
        return rule.severity

    def defect_rate(
        self, *, area: str, period_date: date, shift: str | None, defect_rate: float | None
    ) -> Alert | None:
        """AL-Q1: defect share of an area for a shift > limit (warning) / > critical (critical)."""
        value = round_fraction(defect_rate)
        if not self.enabled(AL_DEFECT_RATE) or value is None:
            return None
        t = self.thresholds
        if value <= t.defect_rate_limit:
            return None
        severity: Severity = "critical" if value > t.defect_rate_critical else "warning"
        return Alert(AL_DEFECT_RATE, severity, "area", area, period_date, value, shift)

    def oee(
        self, *, line: str, period_date: date, shift: str | None, oee: float | None
    ) -> Alert | None:
        """AL-O1: OEE < target; else AL-O2: target <= OEE < target + near margin."""
        value = round_fraction(oee)
        if value is None:
            return None
        t = self.thresholds
        if value < t.oee_target:
            rule_id = AL_OEE_BELOW
        elif value < t.oee_target + t.oee_near_margin_pp / 100:
            rule_id = AL_OEE_NEAR
        else:
            return None
        severity = self.fixed_severity(rule_id)
        if severity is None:
            return None
        return Alert(rule_id, severity, "line", line, period_date, value, shift)

    def critical_downtime(
        self, *, equipment: str, period_date: date, unplanned_min: float
    ) -> Alert | None:
        """AL-D1: unplanned downtime of a class-A unit over a day vs the daily limit.

        >= limit -> critical; >= limit x warn ratio -> warning. The caller passes class A units
        only and unplanned minutes only (planned maintenance is excluded, D3).
        """
        if not self.enabled(AL_DOWNTIME_LIMIT):
            return None
        t = self.thresholds
        limit = t.critical_downtime_limit_min_per_day
        if unplanned_min >= limit:
            severity: Severity = "critical"
        elif unplanned_min >= limit * t.critical_downtime_warn_ratio:
            severity = "warning"
        else:
            return None
        return Alert(
            AL_DOWNTIME_LIMIT, severity, "equipment", equipment, period_date, unplanned_min
        )

    def systemic_defects(
        self,
        *,
        period_date: date,
        previous: Mapping[str, float | None],
        current: Mapping[str, float | None],
        shift: str | None = None,
    ) -> Alert | None:
        """AL-Q3: defect share rose on every area of the flow and at least one is over the limit.

        ``previous`` / ``current`` map area code -> defect share for consecutive periods; only
        areas known in both periods are compared.
        """
        severity = self.fixed_severity(AL_SYSTEMIC_DEFECTS)
        if severity is None:
            return None
        cur = {area: round_fraction(rate) for area, rate in current.items()}
        prev = {area: round_fraction(rate) for area, rate in previous.items()}
        areas = [a for a in cur if cur[a] is not None and prev.get(a) is not None]
        if not areas:
            return None
        rates = {a: v for a in areas if (v := cur[a]) is not None}
        rose = all(rates[a] > (prev[a] or 0.0) for a in areas)
        over = any(rates[a] > self.thresholds.defect_rate_limit for a in areas)
        if not (rose and over):
            return None
        return Alert(AL_SYSTEMIC_DEFECTS, severity, "site", PLANT_ENTITY, period_date, rates, shift)

    # ------------------------------------------------------------------ live rules (M3)

    def equipment_stop(
        self,
        *,
        equipment: str,
        criticality: Criticality,
        period_date: date,
        started_key: str,
        elapsed_s: float,
        value: Mapping[str, Any],
    ) -> Alert | None:
        """AL-S1: unplanned stop of a unit. Class A alerts at once; B and C after the stop lasts
        ``microstop_threshold_s`` (5 min). Severity per criticality from ``rules.yaml``."""
        rule = self._rules.get(AL_EQUIPMENT_STOP)
        if rule is None:
            return None
        if criticality != "A" and elapsed_s < self.thresholds.microstop_threshold_s:
            return None
        if isinstance(rule.severity, dict):
            severity = rule.severity.get(criticality)
        else:
            severity = rule.severity
        if severity is None:
            return None
        return Alert(
            AL_EQUIPMENT_STOP,
            severity,
            "equipment",
            equipment,
            period_date,
            dict(value),
            period_key=started_key,
        )

    def buffer_condition(self, *, level: int, capacity: int) -> tuple[Severity, str] | None:
        """AL-B1 condition: (severity, ``low``|``high``) or ``None`` inside the normal band.

        level = 0 -> critical; < low ratio x capacity -> warning; > high ratio x capacity ->
        warning (risk of blocking the upstream line)."""
        if not self.enabled(AL_BUFFER) or capacity <= 0:
            return None
        t = self.thresholds
        if level <= 0:
            return "critical", "low"
        if level < t.buffer_low_ratio * capacity:
            return "warning", "low"
        if level > t.buffer_high_ratio * capacity:
            return "warning", "high"
        return None

    def buffer_level(
        self,
        *,
        buffer: str,
        level: int,
        capacity: int,
        period_date: date,
        started_key: str,
    ) -> Alert | None:
        """AL-B1 alert for one excursion (``started_key`` = its start)."""
        condition = self.buffer_condition(level=level, capacity=capacity)
        if condition is None:
            return None
        severity, direction = condition
        return Alert(
            AL_BUFFER,
            severity,
            "buffer",
            buffer,
            period_date,
            {"level": level, "capacity": capacity, "direction": direction},
            period_key=started_key,
        )

    def ckd_coverage(
        self,
        *,
        product: str,
        kits: int,
        daily_plan: float,
        period_date: date,
        started_key: str,
    ) -> Alert | None:
        """AL-L1: kits of a model cover less than ``ckd_coverage_min_days`` of its daily plan."""
        severity = self.fixed_severity(AL_CKD_COVERAGE)
        if severity is None or daily_plan <= 0:
            return None
        days = kits / daily_plan
        if round(days, 4) >= self.thresholds.ckd_coverage_min_days:
            return None
        return Alert(
            AL_CKD_COVERAGE,
            severity,
            "product",
            product,
            period_date,
            {"kits": kits, "daily_plan": round(daily_plan, 2), "coverage_days": round(days, 2)},
            period_key=started_key,
        )

    def plan_risk(self, *, period_date: date, forecast: Mapping[str, Any]) -> Alert | None:
        """AL-P1: P(month total >= target) < ``plan_risk_warn_p`` (warning) / ``plan_risk_crit_p``
        (critical).

        ``forecast`` is a forecast result (``ForecastResult.model_dump(mode="json")`` or the JSON
        stored in ``forecast_run.result``). The target is ``thresholds.plan_risk_target``:
        ``line_plan`` by default, because the plant target (5 500) is above the line ceiling
        without extra shifts (123.6 x 42 = 5 191) and would keep the alert critical all month.
        """
        if not self.enabled(AL_PLAN_RISK):
            return None
        t = self.thresholds
        p = (forecast.get("p_reach") or {}).get(t.plan_risk_target)
        qty = (forecast.get("targets") or {}).get(t.plan_risk_target)
        if p is None or qty is None:
            return None
        prob = round_fraction(float(p))
        severity: Severity
        if prob < t.plan_risk_crit_p:
            severity = "critical"
        elif prob < t.plan_risk_warn_p:
            severity = "warning"
        else:
            return None
        value: dict[str, float] = {"p": prob, "target_qty": float(qty)}
        summary = forecast.get("summary") or {}
        if "p50" in summary:
            value["p50"] = float(summary["p50"])
        return Alert(AL_PLAN_RISK, severity, "site", PLANT_ENTITY, period_date, value)
