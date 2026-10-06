"""T-GOLD: importing the case in every format reproduces ``import_expected.json`` (FR-IMP-05).

The golden file is produced by the reference script ``data/case/expected/compute_expected.py``;
this test runs the production pipeline (``twin_core.importer``: readers -> table recognition ->
KPI ISO 22400 -> DQ -> alert rules) without a database. Compared: ``constraints_parsed``,
``shift_reports``, ``downtime``, ``plan``, ``flow``, ``bottleneck_aggregate``,
``data_quality_issues``, ``alerts`` and ``meta`` (except ``source``); lists in any order, floats
within ``meta.float_tolerance``. The API returns the same report (tests/integration).
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from support import (
    CASE_CSVS,
    CASE_DOCX,
    CASE_XLSX,
    diff_json,
    golden,
    golden_differences,
)
from twin_core.config import TwinConfig
from twin_core.importer import UploadedFile, run_import


def _files(*paths: Path) -> list[UploadedFile]:
    return [UploadedFile(p.name, p.read_bytes()) for p in paths]


def _zip(paths: tuple[Path, ...]) -> list[UploadedFile]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in paths:
            archive.write(path, f"case/{path.name}")
    return [UploadedFile("case.zip", buffer.getvalue())]


VARIANTS = {
    "docx": lambda: _files(CASE_DOCX),
    "xlsx": lambda: _files(CASE_XLSX),
    "csv": lambda: _files(*CASE_CSVS),
    "csv-reversed": lambda: _files(*reversed(CASE_CSVS)),
    "csv-zip": lambda: _zip(CASE_CSVS),
}


def test_case_fixtures_exist() -> None:
    assert CASE_DOCX.is_file()
    assert CASE_XLSX.is_file()
    assert [p.name for p in CASE_CSVS] == [
        "01_lines.csv",
        "02_downtime.csv",
        "03_plan.csv",
        "04_quality.csv",
    ]


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_import_matches_golden(variant: str, cfg: TwinConfig) -> None:
    report = run_import(VARIANTS[variant](), cfg).to_json()
    # The report must survive JSON (it is stored in import_job.result and served by the API).
    report = json.loads(json.dumps(report, ensure_ascii=False))
    assert golden_differences(report) == []


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_golden_counts(variant: str, cfg: TwinConfig) -> None:
    report = run_import(VARIANTS[variant](), cfg)
    rules = [i.rule_id for i in report.dq_issues]
    assert {r: rules.count(r) for r in sorted(set(rules))} == {
        "DQ-01": 1,
        "DQ-02": 5,
        "DQ-03": 1,
        "DQ-04": 2,
        "DQ-05": 1,
    }
    assert len(report.alerts) == 6
    assert report.warnings == ()


def test_docx_constraints_come_from_text(cfg: TwinConfig) -> None:
    report = run_import(VARIANTS["docx"](), cfg)
    assert {c.source for c in report.constraint_checks} == {"text"}
    assert all(c.matches for c in report.constraint_checks)


@pytest.mark.parametrize("variant", ["xlsx", "csv"])
def test_tabular_constraints_fall_back_to_config(variant: str, cfg: TwinConfig) -> None:
    # The case csv/xlsx carry no «Дополнительные вводные» text: constraints come from the config.
    report = run_import(VARIANTS[variant](), cfg)
    assert {c.source for c in report.constraint_checks} == {"config"}


def test_meta_source_names_the_upload(cfg: TwinConfig) -> None:
    report = run_import(VARIANTS["docx"](), cfg).to_json()
    assert report["meta"]["source"] == CASE_DOCX.name
    assert report["meta"]["kind"] == "docx"
    assert report["meta"]["period"] == {"from": "2026-10-01", "to": "2026-10-02"}


# --------------------------------------------------------------------------- the comparator


def test_comparator_detects_drift(cfg: TwinConfig) -> None:
    report: dict[str, Any] = run_import(VARIANTS["docx"](), cfg).to_json()
    report["shift_reports"][0]["oee"] += 2e-4
    report["alerts"].pop()
    report["plan"]["extra"] = 1
    problems = golden_differences(report)
    assert any("shift_reports" in p for p in problems)
    assert any("alerts" in p for p in problems)
    assert any("unexpected key 'extra'" in p for p in problems)


def test_comparator_rules() -> None:
    assert diff_json([1, 2.00001], [2, 1], tol=1e-4) == []
    assert diff_json({"a": 1}, {"a": 1.0}, tol=1e-4) == []
    assert diff_json(True, 1, tol=1e-4) != []
    assert diff_json([1], [1, 2], tol=1e-4) == ["$: 1 items != 2 expected"]
    assert diff_json("x", "y", tol=1e-4) == ["$: 'x' != 'y'"]
    assert diff_json({}, {"a": 1}, tol=1e-4) == ["$: missing key 'a'"]
    assert golden_differences({}, golden())[0] == "$.constraints_parsed: missing"
    assert "$.meta.ict_seconds: missing" in golden_differences({"meta": {}}, golden())
