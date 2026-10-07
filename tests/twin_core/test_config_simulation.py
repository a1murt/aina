"""simulation.yaml keys added in M2 (bindings of the virtual plant) are validated at load."""

from __future__ import annotations

from pathlib import Path

import pytest

from support import mutate
from twin_core.config import ConfigError, ConfigIssue, TwinConfig, load_config

SIM = "simulation.yaml"


def issues_after(config_dir: Path, *changes: tuple[str, str]) -> list[ConfigIssue]:
    for old, new in changes:
        mutate(config_dir / SIM, old, new)
    with pytest.raises(ConfigError) as caught:
        load_config(config_dir, tag_map=False)
    return list(caught.value.issues)


def only(issues: list[ConfigIssue]) -> ConfigIssue:
    assert len(issues) == 1, "\n".join(i.render() for i in issues)
    return issues[0]


def test_repository_values(cfg: TwinConfig) -> None:
    sim = cfg.simulation
    assert sim.process.ckd_shortage_policy == "resequence"
    assert sim.paint_filters is not None
    assert (sim.paint_filters.equipment_type, sim.paint_filters.signal) == ("booth", "filter_dp_pa")
    assert sim.defects.per_area["PAINT"].humidity_signal == "humidity_pct"
    assert sim.defects.per_area["WELD"].wear_equipment_type == "robot"
    assert sim.degradation.per_type["robot"].pm_floor == pytest.approx(0.05)
    assert sim.failures["oven"].wear_reasons == ["EL-DRIVE"]


def test_unknown_filter_signal(config_copy: Path) -> None:
    issue = only(
        issues_after(config_copy, ("signal: filter_dp_pa      #", "signal: filter_dp      #"))
    )
    assert (issue.path, issue.suggestion) == ("paint_filters.signal", "filter_dp_pa")


def test_unknown_filter_equipment_type(config_copy: Path) -> None:
    issue = only(
        issues_after(config_copy, ("equipment_type: booth     #", "equipment_type: boot     #"))
    )
    assert (issue.path, issue.suggestion) == ("paint_filters.equipment_type", "booth")


def test_filter_replacement_reason_must_be_unplanned(config_copy: Path) -> None:
    issue = only(issues_after(config_copy, ("reason: MT-FILTER }", "reason: PM-CLEANING }")))
    assert issue.path == "paint_filters.replacement.reason"
    assert "planned" in issue.message


def test_humidity_signal_must_exist_in_area(config_copy: Path) -> None:
    issue = only(
        issues_after(config_copy, ("humidity_signal: humidity_pct", "humidity_signal: air_temp"))
    )
    assert issue.path == "defects.PAINT.humidity_signal"
    assert issue.suggestion == "air_temp_c"


def test_humidity_signal_needs_a_warn_band(config_copy: Path) -> None:
    issue = only(
        issues_after(config_copy, ("humidity_signal: humidity_pct", "humidity_signal: airflow_mps"))
    )
    assert "warn_lo and warn_hi" in issue.message


def test_humidity_pair_is_required(config_copy: Path) -> None:
    issue = only(issues_after(config_copy, (" humidity_signal: humidity_pct,", "")))
    assert issue.path == "defects.PAINT"
    assert "must be given together" in issue.message


def test_wear_type_must_be_in_area(config_copy: Path) -> None:
    issue = only(
        issues_after(config_copy, ("wear_equipment_type: robot", "wear_equipment_type: booth"))
    )
    assert issue.path == "defects.WELD.wear_equipment_type"


def test_wear_type_needs_degradation(config_copy: Path) -> None:
    issue = only(
        issues_after(
            config_copy,
            ("wear_equipment_type: robot", "wear_equipment_type: fixture"),
            (
                "  fixture:  { mean_rate_per_h: 0.0012, shape: 2.0, reset_after_repair: 0.10, "
                "pm_reduction: 0.30, pm_floor: 0.05 }\n",
                "",
            ),
        )
    )
    assert "no degradation parameters" in issue.message


def test_ckd_shortage_policy_values(config_copy: Path) -> None:
    issue = only(
        issues_after(config_copy, ("ckd_shortage_policy: resequence", "ckd_shortage_policy: skip"))
    )
    assert issue.path == "process.ckd_shortage_policy"


def test_pm_floor_is_a_fraction(config_copy: Path) -> None:
    issue = only(
        issues_after(
            config_copy,
            (
                "pm_reduction: 0.30, pm_floor: 0.05 }\n  fixture",
                "pm_reduction: 0.30, pm_floor: 1.5 }\n  fixture",
            ),
        )
    )
    assert issue.path == "degradation.robot.pm_floor"
