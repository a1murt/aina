"""Levers: counterfactual fast-model runs with common random numbers (SPEC §10.4).

Each lever is expressed as public overrides (FR-FC-02), so the UI can apply it to the what-if
panel as is:

* ``eliminate_failures`` per class A/B unit — ``mtbf_multiplier[unit] = failure_free_multiplier``;
* ``defect_norm`` per area above ``rules.yaml: defect_rate_limit`` — ``defect_rate[area] = limit``;
* ``filter_policy`` — ``predictive_shift_change``;
* ``extra_shift`` — the first non-working ``levers.extra_shift.weekday`` after "now";
* ``buffer_capacity`` — ``levers.buffer.code`` + ``levers.buffer.add`` places.

For every lever: Δ P50, Δ mean, Δ P(target) for both targets and the effect in ₸ (§10.5) over
the remaining horizon; levers are ranked by ``rank_by`` and the first ``top`` are flagged.
:func:`shifts_needed` answers "how many extra shifts make P(target) exceed ``p_goal``".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict

from twin_core.calendar import PlantCalendar
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.config.forecast import CandidateShifts
from twin_core.forecast.effect import Economics, effect_per_run
from twin_core.forecast.model import Paths
from twin_core.forecast.overrides import ExtraShift, Overrides
from twin_core.forecast.params import CalibrationParams
from twin_core.forecast.result import Outcome, Quantiles, p_reach, quantiles


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class LeverSpec:
    id: str
    kind: str
    params: dict[str, Any]
    overrides: Overrides


class LeverResult(_Frozen):
    id: str
    kind: str
    """``eliminate_failures`` | ``defect_norm`` | ``filter_policy`` | ``extra_shift`` |
    ``buffer_capacity`` (the UI renders the text from kind + params)."""
    params: dict[str, Any]
    overrides: dict[str, Any]
    delta_p50: float
    delta_mean: float
    delta_p_reach: dict[str, float]
    effect_kzt: Quantiles
    rank: int
    top: bool


class ShiftsNeeded(_Frozen):
    target: str
    qty: int
    candidates_set: str
    """Name from ``levers.shifts_needed.candidates`` (e.g. ``saturdays``)."""
    shifts: int | None
    """Fewest extra shifts with P(total >= qty) > ``p_goal``; ``None``: not reachable with the
    candidate shifts of the month."""
    dates: list[dict[str, str]]
    p_reach: float | None
    p_base: float
    p_max: float
    candidates: int
    p_goal: float


class LeversResult(_Frozen):
    month: str
    as_of: datetime
    n_runs: int
    seed: int
    base: Outcome
    levers: list[LeverResult]
    shifts_needed_for_target: dict[str, dict[str, ShiftsNeeded]]
    """Target -> candidate set -> answer."""
    calibration_id: int | None = None
    duration_ms: int | None = None


def _first_free_day(
    calendar: PlantCalendar,
    *,
    weekday: int,
    shifts: list[str],
    as_of: datetime,
    month_end: datetime,
) -> date | None:
    tz = calendar.tz
    day = ensure_utc(as_of).astimezone(tz).date()
    last = ensure_utc(month_end).astimezone(tz).date()
    while day < last:
        free = not set(shifts) & set(calendar.working_shift_codes(day))
        if (
            day.isoweekday() == weekday
            and free
            and all(calendar.shift(day, c).start >= ensure_utc(as_of) for c in shifts)
        ):
            return day
        day = date.fromordinal(day.toordinal() + 1)
    return None


def lever_specs(
    cfg: TwinConfig,
    params: CalibrationParams,
    *,
    as_of: datetime,
    month_end: datetime,
) -> list[LeverSpec]:
    lv = cfg.simulation.forecast.levers
    specs: list[LeverSpec] = []
    for code, eq in params.equipment.items():
        if eq.criticality in ("A", "B"):
            specs.append(
                LeverSpec(
                    f"eliminate_failures:{code}",
                    "eliminate_failures",
                    {"equipment": code, "criticality": eq.criticality},
                    Overrides(mtbf_multiplier={code: lv.failure_free_multiplier}),
                )
            )
    limit = cfg.rules.thresholds.defect_rate_limit
    for area, d in params.areas.items():
        if d.rate.mean > limit:
            specs.append(
                LeverSpec(
                    f"defect_norm:{area}",
                    "defect_norm",
                    {"area": area, "from": round(d.rate.mean, 4), "to": limit},
                    Overrides(defect_rate={area: limit}),
                )
            )
    pf = cfg.simulation.paint_filters
    if params.filters is not None and pf is not None and pf.policy != "predictive_shift_change":
        specs.append(
            LeverSpec(
                "filter_policy",
                "filter_policy",
                {"policy": "predictive_shift_change"},
                Overrides(filter_policy="predictive_shift_change"),
            )
        )
    day = _first_free_day(
        cfg.calendar,
        weekday=lv.extra_shift.weekday,
        shifts=list(lv.extra_shift.shifts),
        as_of=as_of,
        month_end=month_end,
    )
    if day is not None:
        specs.append(
            LeverSpec(
                f"extra_shift:{day.isoformat()}",
                "extra_shift",
                {"date": day.isoformat(), "shifts": list(lv.extra_shift.shifts)},
                Overrides(extra_shifts=[ExtraShift(date=day, shifts=list(lv.extra_shift.shifts))]),
            )
        )
    buf = params.buffers.get(lv.buffer.code)
    if buf is not None:
        specs.append(
            LeverSpec(
                f"buffer_capacity:{buf.code}",
                "buffer_capacity",
                {"buffer": buf.code, "from": buf.capacity, "to": buf.capacity + lv.buffer.add},
                Overrides(buffer_capacity={buf.code: buf.capacity + lv.buffer.add}),
            )
        )
    return specs


Runner = Callable[[Overrides], tuple[Paths, tuple[str, ...], int]]
"""Runs the model for overrides: (paths, areas of the lines, extra shift count)."""


def evaluate_levers(
    cfg: TwinConfig,
    specs: list[LeverSpec],
    *,
    base: Paths,
    run: Runner,
    targets: dict[str, int],
    econ: Economics,
) -> list[LeverResult]:
    lv = cfg.simulation.forecast.levers
    base_p50 = float(np.percentile(base.total, 50))
    base_reach = p_reach(base.total, targets)
    rows: list[tuple[float, LeverSpec, float, float, dict[str, float], Quantiles]] = []
    for spec in specs:
        paths, areas, extra = run(spec.overrides)
        effect = quantiles(effect_per_run(base, paths, areas=areas, econ=econ, extra_shifts=extra))
        reach = p_reach(paths.total, targets)
        d_p50 = round(float(np.percentile(paths.total, 50)) - base_p50, 1)
        d_mean = round(float((paths.total - base.total).mean()), 1)
        d_reach = {k: round(reach[k] - base_reach[k], 4) for k in targets}
        score = effect.mean if lv.rank_by == "effect_kzt" else d_p50
        rows.append((score, spec, d_p50, d_mean, d_reach, effect))
    rows.sort(key=lambda r: (-r[0], r[1].id))
    return [
        LeverResult(
            id=spec.id,
            kind=spec.kind,
            params=spec.params,
            overrides=spec.overrides.normalized(),
            delta_p50=d_p50,
            delta_mean=d_mean,
            delta_p_reach=d_reach,
            effect_kzt=effect,
            rank=rank,
            top=rank <= lv.top,
        )
        for rank, (_score, spec, d_p50, d_mean, d_reach, effect) in enumerate(rows, start=1)
    ]


def candidate_shifts(
    cfg: TwinConfig, candidates: CandidateShifts, *, as_of: datetime, month_end: datetime
) -> list[tuple[date, str]]:
    """Extra-shift slots for :func:`shifts_needed`, in time order."""
    cal = cfg.calendar
    codes = list(candidates.shifts) if candidates.shifts is not None else list(cal.shift_codes)
    tz = cfg.timezone
    moment = ensure_utc(as_of)
    day = moment.astimezone(tz).date()
    last = ensure_utc(month_end).astimezone(tz).date()
    out: list[tuple[date, str]] = []
    while day < last:
        eligible = day.isoweekday() in candidates.weekdays or (
            candidates.include_holidays and cal.is_holiday(day)
        )
        if eligible:
            worked = set(cal.working_shift_codes(day))
            for code in codes:
                if code not in worked and cal.shift(day, code).start >= moment:
                    out.append((day, code))
        day = date.fromordinal(day.toordinal() + 1)
    return out


def _as_overrides(slots: list[tuple[date, str]]) -> Overrides:
    by_day: dict[date, list[str]] = {}
    for day, code in slots:
        by_day.setdefault(day, []).append(code)
    return Overrides(extra_shifts=[ExtraShift(date=d, shifts=c) for d, c in by_day.items()])


def shifts_needed(
    cfg: TwinConfig,
    *,
    base: Paths,
    run: Runner,
    targets: dict[str, int],
    as_of: datetime,
    month_end: datetime,
) -> dict[str, dict[str, ShiftsNeeded]]:
    """Per target and candidate set: the fewest extra shifts (in time order) with
    P(total >= target) > ``p_goal``. Binary search: with common random numbers P does not
    decrease as shifts are added."""
    sn = cfg.simulation.forecast.levers.shifts_needed
    goal = sn.p_goal
    out: dict[str, dict[str, ShiftsNeeded]] = {name: {} for name in targets}
    for set_name, cand in sn.candidates.items():
        slots = candidate_shifts(cfg, cand, as_of=as_of, month_end=month_end)
        memo: dict[int, Paths] = {0: base}

        def paths(
            k: int, slots: list[tuple[date, str]] = slots, memo: dict[int, Paths] = memo
        ) -> Paths:
            if k not in memo:
                memo[k] = run(_as_overrides(slots[:k]))[0]
            return memo[k]

        def p(k: int, qty: int) -> float:
            return float((np.rint(paths(k).total) >= qty).mean())

        n = len(slots)
        for name, qty in targets.items():
            p0 = p(0, qty)
            p_max = p(n, qty) if n else p0
            k: int | None
            if p0 > goal:
                k = 0
            elif p_max <= goal:
                k = None
            else:
                lo, hi = 0, n  # p(lo) <= goal < p(hi)
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if p(mid, qty) > goal:
                        hi = mid
                    else:
                        lo = mid
                k = hi
            out[name][set_name] = ShiftsNeeded(
                target=name,
                qty=qty,
                candidates_set=set_name,
                shifts=k,
                dates=[{"date": d.isoformat(), "shift": c} for d, c in slots[: k or 0]],
                p_reach=None if k is None else round(p(k, qty), 4),
                p_base=round(p0, 4),
                p_max=round(p_max, 4),
                candidates=n,
                p_goal=goal,
            )
    return out
