"""Shared pydantic building blocks for config models."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Code = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]+(-[A-Z0-9]+)*$")]
"""Entity code: Latin UPPER-KEBAB (``WELD-1``, ``CONV-03``, ``PM-SCHEDULED``)."""

Ident = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
"""Lower snake_case identifier: roles, equipment types, signal codes."""

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class StrictModel(BaseModel):
    """Immutable model that rejects unknown keys (typos in YAML must not pass silently)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Named(StrictModel):
    """Mixin for entities with Russian / Kazakh display names (UI only)."""

    name_ru: NonEmptyStr
    name_kk: NonEmptyStr | None = None


class Range(StrictModel):
    """Closed numeric interval ``[min, max]``."""

    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Range:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) must not exceed max ({self.max})")
        return self


class LogNormal(StrictModel):
    """Lognormal distribution given by its median and sigma of the underlying normal."""

    median: PositiveFloat
    sigma: NonNegativeFloat
