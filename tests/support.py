"""Helpers shared by tests (importable thanks to ``pythonpath = ["tests"]``)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


def mutate(path: Path, old: str, new: str) -> None:
    """Replace exactly one occurrence of ``old`` in a file (fails loudly if absent/ambiguous)."""
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    assert count == 1, f"{old!r} occurs {count} times in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def line_of(path: Path, needle: str) -> int:
    """1-based line number of the first line containing ``needle``."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not found in {path}")


# --------------------------------------------------------------------------- golden comparison

CASE_DIR = REPO_ROOT / "data" / "case"
CASE_DOCX = CASE_DIR / "source" / "case2_data.docx"
CASE_XLSX = CASE_DIR / "csv" / "case2_data.xlsx"
CASE_CSVS = tuple(sorted((CASE_DIR / "csv").glob("*.csv")))
GOLDEN_JSON = CASE_DIR / "expected" / "import_expected.json"


def require_case_docx() -> None:
    """Skip when the organisers' original case files are absent: they are not published in the
    repository (put them into ``data/case/source/`` locally); the CSV/XLSX copies stay."""
    if not CASE_DOCX.is_file():
        pytest.skip("data/case/source/case2_data.docx is not in the repository")


GOLDEN_KEYS = (
    "constraints_parsed",
    "shift_reports",
    "downtime",
    "plan",
    "flow",
    "bottleneck_aggregate",
    "data_quality_issues",
    "alerts",
)
"""Keys of the import report compared with the golden file (FR-IMP-05)."""
GOLDEN_META_IGNORED = frozenset({"source"})
"""``meta.source`` names the uploaded file and differs per format."""


def golden() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))
    return data


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def diff_json(actual: Any, expected: Any, *, tol: float, path: str = "$") -> list[str]:
    """Differences between two JSON values: dict keys must match exactly, lists are compared
    regardless of order, numbers within ``tol`` (int and float alike), the rest by equality."""
    if _is_number(expected) and _is_number(actual):
        return (
            []
            if math.isclose(actual, expected, rel_tol=0, abs_tol=tol)
            else [f"{path}: {actual!r} != {expected!r}"]
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        problems = [f"{path}: missing key {k!r}" for k in expected if k not in actual]
        problems += [f"{path}: unexpected key {k!r}" for k in actual if k not in expected]
        for key in expected:
            if key in actual:
                problems += diff_json(actual[key], expected[key], tol=tol, path=f"{path}.{key}")
        return problems
    if isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            return [f"{path}: {len(actual)} items != {len(expected)} expected"]
        unmatched = list(range(len(actual)))
        problems = []
        for i, item in enumerate(expected):
            match = next(
                (j for j in unmatched if not diff_json(actual[j], item, tol=tol, path=path)), None
            )
            if match is None:
                problems.append(f"{path}[{i}]: no actual item equals {item!r}")
            else:
                unmatched.remove(match)
        return problems
    if type(actual) is not type(expected) or actual != expected:
        return [f"{path}: {actual!r} != {expected!r}"]
    return []


def golden_differences(report: dict[str, Any], expected: dict[str, Any] | None = None) -> list[str]:
    """Compare an import report with ``import_expected.json`` (T-GOLD, FR-IMP-05)."""
    gold = expected if expected is not None else golden()
    tol = float(gold["meta"]["float_tolerance"])
    problems: list[str] = []
    for key in GOLDEN_KEYS:
        if key not in report:
            problems.append(f"$.{key}: missing")
            continue
        problems += diff_json(report[key], gold[key], tol=tol, path=f"$.{key}")
    meta = report.get("meta", {})
    for key, value in gold["meta"].items():
        if key in GOLDEN_META_IGNORED:
            continue
        if key not in meta:
            problems.append(f"$.meta.{key}: missing")
        else:
            problems += diff_json(meta[key], value, tol=tol, path=f"$.meta.{key}")
    return problems
