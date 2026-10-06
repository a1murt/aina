"""Models for ``config/reason_codes.yaml`` — downtime reason tree (ISO 22400 PDOT/ADOT)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from twin_core.config.common import Code, Named, NonEmptyStr, StrictModel
from twin_core.domain import ReasonBucket

FALLBACK_REASON_CODE = "UNK"
"""Reason assigned when neither the event nor the alarm alias gives one (SPEC FR-ENG-02)."""


class Reason(Named):
    code: Code
    planned: bool
    """True: excluded from planned busy time (PDOT); False: unplanned (ADOT)."""
    bucket: ReasonBucket
    aliases: list[NonEmptyStr] = []


class ReasonCategory(Named):
    code: Code
    reasons: Annotated[list[Reason], Field(min_length=1)]


class ReasonCodesConfig(StrictModel):
    """Root of ``reason_codes.yaml``."""

    version: Literal[1]
    categories: Annotated[list[ReasonCategory], Field(min_length=1)]

    @property
    def reasons(self) -> list[Reason]:
        return [reason for category in self.categories for reason in category.reasons]
