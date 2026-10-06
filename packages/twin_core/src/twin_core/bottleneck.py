"""Bottleneck detection (SPEC §9.5).

M1 implements the aggregate path for imported periods (no state timelines): the bottleneck of a
day is the flow step with the smallest output; over the period, the one with the smallest mean
output; ``shifting`` when the daily bottleneck changed. The active-period method for event data
(Roser, Nakano, Tanaka 2002) is added here in M3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class AggregateBottleneck:
    by_day: Mapping[date, str]
    overall: str | None
    shifting: bool
    mean_output: Mapping[str, float]
    """Mean output per period unit (shift) of each flow step."""

    def to_report(self) -> dict[str, Any]:
        """The import-report form (``import_expected.json: bottleneck_aggregate``)."""
        return {
            "by_day": {day.isoformat(): step for day, step in sorted(self.by_day.items())},
            "overall": self.overall,
            "shifting": self.shifting,
        }


def aggregate_bottleneck(
    output: Mapping[date, Mapping[str, float]], order: Sequence[str]
) -> AggregateBottleneck:
    """Bottleneck by minimum output.

    Args:
        output: ``day -> step -> output`` (step = area or line code).
        order: flow order of the steps; ties go to the earliest step in this order.
    """
    rank = {step: i for i, step in enumerate(order)}

    def first_min(values: Mapping[str, float]) -> str | None:
        steps = sorted((s for s in values if s in rank), key=rank.__getitem__)
        return min(steps, key=values.__getitem__) if steps else None

    by_day: dict[date, str] = {}
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for day in sorted(output):
        step = first_min(output[day])
        if step is not None:
            by_day[day] = step
        for name, value in output[day].items():
            if name in rank:
                totals[name] = totals.get(name, 0.0) + value
                counts[name] = counts.get(name, 0) + 1
    mean = {step: totals[step] / counts[step] for step in order if step in totals}
    return AggregateBottleneck(
        by_day=by_day,
        overall=first_min(mean),
        shifting=len(set(by_day.values())) > 1,
        mean_output=mean,
    )
