"""Recognized table rows -> typed records (FR-IMP-01/02).

Names are mapped to codes through ``aliases`` (FR-DOM-02). A value that maps to nothing, or
does not parse as a number/date, becomes a DQ-07 issue with the closest known code (Levenshtein,
FR-IMP-02) and the row is skipped; the remaining rows are imported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from twin_core.aliases import AliasIndex
from twin_core.config import TwinConfig
from twin_core.dq import DqIssue, unknown_value
from twin_core.importer.model import RawUpload
from twin_core.importer.tables import (
    RecognizedTable,
    Row,
    TableKind,
    minutes_factor,
    recognize,
    require_tables,
)
from twin_core.importer.values import parse_date, parse_int, parse_number


@dataclass(frozen=True, slots=True)
class LineRow:
    day: date
    line: str
    line_src: str
    plan_qty: int | None
    produced: int
    worked_min: float
    reported_load_pct: float | None


@dataclass(frozen=True, slots=True)
class DowntimeRow:
    day: date
    area: str
    equipment: str
    reason_code: str
    reason_src: str
    duration_min: float


@dataclass(frozen=True, slots=True)
class PlanRow:
    model: str
    model_src: str
    qty: int


@dataclass(frozen=True, slots=True)
class QualityRow:
    day: date
    area: str
    produced: int | None
    defects: int
    defect_pct: float | None


@dataclass(slots=True)
class ParsedImport:
    """Typed content of an upload plus everything that went wrong on the way."""

    kind: str
    files: list[str]
    lines: list[LineRow] = field(default_factory=list)
    downtime: list[DowntimeRow] = field(default_factory=list)
    plan: list[PlanRow] = field(default_factory=list)
    quality: list[QualityRow] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    issues: list[DqIssue] = field(default_factory=list)
    """DQ-07 findings (unknown names, unreadable values)."""
    warnings: list[str] = field(default_factory=list)


class _SkipRow(Exception):
    """A cell of the row could not be used; the issue is already recorded."""


class _RowReader:
    def __init__(self, table: RecognizedTable, issues: list[DqIssue]) -> None:
        self.table = table
        self.issues = issues

    def _fail(
        self, row: Row, field_name: str, kind: str, raw: str, suggestion: str | None = None
    ) -> _SkipRow:
        self.issues.append(
            unknown_value(
                kind=kind,
                value=raw,
                suggestion=suggestion,
                period_date=self._row_date(row),
                table=self.table.kind.value,
                source=row.source,
                row=row.number,
                column=self.table.headers.get(field_name, field_name),
            )
        )
        return _SkipRow()

    @staticmethod
    def _row_date(row: Row) -> date | None:
        try:
            return parse_date(row.get("date"))
        except ValueError:
            return None

    def value[T](self, row: Row, field_name: str, kind: str, parse: Callable[[str], T]) -> T:
        raw = row.get(field_name)
        try:
            return parse(raw)
        except ValueError:
            raise self._fail(row, field_name, kind, raw) from None

    def optional[T](
        self, row: Row, field_name: str, kind: str, parse: Callable[[str], T]
    ) -> T | None:
        if field_name not in self.table.headers or not row.get(field_name):
            return None
        return self.value(row, field_name, kind, parse)

    def code(self, row: Row, field_name: str, index: AliasIndex) -> str:
        raw = row.get(field_name)
        code = index.resolve(raw) if raw else None
        if code is None:
            ranked = index.suggest(raw, limit=1) if raw else []
            raise self._fail(row, field_name, index.kind, raw, ranked[0].code if ranked else None)
        return code


def _rows[T](
    table: RecognizedTable, issues: list[DqIssue], build: Callable[[_RowReader, Row], T]
) -> list[T]:
    reader = _RowReader(table, issues)
    out: list[T] = []
    for row in table.rows:
        try:
            out.append(build(reader, row))
        except _SkipRow:
            continue
    return out


def parse_upload(upload: RawUpload, cfg: TwinConfig) -> ParsedImport:
    """Recognize the tables of an upload and convert their rows to typed records."""
    found, leftovers = recognize(upload.tables)
    require_tables(found, [t.source for t in upload.tables] or upload.files)
    parsed = ParsedImport(
        kind=upload.kind.value,
        files=list(upload.files),
        text=[*upload.text, *leftovers],
        warnings=list(upload.warnings),
    )
    aliases = cfg.aliases
    lines_t = found[TableKind.LINES]
    worked_factor = minutes_factor(lines_t.headers["worked"], default_hours=True)

    def line_row(r: _RowReader, row: Row) -> LineRow:
        return LineRow(
            day=r.value(row, "date", "date", parse_date),
            line=r.code(row, "line", aliases.lines),
            line_src=row.get("line"),
            plan_qty=r.optional(row, "plan", "number", parse_int),
            produced=r.value(row, "fact", "number", parse_int),
            worked_min=r.value(row, "worked", "number", parse_number) * worked_factor,
            reported_load_pct=r.optional(row, "load", "number", parse_number),
        )

    downtime_t = found[TableKind.DOWNTIME]
    duration_factor = minutes_factor(downtime_t.headers["duration"], default_hours=False)

    def downtime_row(r: _RowReader, row: Row) -> DowntimeRow:
        day = r.value(row, "date", "date", parse_date)
        equipment = r.code(row, "equipment", aliases.equipment)
        if row.get("area"):
            area = r.code(row, "area", aliases.areas)
        else:
            area = cfg.area_of_equipment(equipment).code
        return DowntimeRow(
            day=day,
            area=area,
            equipment=equipment,
            reason_code=r.code(row, "reason", aliases.reasons),
            reason_src=row.get("reason"),
            duration_min=r.value(row, "duration", "number", parse_number) * duration_factor,
        )

    def plan_row(r: _RowReader, row: Row) -> PlanRow:
        return PlanRow(
            model=r.code(row, "model", aliases.products),
            model_src=row.get("model"),
            qty=r.value(row, "qty", "number", parse_int),
        )

    def quality_row(r: _RowReader, row: Row) -> QualityRow:
        return QualityRow(
            day=r.value(row, "date", "date", parse_date),
            area=r.code(row, "area", aliases.areas),
            produced=r.optional(row, "produced", "number", parse_int),
            defects=r.value(row, "defects", "number", parse_int),
            defect_pct=r.optional(row, "defect_pct", "number", parse_number),
        )

    parsed.lines = _rows(lines_t, parsed.issues, line_row)
    parsed.downtime = _rows(downtime_t, parsed.issues, downtime_row)
    parsed.plan = _rows(found[TableKind.PLAN], parsed.issues, plan_row)
    parsed.quality = _rows(found[TableKind.QUALITY], parsed.issues, quality_row)
    for item in parsed.downtime:
        expected_area = cfg.area_of_equipment(item.equipment).code
        if item.area != expected_area:
            parsed.warnings.append(
                f"downtime {item.day.isoformat()} {item.equipment}: area {item.area} in the file, "
                f"{expected_area} in plant.yaml"
            )
    return parsed
