"""Table recognition by headers and column mapping (SPEC §7.4).

A table is recognized by the keywords of its header row (case-insensitive): lines — «Линия» and
«Факт»; downtime — «Оборудование» and «Причина»; plan — «Модель» and «План»; quality —
«Выпущено» and «Брак». The header may be preceded by a few title rows. Columns are then found by
header keywords too, so their order does not matter. Tables of the same kind found in several
files/sheets are concatenated.

The keywords are the import file contract of SPEC §7.4 (the case's own Russian headers), not
plant configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from twin_core.importer.model import ImportFormatError, RawTable
from twin_core.importer.values import normalize_header

HEADER_SEARCH_ROWS: Final = 5
"""How many leading rows may hold a title above the header row."""


class TableKind(StrEnum):
    LINES = "lines"
    DOWNTIME = "downtime"
    PLAN = "plan"
    QUALITY = "quality"


SIGNATURES: Final[tuple[tuple[TableKind, tuple[str, ...]], ...]] = (
    (TableKind.LINES, ("линия", "факт")),
    (TableKind.DOWNTIME, ("оборудование", "причина")),
    (TableKind.PLAN, ("модель", "план")),
    (TableKind.QUALITY, ("выпущено", "брак")),
)
"""Checked in this order; the first match wins."""

Matcher = Callable[[str], bool]


def _has(*words: str) -> Matcher:
    return lambda header: all(word in header for word in words)


def _defects_count(header: str) -> bool:
    return "брак" in header and "%" not in header


def _defects_pct(header: str) -> bool:
    return "брак" in header and "%" in header


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    field: str
    matches: Matcher
    required: bool = True


COLUMNS: Final[dict[TableKind, tuple[ColumnSpec, ...]]] = {
    TableKind.LINES: (
        ColumnSpec("date", _has("дата")),
        ColumnSpec("line", _has("линия")),
        ColumnSpec("plan", lambda h: h.startswith("план"), required=False),
        ColumnSpec("fact", _has("факт")),
        ColumnSpec("worked", _has("время работы")),
        ColumnSpec("load", _has("загрузка"), required=False),
    ),
    TableKind.DOWNTIME: (
        ColumnSpec("date", _has("дата")),
        ColumnSpec("area", _has("участок"), required=False),
        ColumnSpec("equipment", _has("оборудование")),
        ColumnSpec("reason", _has("причина")),
        ColumnSpec("duration", _has("длительность")),
    ),
    TableKind.PLAN: (
        ColumnSpec("model", _has("модель")),
        ColumnSpec("qty", _has("план")),
    ),
    TableKind.QUALITY: (
        ColumnSpec("date", _has("дата")),
        ColumnSpec("area", _has("участок")),
        ColumnSpec("produced", _has("выпущено"), required=False),
        ColumnSpec("defects", _defects_count),
        ColumnSpec("defect_pct", _defects_pct, required=False),
    ),
}


@dataclass(frozen=True, slots=True)
class Row:
    """One data row: 1-based row number in its source and the cells by field name."""

    source: str
    number: int
    cells: dict[str, str]

    def get(self, field: str) -> str:
        return self.cells.get(field, "").strip()


@dataclass(slots=True)
class RecognizedTable:
    kind: TableKind
    sources: list[str]
    headers: dict[str, str]
    """field -> header text as found (from the first source)."""
    rows: list[Row]


def classify_header(cells: Sequence[str]) -> TableKind | None:
    header = " | ".join(normalize_header(c) for c in cells)
    for kind, words in SIGNATURES:
        if all(word in header for word in words):
            return kind
    return None


def minutes_factor(header: str, *, default_hours: bool) -> float:
    """Unit of a time column from its header: ``…, ч`` / ``час`` -> 60, ``мин`` -> 1."""
    h = normalize_header(header)
    if "мин" in h:
        return 1.0
    if "час" in h or h.endswith((", ч", " ч")) or "(ч)" in h:
        return 60.0
    return 60.0 if default_hours else 1.0


def _map_columns(kind: TableKind, header: Sequence[str], source: str) -> dict[str, int]:
    normalized = [normalize_header(h) for h in header]
    mapping: dict[str, int] = {}
    missing: list[str] = []
    for spec in COLUMNS[kind]:
        index = next(
            (i for i, h in enumerate(normalized) if i not in mapping.values() and spec.matches(h)),
            None,
        )
        if index is not None:
            mapping[spec.field] = index
        elif spec.required:
            missing.append(spec.field)
    if missing:
        raise ImportFormatError(
            f"{source}: table '{kind}' lacks columns: {', '.join(missing)}",
            problems=[f"{source}: missing column '{name}'" for name in missing],
        )
    return mapping


def recognize(tables: Sequence[RawTable]) -> tuple[dict[TableKind, RecognizedTable], list[str]]:
    """Recognize tables by headers.

    Returns the recognized tables by kind and the texts of unrecognized tables (one paragraph
    per non-empty cell) — e.g. an xlsx sheet «Вводные» with the constraints text.
    """
    found: dict[TableKind, RecognizedTable] = {}
    leftovers: list[str] = []
    for table in tables:
        kind: TableKind | None = None
        header_at = 0
        for i, row in enumerate(table.rows[:HEADER_SEARCH_ROWS]):
            kind = classify_header(row)
            if kind is not None:
                header_at = i
                break
        if kind is None:
            leftovers.extend(cell for row in table.rows for cell in row if cell.strip())
            continue
        header = table.rows[header_at]
        mapping = _map_columns(kind, header, table.source)
        rows = [
            Row(
                table.source,
                header_at + 2 + offset,
                {name: cells[i] if i < len(cells) else "" for name, i in mapping.items()},
            )
            for offset, cells in enumerate(table.rows[header_at + 1 :])
        ]
        rows = [r for r in rows if any(v.strip() for v in r.cells.values())]
        headers = {name: header[i] for name, i in mapping.items()}
        if kind in found:
            found[kind].sources.append(table.source)
            found[kind].rows.extend(rows)
        else:
            found[kind] = RecognizedTable(kind, [table.source], headers, rows)
    return found, leftovers


def require_tables(found: dict[TableKind, RecognizedTable], sources: Sequence[str]) -> None:
    missing = [kind for kind in TableKind if kind not in found]
    if missing:
        raise ImportFormatError(
            f"tables not found: {', '.join(missing)} (recognized by headers, SPEC §7.4)",
            problems=[f"missing table '{kind}'" for kind in missing]
            + [f"read: {source}" for source in sources],
        )
