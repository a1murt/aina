"""Import of the case files: ``.docx``, ``.xlsx``, ``.csv`` (SPEC §7.4, FR-IMP-01..07).

Pipeline (pure, no DB): :func:`read_upload` (format readers) -> :func:`parse_upload` (table
recognition by headers, alias mapping, DQ-07) -> :func:`build_import_report` (KPIs ISO 22400,
DQ-01…06, aggregate alerts, plan, flow, bottleneck). :func:`run_import` chains the three.
The API persists the result (``services/api``).
"""

from twin_core.importer.constraints import (
    CONSTRAINT_KEYS,
    ConstraintCheck,
    configured_constraints,
    parse_constraints,
    resolve_constraints,
)
from twin_core.importer.model import (
    ImportFormatError,
    ImportKind,
    RawTable,
    RawUpload,
    UploadedFile,
)
from twin_core.importer.parse import ParsedImport, parse_upload
from twin_core.importer.readers import read_upload
from twin_core.importer.report import (
    DowntimeRecord,
    FlowSummary,
    ImportReport,
    PlanSummary,
    ShiftReportRecord,
    build_import_report,
    run_import,
)
from twin_core.importer.template import build_template

__all__ = [
    "CONSTRAINT_KEYS",
    "ConstraintCheck",
    "DowntimeRecord",
    "FlowSummary",
    "ImportFormatError",
    "ImportKind",
    "ImportReport",
    "ParsedImport",
    "PlanSummary",
    "RawTable",
    "RawUpload",
    "ShiftReportRecord",
    "UploadedFile",
    "build_import_report",
    "build_template",
    "configured_constraints",
    "parse_constraints",
    "parse_upload",
    "read_upload",
    "resolve_constraints",
    "run_import",
]
