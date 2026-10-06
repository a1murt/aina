"""Models for ``config/defect_codes.yaml`` (SPEC §5.5)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from twin_core.config.common import Code, Named, NonEmptyStr, NonNegativeFloat, StrictModel
from twin_core.domain import Disposition

ANY_AREA = "ANY"
"""Defect-code area keyword: the code may be raised by any area."""


class DefectCode(Named):
    code: Code
    area: Code
    """Area code from plant.yaml, or ``ANY``."""
    disposition: Disposition
    rework_min: NonNegativeFloat
    repaint: bool = False
    """True: a full repaint, the body goes back to the paint line entry."""
    aliases: list[NonEmptyStr] = []


class DefectCodesConfig(StrictModel):
    """Root of ``defect_codes.yaml``."""

    version: Literal[1]
    defects: Annotated[list[DefectCode], Field(min_length=1)]
