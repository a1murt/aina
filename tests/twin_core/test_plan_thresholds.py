"""Plan progress (SPEC §5.8, golden 114.29 / 130.95) and threshold overrides (settings, §8)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from support import golden
from twin_core.config import TwinConfig
from twin_core.kpi import round_fraction
from twin_core.plan import (
    line_plan_by_product,
    month_shift_count,
    plan_progress,
    plan_progress_json,
)
from twin_core.thresholds import (
    OverrideError,
    apply_rule_overrides,
    effective_config,
    overrides_from_rows,
)

MONTH_START = datetime(2026, 9, 30, 19, 0, tzinfo=UTC)  # 01.10 00:00 Asia/Qostanay


def test_required_rate_at_month_start_matches_golden(cfg: TwinConfig) -> None:
    progress = plan_progress(
        cfg, "2026-10", as_of=MONTH_START, mtd_output=0, plant_target=5500, line_plan=4800
    )
    plan = golden()["plan"]
    assert progress.shifts.total == 42
    assert progress.shifts.remaining == 42
    line, target = progress.targets["line_plan"], progress.targets["plant_target"]
    assert round_fraction(line.required_rate) == round(plan["required_rate_per_shift_line_plan"], 4)
    assert round_fraction(target.required_rate) == round(plan["required_rate_per_shift_target"], 4)
    assert round_fraction(line.required_rate) == 114.2857
    assert round_fraction(target.required_rate) == 130.9524
    assert line.fulfilment is None  # nothing planned yet
    assert progress.unallocated == -700


def test_progress_mid_shift(cfg: TwinConfig) -> None:
    # 16.10 09:30 local: 11 working days done (22 shifts) + 2.5 h of shift A
    as_of = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)
    count = month_shift_count(cfg.calendar, cfg, "2026-10", as_of)
    assert count.total == 42
    assert count.elapsed == pytest.approx(22 + 150 / 480)
    progress = plan_progress(
        cfg,
        "2026-10",
        as_of=as_of,
        mtd_output=2500,
        plant_target=5500,
        line_plan=4800,
        daily_output={date(2026, 10, 1): 230, date(2026, 10, 2): 228},
    )
    line = progress.targets["line_plan"]
    assert line.plan_to_date == pytest.approx(4800 * count.elapsed / 42)
    assert line.plan_to_date is not None
    assert line.fulfilment == pytest.approx(2500 / line.plan_to_date)
    assert line.required_rate == pytest.approx((4800 - 2500) / count.remaining)
    body: dict[str, Any] = plan_progress_json(progress)
    assert body["daily"][0] == {
        "date": "2026-10-01",
        "output": 230,
        "cum_output": 230,
        "cum_plan": {"plant_target": round(5500 * 2 / 42, 2), "line_plan": round(4800 * 2 / 42, 2)},
    }
    assert len(body["daily"]) == 31
    assert body["daily"][-1]["cum_plan"]["line_plan"] == 4800.0


def test_line_plan_by_product(cfg: TwinConfig) -> None:
    assert line_plan_by_product(cfg, "2026-10") == {"ONIX": 2500, "COBALT": 1800, "J7": 500}
    rows = [("line_model", "ASSY-1", "ONIX", 100), ("plant_target", None, None, 5)]
    assert line_plan_by_product(cfg, "2026-10", rows) == {"ONIX": 100}


def test_overrides_apply_on_top_of_yaml(cfg: TwinConfig) -> None:
    rules = apply_rule_overrides(cfg.rules, {"thresholds": {"oee_target": 0.8, "pdm_warn_p": None}})
    assert rules.thresholds.oee_target == 0.8
    assert rules.thresholds.pdm_warn_p == cfg.rules.thresholds.pdm_warn_p
    assert cfg.rules.thresholds.oee_target == 0.85  # the YAML model is untouched
    eff = effective_config(cfg, {"data_quality": {"downtime_recon_min": 15}})
    assert eff.rules.data_quality.downtime_recon_min == 15
    assert eff.lines.keys() == cfg.lines.keys()
    assert effective_config(cfg, {}) is cfg


@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"thresholds": {"oee_targt": 0.8}}, ["thresholds", "oee_targt"]),
        ({"thresholds": {"defect_rate_limit": 0.5}}, ["thresholds"]),
        ({"thresholds": {"oee_target": "high"}}, ["thresholds", "oee_target"]),
        ({"engine": {"kpi_tick_s": 1}}, ["engine"]),
    ],
)
def test_bad_overrides_are_rejected(
    cfg: TwinConfig, overrides: dict[str, dict[str, object]], loc: list[str]
) -> None:
    with pytest.raises(OverrideError) as info:
        apply_rule_overrides(cfg.rules, overrides)
    assert any(e["loc"][: len(loc)] == loc for e in info.value.errors)


def test_overrides_from_settings_rows() -> None:
    rows = {"thresholds": '{"oee_target": 0.8}', "data_quality": {}, "other": {"x": 1}}
    assert overrides_from_rows(rows) == {"thresholds": {"oee_target": 0.8}}
