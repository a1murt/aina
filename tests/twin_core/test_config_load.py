"""The repository configuration loads, validates and exposes typed indexes (FR-DOM-01/02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from twin_core.config import (
    CONFIG_FILES,
    FALLBACK_REASON_CODE,
    TwinConfig,
    load_config,
    load_config_from_settings,
    load_tag_map,
)
from twin_core.config.simulation import FailureInject, SetStateInject
from twin_core.domain import EquipmentState
from twin_core.settings import TwinSettings, default_config_dir


def test_every_config_file_exists(config_dir: Path) -> None:
    for name in CONFIG_FILES.values():
        assert (config_dir / name).is_file(), name


def test_repository_config_loads(cfg: TwinConfig) -> None:
    assert cfg.plant.site.code == "KST"
    assert cfg.flow_lines == ("WELD-1", "PAINT-1", "ASSY-1", "QC-1")
    assert set(cfg.areas) == {"CKD", "WELD", "PAINT", "ASSY", "QC", "FG"}
    assert len(cfg.equipment) == 14
    assert set(cfg.buffers) == {"BIW", "PBS", "EOL"}
    assert set(cfg.products) == {"ONIX", "COBALT", "J7"}
    assert FALLBACK_REASON_CODE in cfg.reasons
    assert cfg.tag_map is None


def test_indexes_and_relations(cfg: TwinConfig) -> None:
    assert cfg.line_of_equipment("CONV-03").code == "ASSY-1"
    assert cfg.area_of_equipment("BOOTH-02").code == "PAINT"
    assert cfg.area_of_line("QC-1").code == "QC"
    assert [e.code for e in cfg.equipment_of_line("PAINT-1")] == ["BOOTH-01", "BOOTH-02", "OVEN-01"]
    assert cfg.equipment["ABB-01"].criticality == "B"
    assert cfg.equipment["ABB-01"].degraded_capacity == pytest.approx(0.5)
    assert cfg.reasons["PM-SCHEDULED"].planned is True
    assert cfg.reasons["ME-CHAIN"].planned is False
    assert cfg.defects["P-THIN"].repaint is True
    assert cfg.alert_rules["AL-S1"].severity == {"A": "critical", "B": "warning", "C": "info"}
    assert cfg.lines["PAINT-1"].rework.repaint_share == pytest.approx(0.4)
    assert cfg.timezone.key == "Asia/Qostanay"


def test_values_mirrored_by_golden_script(cfg: TwinConfig) -> None:
    """compute_expected.py hard-codes these defaults; keep them in sync (SPEC §0)."""
    thresholds = cfg.rules.thresholds
    assert {cfg.lines[c].ict_seconds for c in ("WELD-1", "PAINT-1", "ASSY-1")} == {233}
    assert thresholds.oee_target == pytest.approx(0.85)
    assert thresholds.defect_rate_limit == pytest.approx(0.02)
    assert thresholds.critical_downtime_limit_min_per_day == pytest.approx(60)
    assert {s.duration_min for s in cfg.plant.calendar.shifts} == {480}


def test_simulation_sections(cfg: TwinConfig) -> None:
    sim = cfg.simulation
    assert set(sim.degradation.per_type) == {"robot", "fixture", "conveyor"}
    assert set(sim.defects.per_area) == {"WELD", "PAINT", "ASSY", "QC"}
    assert set(sim.telemetry.per_type["booth"]) == {
        "filter_dp_pa",
        "humidity_pct",
        "air_temp_c",
        "airflow_mps",
    }
    s1 = cfg.scenarios["S1-CHAIN-BREAK"].inject
    assert isinstance(s1, FailureInject)
    assert (s1.equipment, s1.reason) == ("CONV-03", "ME-CHAIN")
    s2 = cfg.scenarios["S2-FILTER-TREND"].inject
    assert isinstance(s2, SetStateInject)
    assert s2.values == {"filter_dp_pa": 370.0}
    assert sim.ml_dataset.from_.isoformat() == "2025-10-01T00:00:00+05:00"
    assert sim.clock.demo_start.isoformat() == "2026-10-16T07:00:00+05:00"


def test_example_tag_map_validates_against_plant(cfg: TwinConfig, config_dir: Path) -> None:
    tag_map = load_tag_map(config_dir / "tag_map.example.yaml", cfg.plant)
    assert tag_map.opcua.state_enum[4] is EquipmentState.DOWN_UNPLANNED
    assert {n.target for n in tag_map.opcua.nodes} >= {("equipment", "CONV-03"), ("buffer", "PBS")}


def test_tag_map_auto_detection(config_copy: Path) -> None:
    assert load_config(config_copy).tag_map is None
    (config_copy / "tag_map.demo.yaml").write_text(
        (config_copy / "tag_map.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    loaded = load_config(config_copy)
    assert loaded.tag_map is not None
    assert loaded.tag_map_path == config_copy / "tag_map.demo.yaml"


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:
    monkeypatch.setenv("PLANT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PLANT_TAG_MAP", "tag_map.example.yaml")
    monkeypatch.setenv("CLOCK_MODE", "sim")
    settings = TwinSettings()
    assert settings.clock_mode == "sim"
    loaded = load_config_from_settings(settings)
    assert loaded.tag_map is not None


def test_default_config_dir_is_repository_config(config_dir: Path) -> None:
    assert default_config_dir().resolve() == config_dir.resolve()
