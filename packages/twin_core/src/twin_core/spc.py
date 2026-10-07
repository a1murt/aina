"""SPC p-chart of defect share per area and shift with Western Electric rules (SPEC §11.3, AL-Q2).

* Centre line p̄ = Σ defects / Σ PQ over the last ``baseline_size`` (20) closed shifts, leaving out
  shifts flagged with a special cause.
* Limits per subgroup size nᵢ: σᵢ = √(p̄(1−p̄)/nᵢ), UCLᵢ = p̄ + 3σᵢ, LCLᵢ = max(0, p̄ − 3σᵢ).
* Each point gets zᵢ = (pᵢ − p̄)/σᵢ, so the zone rules work with varying nᵢ:

  1. one point beyond 3σ;
  2. 2 of 3 consecutive points beyond 2σ on the same side;
  3. 4 of 5 consecutive points beyond 1σ on the same side;
  4. 8 consecutive points on the same side of the centre line.

  A violation is reported at the point that completes the pattern, and that point itself is in the
  zone (the usual convention), so AL-Q2 for a new shift = a violation ending at the last point.
* The 2% norm (``rules.yaml: thresholds.defect_rate_limit``) is carried along for the chart.

Shifts without production (n = 0) are not points of the chart.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

BASELINE_SHIFTS = 20
"""SPEC §11.3: p̄ over the last 20 closed shifts."""
RULES = (1, 2, 3, 4)


@dataclass(frozen=True, slots=True)
class Subgroup:
    """One closed shift of one area: defects among ``n`` inspected units (PQ)."""

    key: str
    """Shift key, e.g. ``2026-10-15/B``; chronological order is the order of the input."""
    defects: int
    n: int
    special_cause: bool = False
    """Flagged by quality as a known special cause: excluded from p̄, still plotted."""


@dataclass(frozen=True, slots=True)
class Violation:
    rule: int
    side: int
    """+1 above the centre line, −1 below."""
    start: int
    end: int
    """Indices into :attr:`PChart.points` (inclusive); ``end`` completes the pattern."""
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PChartPoint:
    key: str
    defects: int
    n: int
    p: float
    ucl: float
    lcl: float
    sigma: float
    z: float
    in_baseline: bool
    special_cause: bool
    rules: tuple[int, ...] = ()
    """Rules whose violation ends at this point."""


@dataclass(frozen=True, slots=True)
class PChart:
    p_bar: float | None
    """``None`` when the baseline has no inspected units."""
    baseline_keys: tuple[str, ...]
    points: tuple[PChartPoint, ...]
    violations: tuple[Violation, ...]
    norm: float | None = None
    rules: tuple[int, ...] = field(default=RULES)

    @property
    def latest_violations(self) -> tuple[Violation, ...]:
        """Violations completed by the newest point (what a new AL-Q2 is raised for)."""
        if not self.points:
            return ()
        last = len(self.points) - 1
        return tuple(v for v in self.violations if v.end == last)

    @property
    def in_control(self) -> bool:
        return not self.violations


def _z(p: float, p_bar: float, sigma: float) -> float:
    if sigma > 0:
        return (p - p_bar) / sigma
    if p == p_bar:
        return 0.0
    return math.inf if p > p_bar else -math.inf


def western_electric(z: Sequence[float], rules: Sequence[int] = RULES) -> list[Violation]:
    """Western Electric rules 1–4 over z-scores (keys are positions as strings)."""
    return _western_electric(z, [str(i) for i in range(len(z))], rules)


def _western_electric(
    z: Sequence[float], keys: Sequence[str], rules: Sequence[int]
) -> list[Violation]:
    out: list[Violation] = []
    n = len(z)

    def add(rule: int, side: int, start: int, end: int) -> None:
        out.append(Violation(rule, side, start, end, tuple(keys[start : end + 1])))

    for i in range(n):
        zi = z[i]
        if 1 in rules and abs(zi) > 3:
            add(1, 1 if zi > 0 else -1, i, i)
        for rule, length, need, limit in ((2, 3, 2, 2.0), (3, 5, 4, 1.0)):
            if rule not in rules or i + 1 < length:
                continue
            window = z[i + 1 - length : i + 1]
            for side in (1, -1):
                if side * zi > limit and sum(1 for v in window if side * v > limit) >= need:
                    add(rule, side, i + 1 - length, i)
        if 4 in rules and i + 1 >= 8:
            window = z[i - 7 : i + 1]
            for side in (1, -1):
                if all(side * v > 0 for v in window):
                    add(4, side, i - 7, i)
    out.sort(key=lambda v: (v.end, v.rule, v.side))
    return out


def p_chart(
    subgroups: Sequence[Subgroup],
    *,
    baseline_size: int = BASELINE_SHIFTS,
    norm: float | None = None,
    rules: Sequence[int] = RULES,
) -> PChart:
    """Build the p-chart for chronologically ordered closed shifts (oldest first)."""
    if baseline_size <= 0:
        raise ValueError("baseline_size must be positive")
    for sg in subgroups:
        if sg.n < 0 or sg.defects < 0 or sg.defects > sg.n:
            raise ValueError(f"subgroup {sg.key}: need 0 <= defects <= n, got {sg.defects}/{sg.n}")
    produced = [sg for sg in subgroups if sg.n > 0]
    recent = produced[-baseline_size:]
    baseline = [sg for sg in recent if not sg.special_cause]
    total_n = sum(sg.n for sg in baseline)
    p_bar = sum(sg.defects for sg in baseline) / total_n if total_n else None
    baseline_keys = tuple(sg.key for sg in baseline)
    if p_bar is None:
        return PChart(None, baseline_keys, (), (), norm, tuple(rules))

    in_base = set(baseline_keys)
    raw: list[tuple[Subgroup, float, float, float, float, float]] = []
    for sg in produced:
        p = sg.defects / sg.n
        sigma = math.sqrt(p_bar * (1.0 - p_bar) / sg.n)
        raw.append(
            (sg, p, p_bar + 3 * sigma, max(0.0, p_bar - 3 * sigma), sigma, _z(p, p_bar, sigma))
        )
    keys = [sg.key for sg, *_ in raw]
    violations = _western_electric([r[5] for r in raw], keys, rules)
    ending: dict[int, list[int]] = {}
    for v in violations:
        ending.setdefault(v.end, []).append(v.rule)
    points = tuple(
        PChartPoint(
            key=sg.key,
            defects=sg.defects,
            n=sg.n,
            p=p,
            ucl=ucl,
            lcl=lcl,
            sigma=sigma,
            z=z,
            in_baseline=sg.key in in_base,
            special_cause=sg.special_cause,
            rules=tuple(sorted(set(ending.get(i, [])))),
        )
        for i, (sg, p, ucl, lcl, sigma, z) in enumerate(raw)
    )
    return PChart(p_bar, baseline_keys, points, tuple(violations), norm, tuple(rules))
