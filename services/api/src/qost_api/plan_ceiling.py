"""Line capacity of a month (the "ceiling" of ``GET /plan/progress``).

The flow line cannot release more than its slowest line: ``ceiling = Σ over the working shifts
of the month (shift minutes x 60 / ICT of the slowest flow line)``. Shift lengths come from the
plant calendar and ICT from ``plant.yaml``; nothing is hard-coded (≈ 5 191 for October 2026:
42 shifts x 480 min x 60 / 233 s).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from twin_core.config import TwinConfig
from twin_core.kpi import minutes_to_units


@dataclass(frozen=True, slots=True)
class Ceiling:
    qty: int
    """Whole cars the line can release in the month at the bottleneck ICT."""
    rate_per_shift: float
    shifts: int
    ict_seconds: float
    line: str
    """The slowest flow line (the structural bottleneck)."""

    def as_json(self) -> dict[str, int | float]:
        return {
            "qty": self.qty,
            "rate_per_shift": round(self.rate_per_shift, 1),
            "shifts": self.shifts,
        }


def line_ceiling(cfg: TwinConfig, month: str) -> Ceiling | None:
    """Ceiling of the month ``YYYY-MM``; ``None`` without working shifts or flow lines."""
    if not cfg.flow_lines:
        return None
    line = max(cfg.flow_lines, key=lambda code: float(cfg.lines[code].ict_seconds))
    ict = float(cfg.lines[line].ict_seconds)
    year, mon = (int(x) for x in month.split("-")[:2])
    first = date(year, mon, 1)
    last = date(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)
    shifts = [s for s in cfg.calendar.materialize(first, last) if s.working]
    if not shifts or ict <= 0:
        return None
    total = sum(minutes_to_units(s.duration.total_seconds() / 60.0, ict) or 0.0 for s in shifts)
    return Ceiling(
        qty=math.floor(total + 1e-9),
        rate_per_shift=total / len(shifts),
        shifts=len(shifts),
        ict_seconds=ict,
        line=line,
    )
