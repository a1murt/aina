"""Shift reports (SPEC §11.5, US-7): input assembly, template text, number check.

Pure functions only; the API reads the facts and talks to the LLM (``qost_api.reports``).
"""

from __future__ import annotations

from twin_core.report.assemble import (
    AlertFact,
    BottleneckFact,
    DefectFact,
    KpiFact,
    ShiftFacts,
    StopFact,
    build_shift_input,
    entity_name,
    next_working_shift,
)
from twin_core.report.model import LANGS, Lang, ShiftInput
from twin_core.report.numbers import Mismatch, VerifyResult, canonical_json, extract, verify
from twin_core.report.template import (
    KK_DRAFT,
    MAX_WORDS,
    SECTIONS,
    has_sections,
    render_template,
    word_count,
)


def verify_report(text: str, data: ShiftInput) -> VerifyResult:
    """Number check of a report text against its input (entity names masked)."""
    return verify(text, data.model_dump(mode="json"), mask=data.names())


__all__ = [
    "KK_DRAFT",
    "LANGS",
    "MAX_WORDS",
    "SECTIONS",
    "AlertFact",
    "BottleneckFact",
    "DefectFact",
    "KpiFact",
    "Lang",
    "Mismatch",
    "ShiftFacts",
    "ShiftInput",
    "StopFact",
    "VerifyResult",
    "build_shift_input",
    "canonical_json",
    "entity_name",
    "extract",
    "has_sections",
    "next_working_shift",
    "render_template",
    "verify",
    "verify_report",
    "word_count",
]
