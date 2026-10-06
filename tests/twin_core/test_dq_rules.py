"""DQ-01…07 (SPEC §9.6), aggregate alert rules AL-* (SPEC §9.7) and the aggregate bottleneck."""

from __future__ import annotations

from datetime import date

import pytest

from twin_core.bottleneck import aggregate_bottleneck
from twin_core.config import TwinConfig
from twin_core.config.rules import RulesConfig
from twin_core.dq import (
    DqIssue,
    check_downtime_reconciliation,
    check_effectiveness,
    check_flow_balance,
    check_load_mismatch,
    check_plan_vs_target,
    check_records_without_shift,
    unknown_value,
)
from twin_core.rules import Alert, AlertEvaluator, deduplicate

D1 = date(2026, 10, 1)
D2 = date(2026, 10, 2)


# --------------------------------------------------------------------------- DQ


def test_dq01_load_mismatch(cfg: TwinConfig) -> None:
    t = cfg.rules.data_quality
    issue = check_load_mismatch(
        line="WELD-1", period_date=D2, reported_load_pct=91.0, availability=0.9, thresholds=t
    )
    assert issue is not None
    assert issue.severity == "info"
    assert issue.to_report()["details"] == {
        "reported_load_pct": 91.0,
        "computed_availability_pct": 90.0,
        "diff_pp": 1.0,
    }
    # 0.5 pp exactly is not above the threshold; values are rounded to 0.1 pp first.
    assert (
        check_load_mismatch(
            line="L", period_date=D1, reported_load_pct=98.0, availability=0.97504, thresholds=t
        )
        is None
    )
    assert (
        check_load_mismatch(
            line="L", period_date=D1, reported_load_pct=None, availability=0.9, thresholds=t
        )
        is None
    )


def test_dq02_downtime_reconciliation(cfg: TwinConfig) -> None:
    t = cfg.rules.data_quality
    over = check_downtime_reconciliation(
        area="ASSY", period_date=D2, logged_min=55.0, lost_min=6.0, thresholds=t
    )
    assert over is not None
    assert over.details["direction"] == "log_exceeds_loss"
    assert over.details["diff_min"] == 49.0
    under = check_downtime_reconciliation(
        area="PAINT", period_date=D2, logged_min=0.0, lost_min=18.0, thresholds=t
    )
    assert under is not None
    assert under.details["direction"] == "unlogged_loss"
    assert (
        check_downtime_reconciliation(
            area="X", period_date=D1, logged_min=40.0, lost_min=30.0, thresholds=t
        )
        is not None
    ), "a difference of exactly downtime_recon_min is reported"
    assert (
        check_downtime_reconciliation(
            area="X", period_date=D1, logged_min=35.0, lost_min=30.0, thresholds=t
        )
        is None
    )
    assert (
        check_downtime_reconciliation(
            area="X", period_date=D1, logged_min=0.0, lost_min=0.0, thresholds=t
        )
        is None
    )


def test_dq03_to_dq06(cfg: TwinConfig) -> None:
    t = cfg.rules.data_quality
    assert check_records_without_shift(0) is None
    no_shift = check_records_without_shift(4)
    assert no_shift is not None
    assert no_shift.to_report() == {
        "rule_id": "DQ-03",
        "severity": "info",
        "entity": "downtime_log",
        "date": None,
        "details": {"records_without_shift": 4},
    }

    def flow(upstream_produced: int, downstream_produced: int) -> DqIssue | None:
        return check_flow_balance(
            upstream="PAINT",
            downstream="ASSY",
            upstream_produced=upstream_produced,
            downstream_produced=downstream_produced,
            thresholds=t,
        )

    warn = flow(231, 240)
    info = flow(229, 231)
    assert warn is not None
    assert warn.severity == "warning"
    assert warn.entity == "PAINT->ASSY"
    assert info is not None
    assert info.severity == "info"
    assert flow(10, 9) is None
    gap = check_plan_vs_target(line_model_plan=4800, plant_target=5500)
    assert gap is not None
    assert gap.details["gap"] == -700
    assert check_plan_vs_target(line_model_plan=5500, plant_target=5500) is None
    e = check_effectiveness(line="WELD-1", period_date=D1, effectiveness=1.00004)
    assert e is None, "compared rounded to 4 digits"
    e = check_effectiveness(line="WELD-1", period_date=D1, effectiveness=1.02)
    assert e is not None
    assert e.details == {"effectiveness": 1.02}
    assert check_effectiveness(line="WELD-1", period_date=D1, effectiveness=None) is None


def test_dq07_unknown_value() -> None:
    issue = unknown_value(
        kind="equipment", value="Камера-2", suggestion="BOOTH-02", table="downtime", row=3
    )
    assert issue.rule_id == "DQ-07"
    assert issue.severity == "warning"
    assert issue.details == {
        "kind": "equipment",
        "value": "Камера-2",
        "suggestion": "BOOTH-02",
        "table": "downtime",
        "row": 3,
    }


# --------------------------------------------------------------------------- alerts


@pytest.fixture
def evaluator(cfg: TwinConfig) -> AlertEvaluator:
    return AlertEvaluator(cfg.rules)


