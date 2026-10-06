"""Broken configs fail at load with readable errors: file, line, path, bad value, suggestion.

AC M0: "невалидный YAML падает с понятной ошибкой"; FR-DOM-01.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support import line_of, mutate
from twin_core.config import ConfigError, ConfigIssue, load_config
from twin_core.config.__main__ import main as config_main
from twin_core.health import load_config_or_exit


def load_issues(config_dir: Path, **kwargs: object) -> list[ConfigIssue]:
    with pytest.raises(ConfigError) as caught:
        load_config(config_dir, **kwargs)  # type: ignore[arg-type]
    return list(caught.value.issues)


def only(issues: list[ConfigIssue]) -> ConfigIssue:
    assert len(issues) == 1, "\n".join(i.render() for i in issues)
    return issues[0]


# --------------------------------------------------------------------------- cross-references


def test_unknown_equipment_in_scenario_is_reported_with_suggestion(config_copy: Path) -> None:
    sim = config_copy / "simulation.yaml"
    mutate(sim, "type: failure, equipment: CONV-03", "type: failure, equipment: CONV-3")
    issue = only(load_issues(config_copy))
    assert issue.file == "simulation.yaml"
    assert issue.path == "scenarios[S1-CHAIN-BREAK].inject.equipment"
    assert issue.value == "CONV-3"
    assert issue.suggestion == "CONV-03"
    assert issue.line == line_of(sim, "equipment: CONV-3")
    assert issue.render() == (
        f"simulation.yaml:{issue.line} › scenarios[S1-CHAIN-BREAK].inject.equipment: "
        "unknown equipment 'CONV-3' (14 known); did you mean 'CONV-03'?"
    )


def test_unknown_role_in_alert_rule(config_copy: Path) -> None:
    mutate(
        config_copy / "rules.yaml",
        "recipients: [master, maintenance]\n    channels: [ui, telegram]\n  - id: AL-D1",
        "recipients: [mastr, maintenance]\n    channels: [ui, telegram]\n  - id: AL-D1",
    )
    issue = only(load_issues(config_copy))
    assert (issue.file, issue.path, issue.suggestion) == (
        "rules.yaml",
        "alert_rules[AL-S1].recipients[0]",
        "master",
    )
    assert "unknown role 'mastr'" in issue.message
    assert "known: admin, director, maintenance, master, operator, quality" in issue.message


def test_unknown_reason_in_simulation_failures(config_copy: Path) -> None:
    mutate(
        config_copy / "simulation.yaml",
        "reasons: { EL-DRIVE: 0.6, EL-PLC: 0.4 }",
        "reasons: { EL-DRIVES: 0.6, EL-PLC: 0.4 }",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "failures.booth.reasons.EL-DRIVES"
    assert issue.suggestion == "EL-DRIVE"


def test_broken_buffer_line_reference(config_copy: Path) -> None:
    plant = config_copy / "plant.yaml"
    mutate(plant, "from_line: PAINT-1, to_line: ASSY-1", "from_line: PAINT-1, to_line: ASY-1")
    issue = only(load_issues(config_copy))
    assert (issue.file, issue.path, issue.value, issue.suggestion) == (
        "plant.yaml",
        "buffers[PBS].to_line",
        "ASY-1",
        "ASSY-1",
    )
    assert issue.line == line_of(plant, "code: PBS")


def test_buffer_must_go_downstream(config_copy: Path) -> None:
    mutate(
        config_copy / "plant.yaml",
        "from_line: WELD-1,  to_line: PAINT-1",
        "from_line: PAINT-1, to_line: WELD-1",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "buffers[BIW].to_line"
    assert "downstream" in issue.message


def test_unknown_equipment_type(config_copy: Path) -> None:
    mutate(config_copy / "plant.yaml", "type: oven,  criticality: A", "type: owen,  criticality: A")
    issue = only(load_issues(config_copy))
    assert issue.path == "areas[PAINT].lines[PAINT-1].equipment[OVEN-01].type"
    assert issue.suggestion == "oven"


def test_planned_reason_cannot_be_a_failure(config_copy: Path) -> None:
    mutate(
        config_copy / "simulation.yaml",
        "reasons: { EL-DRIVE: 0.7, EL-PLC: 0.3 }",
        "reasons: { EL-DRIVE: 0.7, PM-CLEANING: 0.3 }",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "failures.conveyor.reasons.PM-CLEANING"
    assert "planned" in issue.message


def test_planned_maintenance_needs_planned_reason(config_copy: Path) -> None:
    mutate(
        config_copy / "simulation.yaml",
        "stagger: true, reason: PM-CLEANING",
        "stagger: true, reason: ME-JAM",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "planned_maintenance[1].reason"


def test_defect_type_must_belong_to_area(config_copy: Path) -> None:
    mutate(config_copy / "simulation.yaml", "W-SPATTER: 0.25", "A-TORQUE: 0.25")
    issue = only(load_issues(config_copy))
    assert issue.path == "defects.WELD.types.A-TORQUE"
    assert "belongs to area 'ASSY'" in issue.message


def test_unknown_defect_area(config_copy: Path) -> None:
    mutate(
        config_copy / "defect_codes.yaml",
        "code: Q-LEAK,     area: QC,",
        "code: Q-LEAK,     area: QA,",
    )
    issue = only(load_issues(config_copy))
    assert (issue.file, issue.path, issue.suggestion) == (
        "defect_codes.yaml",
        "defects[Q-LEAK].area",
        "QC",
    )


def test_telemetry_signal_must_exist_for_type(config_copy: Path) -> None:
    mutate(config_copy / "simulation.yaml", 'vibration_mm_s: "2.0', 'vibration_mm: "2.0')
    issue = only(load_issues(config_copy))
    assert issue.path == "telemetry.conveyor.vibration_mm"
    assert issue.suggestion == "vibration_mm_s"


def test_set_state_value_must_be_signal_or_degradation(config_copy: Path) -> None:
    mutate(
        config_copy / "simulation.yaml",
        "equipment: BOOTH-02, filter_dp_pa: 370",
        "equipment: BOOTH-02, filter_dp: 370",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "scenarios[S2-FILTER-TREND].inject.filter_dp"
    assert issue.suggestion == "filter_dp_pa"


def test_initial_buffer_level_within_capacity(config_copy: Path) -> None:
    mutate(
        config_copy / "simulation.yaml",
        "initial_buffers: { BIW: 12,",
        "initial_buffers: { BIW: 25,",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "process.initial_buffers.BIW"
    assert "capacity 20" in issue.message


def test_microstops_must_be_below_threshold(config_copy: Path) -> None:
    mutate(config_copy / "rules.yaml", "microstop_threshold_s: 300", "microstop_threshold_s: 60")
    issues = load_issues(config_copy)
    assert {i.path for i in issues} == {
        "microstops.conveyor.duration.median",
        "microstops.robot.duration.median",
    }


def test_duplicate_asset_code(config_copy: Path) -> None:
    mutate(
        config_copy / "plant.yaml",
        '{ code: TEST-02,  name_ru: "Тормозной стенд"',
        '{ code: TEST-01,  name_ru: "Тормозной стенд"',
    )
    issues = load_issues(config_copy)
    assert any(
        i.path == "areas[QC].lines[QC-1].equipment[TEST-01].code"
        and "duplicate asset code 'TEST-01'" in i.message
        for i in issues
    )


def test_ambiguous_alias(config_copy: Path) -> None:
    mutate(config_copy / "plant.yaml", 'aliases: ["Камера-01"]', 'aliases: ["камера - 02"]')
    issues = load_issues(config_copy)
    assert any("ambiguous" in i.message and "BOOTH-01" in i.message for i in issues)


def test_extra_working_day_on_holiday(config_copy: Path) -> None:
    mutate(
        config_copy / "plant.yaml",
        "extra_working_days: []",
        "extra_working_days: [{date: 2026-10-26, shifts: [A]}]",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "calendar.extra_working_days[0].date"
    assert "both a holiday and an extra working day" in issue.message


def test_extra_working_day_unknown_shift(config_copy: Path) -> None:
    mutate(
        config_copy / "plant.yaml",
        "extra_working_days: []",
        "extra_working_days: [{date: 2026-10-17, shifts: [C]}]",
    )
    issue = only(load_issues(config_copy))
    assert issue.path == "calendar.extra_working_days[0].shifts[0]"


def test_business_area_and_currency(config_copy: Path) -> None:
    business = config_copy / "business.yaml"
    mutate(business, "ASSY: 25000", "ASY: 25000")
    mutate(business, "currency: KZT", "currency: USD")
    issues = load_issues(config_copy)
    assert {i.path for i in issues} == {"params.rework_cost_kzt.value.ASY", "currency"}


def test_several_problems_are_reported_together(config_copy: Path) -> None:
    mutate(config_copy / "plant.yaml", "to_line: ASSY-1", "to_line: ASY-1")
    mutate(config_copy / "simulation.yaml", "equipment: CONV-03", "equipment: CONV-3")
    mutate(config_copy / "rules.yaml", "chain: [master, director]", "chain: [master, directr]")
    issues = load_issues(config_copy)
    assert {i.file for i in issues} == {"plant.yaml", "simulation.yaml", "rules.yaml"}
    text = ConfigError(config_copy, issues).render()
    assert text.startswith(f"Invalid plant configuration in {config_copy} (3 problems):")


# --------------------------------------------------------------------------- tag map


def test_tag_map_unknown_equipment_and_signal(config_copy: Path) -> None:
    tag_map = config_copy / "tag_map.example.yaml"
    mutate(tag_map, "equipment: CONV-03, signal: state }", "equipment: CONV-3, signal: state }")
    mutate(tag_map, "signal: vibration_mm_s }", "signal: vibration }")
    issues = load_issues(config_copy, tag_map="tag_map.example.yaml")
    assert {(i.path, i.suggestion) for i in issues} == {
        ("opcua.nodes[0].equipment", "CONV-03"),
        ("opcua.nodes[3].signal", "vibration_mm_s"),
    }


def test_tag_map_must_not_map_degradation_oracle(config_copy: Path) -> None:
    mutate(config_copy / "tag_map.example.yaml", "signal: alarm_code }", "signal: degradation }")
    issue = only(load_issues(config_copy, tag_map="tag_map.example.yaml"))
    assert "FR-SIM-02" in issue.message


def test_explicit_tag_map_must_exist(config_copy: Path) -> None:
    issue = only(load_issues(config_copy, tag_map="tag_map.yaml"))
    assert issue.file == "tag_map.yaml"
    assert "not found" in issue.message


# --------------------------------------------------------------------------- schema / syntax


def test_bad_yaml_syntax_reports_line(config_copy: Path) -> None:
    rules = config_copy / "rules.yaml"
    mutate(rules, "  oee_target: 0.85", "  oee_target: [0.85")
    issue = only(load_issues(config_copy))
    assert issue.file == "rules.yaml"
    assert issue.line is not None
    assert line_of(rules, "oee_target") <= issue.line <= line_of(rules, "oee_target") + 2
    assert issue.message.startswith("invalid YAML syntax")


def test_duplicate_yaml_key_is_not_silently_dropped(config_copy: Path) -> None:
    rules = config_copy / "rules.yaml"
    mutate(rules, "  oee_target: 0.85", "  oee_target: 0.85\n  oee_target: 0.80")
    issue = only(load_issues(config_copy))
    assert issue.path == "thresholds.oee_target"
    assert "duplicate key 'oee_target'" in issue.message
    assert issue.line == line_of(rules, "oee_target: 0.80")


def test_unknown_key_suggests_field_name(config_copy: Path) -> None:
    mutate(config_copy / "plant.yaml", "        ict_seconds: 200", "        ict_second: 200")
    issues = load_issues(config_copy)
    by_message = {i.message: i for i in issues}
    unknown = by_message["unknown key 'ict_second'"]
    assert unknown.path == "areas[QC].lines[QC-1].ict_second"
    assert unknown.suggestion == "ict_seconds"
    assert "required key 'ict_seconds' is missing" in by_message


def test_out_of_range_and_enum_values(config_copy: Path) -> None:
    mutate(
        config_copy / "plant.yaml",
        'type: robot,   criticality: B, degraded_capacity: 0.5, aliases: ["ABB-02"]',
        'type: robot,   criticality: D, degraded_capacity: 1.5, aliases: ["ABB-02"]',
    )
    issues = load_issues(config_copy)
    paths = {i.path: i for i in issues}
    base = "areas[WELD].lines[WELD-1].equipment[ABB-02]"
    assert "(got 'D')" in paths[f"{base}.criticality"].message
    assert "(got 1.5)" in paths[f"{base}.degraded_capacity"].message


def test_unquoted_shift_time_is_explained(config_copy: Path) -> None:
    mutate(config_copy / "plant.yaml", 'start: "07:00"', "start: 7:00")  # YAML 1.1: int 420
    issue = only(load_issues(config_copy))
    assert issue.path == "calendar.shifts[A].start"
    assert 'quoted time "HH:MM"' in issue.message


def test_shares_must_sum_to_one(config_copy: Path) -> None:
    mutate(config_copy / "simulation.yaml", "ONIX: 0.521", "ONIX: 0.621")
    issue = only(load_issues(config_copy))
    assert issue.path == "process"
    assert "sum to 1.0" in issue.message


def test_unknown_scenario_type(config_copy: Path) -> None:
    mutate(config_copy / "simulation.yaml", "inject: { type: ckd,", "inject: { type: kit,")
    issue = only(load_issues(config_copy))
    assert issue.path == "scenarios[S5-KIT-SHORTAGE].inject"
    assert "'kit'" in issue.message


def test_missing_config_file(config_copy: Path) -> None:
    (config_copy / "business.yaml").unlink()
    issue = only(load_issues(config_copy))
    assert issue.file == "business.yaml"
    assert "not found" in issue.message


def test_missing_config_directory(tmp_path: Path) -> None:
    issue = only(load_issues(tmp_path / "nope"))
    assert "config directory not found" in issue.message


# --------------------------------------------------------------------------- services fail fast


def test_service_refuses_to_start_with_broken_config(
    config_copy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mutate(config_copy / "simulation.yaml", "equipment: CONV-03", "equipment: CONV-3")
    monkeypatch.setenv("PLANT_CONFIG_DIR", str(config_copy))
    with pytest.raises(SystemExit) as caught:
        load_config_or_exit("engine")
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "[engine] refusing to start" in err
    assert "did you mean 'CONV-03'?" in err


def test_cli_reports_problems(
    config_dir: Path, config_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert config_main([str(config_dir), "--tag-map", "tag_map.example.yaml"]) == 0
    assert "OK: TwinConfig(" in capsys.readouterr().out
    mutate(config_copy / "plant.yaml", "to_line: ASSY-1", "to_line: ASY-1")
    assert config_main([str(config_copy)]) == 1
    assert "buffers[PBS].to_line: unknown line 'ASY-1'" in capsys.readouterr().err
