"""NFR-11: business logic takes "now" only from twin_core.clock.Clock.

Static scan (AST, so comments/docstrings do not count and import aliases are resolved) of every
Python source under packages/, services/ and ml/ for wall-clock calls. Allowed only in
twin_core/clock.py and the explicit infrastructure allowlist below. Ruff's banned-api rule
(TID251, pyproject.toml) enforces the same at lint time.
"""

from __future__ import annotations

import ast

import pytest

from support import REPO_ROOT

SCANNED_DIRS = ("packages", "services", "ml")
ALLOWLIST = frozenset(
    {
        "packages/twin_core/src/twin_core/clock.py",  # the Clock itself
    }
)
BANNED = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "time.time",
        "time.time_ns",
    }
)


def _qualify(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _qualify(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def wall_clock_calls(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    """Return ``(line, qualified name)`` of every banned call in ``source``."""
    tree = ast.parse(source, filename=filename)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _qualify(node.func, aliases)
            if name in BANNED:
                found.append((node.lineno, name))
    return sorted(found)


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("import datetime\ndatetime.datetime.now()", ["datetime.datetime.now"]),
        ("from datetime import datetime\ndatetime.now(tz=None)", ["datetime.datetime.now"]),
        ("from datetime import datetime as dt\ndt.utcnow()", ["datetime.datetime.utcnow"]),
        ("from datetime import date\ndate.today()", ["datetime.date.today"]),
        ("import time\ntime.time()", ["time.time"]),
        ("import time as t\nt.time_ns()", ["time.time_ns"]),
        ("from time import time\ntime()", ["time.time"]),
        ("import time\ntime.monotonic()\ntime.perf_counter()", []),
        ("from datetime import datetime\n# datetime.now()\nx = 'datetime.now()'", []),
        ("def now():\n    return 1\nnow()", []),
    ],
)
def test_scanner_detects_wall_clock_calls(snippet: str, expected: list[str]) -> None:
    assert [name for _, name in wall_clock_calls(snippet)] == expected


def test_no_wall_clock_outside_clock_module() -> None:
    offenders: list[str] = []
    scanned = 0
    for top in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLIST or "/.venv/" in rel or "/node_modules/" in rel:
                continue
            scanned += 1
            for line, name in wall_clock_calls(path.read_text(encoding="utf-8"), rel):
                offenders.append(f"{rel}:{line}: {name}() — use twin_core.clock.Clock.now()")
    assert scanned > 10
    assert not offenders, "\n".join(offenders)


def test_allowlisted_files_exist() -> None:
    for rel in ALLOWLIST:
        assert (REPO_ROOT / rel).is_file(), rel


def test_clock_module_is_the_wall_clock_source() -> None:
    clock_src = (REPO_ROOT / "packages/twin_core/src/twin_core/clock.py").read_text("utf-8")
    assert [name for _, name in wall_clock_calls(clock_src)] == ["datetime.datetime.now"]
