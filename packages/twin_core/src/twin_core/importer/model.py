"""Raw upload model shared by the readers, the table recognizer and the report builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ImportKind(StrEnum):
    """Upload format (``import_job.kind``)."""

    DOCX = "docx"
    XLSX = "xlsx"
    CSV = "csv"


class ImportFormatError(ValueError):
    """The upload cannot be imported at all (unsupported file, required table missing, …).

    Problems with single values do not raise: they become DQ-07 issues and the row is skipped.
    """

    def __init__(self, message: str, *, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


@dataclass(frozen=True, slots=True)
class UploadedFile:
    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class RawTable:
    """A grid of cell texts as found in the file (header row included)."""

    source: str
    """Where it came from: ``case.docx#table1``, ``case.xlsx#Линии``, ``01_lines.csv``."""
    rows: list[list[str]]


@dataclass(slots=True)
class RawUpload:
    """Everything read from one upload, before recognition."""

    kind: ImportKind
    files: list[str]
    tables: list[RawTable] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    """Free-text paragraphs (docx body, ``.txt`` files) — the source of FR-IMP-03 constraints."""
    warnings: list[str] = field(default_factory=list)
