"""Helpers shared by tests (importable thanks to ``pythonpath = ["tests"]``)."""

from __future__ import annotations

from pathlib import Path

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