def test_al_q1(evaluator: AlertEvaluator) -> None:
    warn = evaluator.defect_rate(area="PAINT", period_date=D1, shift="A", defect_rate=0.03478)
    assert warn is not None
    assert warn.severity == "warning"
    assert warn.value == 0.0348
    crit = evaluator.defect_rate(area="PAINT", period_date=D2, shift="A", defect_rate=0.0517)
    assert crit is not None
    assert crit.severity == "critical"
    assert evaluator.defect_rate(area="ASSY", period_date=D1, shift="A", defect_rate=0.02) is None
    assert evaluator.defect_rate(area="ASSY", period_date=D1, shift="A", defect_rate=None) is None
    assert crit.dedup_key == "AL-Q1|PAINT|2026-10-02/A"
    assert crit.to_report() == {
        "rule_id": "AL-Q1",
        "severity": "critical",
        "entity": "PAINT",
        "date": "2026-10-02",
        "value": 0.0517,
    }


def test_al_o1_o2(evaluator: AlertEvaluator) -> None:
    below = evaluator.oee(line="WELD-1", period_date=D1, shift="A", oee=0.84)
    near = evaluator.oee(line="WELD-1", period_date=D2, shift="A", oee=0.8738)
    assert below is not None
    assert (below.rule_id, below.severity) == ("AL-O1", "warning")
    assert near is not None
    assert (near.rule_id, near.severity) == ("AL-O2", "info")
    assert evaluator.oee(line="WELD-1", period_date=D1, shift="A", oee=0.9385) is None
    assert evaluator.oee(line="WELD-1", period_date=D1, shift="A", oee=None) is None


def test_al_d1(evaluator: AlertEvaluator) -> None:
    warn = evaluator.critical_downtime(equipment="CONV-03", period_date=D2, unplanned_min=55.0)
    crit = evaluator.critical_downtime(equipment="CONV-03", period_date=D2, unplanned_min=60.0)
    assert warn is not None
    assert warn.severity == "warning"
    assert warn.dedup_key.endswith("2026-10-02")
    assert crit is not None
    assert crit.severity == "critical"
    assert (
        evaluator.critical_downtime(equipment="BOOTH-02", period_date=D1, unplanned_min=40) is None
    )


def test_al_q3(evaluator: AlertEvaluator) -> None:
    prev = {"WELD": 0.0169, "PAINT": 0.0348, "ASSY": 0.0083}
    cur = {"WELD": 0.027, "PAINT": 0.0517, "ASSY": 0.0168}
    alert = evaluator.systemic_defects(period_date=D2, previous=prev, current=cur, shift="A")
    assert alert is not None
    assert alert.entity == "PLANT"
    assert alert.value == cur
    assert alert.to_report()["value"] == cur
    flat = {**cur, "ASSY": 0.0083}
    assert evaluator.systemic_defects(period_date=D2, previous=prev, current=flat) is None
    low = {"WELD": 0.018, "PAINT": 0.019, "ASSY": 0.0168}
    assert evaluator.systemic_defects(period_date=D2, previous=prev, current=low) is None
    assert evaluator.systemic_defects(period_date=D2, previous={}, current=cur) is None


def test_rules_disabled_when_missing_from_config(cfg: TwinConfig) -> None:
    rules = RulesConfig.model_validate(
        {
            **cfg.rules.model_dump(),
            "alert_rules": [
                r.model_dump() for r in cfg.rules.alert_rules if r.id not in {"AL-Q1", "AL-O2"}
            ],
        }
    )
    evaluator = AlertEvaluator(rules)
    assert not evaluator.enabled("AL-Q1")
    assert evaluator.defect_rate(area="PAINT", period_date=D1, shift="A", defect_rate=0.05) is None
    assert evaluator.oee(line="WELD-1", period_date=D1, shift="A", oee=0.87) is None
    assert evaluator.fixed_severity("AL-D1") is None, "AL-D1 severity is threshold-derived"


def test_deduplicate_keeps_latest_value() -> None:
    first = Alert("AL-Q1", "warning", "area", "PAINT", D1, 0.03, "A")
    again = Alert("AL-Q1", "critical", "area", "PAINT", D1, 0.05, "A")
    other = Alert("AL-Q1", "warning", "area", "WELD", D1, 0.03, "A")
    merged = deduplicate([first, other, again])
    assert merged == [again, other]
    assert Alert("AL-D1", "warning", "equipment", "CONV-03", D2, 55.0).period == "2026-10-02"


# --------------------------------------------------------------------------- bottleneck


def test_aggregate_bottleneck_golden() -> None:
    result = aggregate_bottleneck(
        {
            D1: {"WELD": 118, "PAINT": 115, "ASSY": 121},
            D2: {"WELD": 111, "PAINT": 116, "ASSY": 119},
        },
        ["WELD", "PAINT", "ASSY"],
    )
    assert result.to_report() == {
        "by_day": {"2026-10-01": "PAINT", "2026-10-02": "WELD"},
        "overall": "WELD",
        "shifting": True,
    }
    assert result.mean_output == {"WELD": 114.5, "PAINT": 115.5, "ASSY": 120.0}


def test_aggregate_bottleneck_ties_and_gaps() -> None:
    result = aggregate_bottleneck({D1: {"A": 5, "B": 5, "X": 1}, D2: {"B": 7}}, ["A", "B"])
    assert result.by_day == {D1: "A", D2: "B"}, "ties go to the earliest step; unknown ignored"
    assert result.overall == "A"
    assert result.shifting
    empty = aggregate_bottleneck({}, ["A"])
    assert empty.overall is None
    assert not empty.shifting
