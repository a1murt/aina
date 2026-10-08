"""Forecast inputs from the virtual plant (SPEC §10.1): records → ``CalibrationInputs``, model →
``PlantState``.

The same facts the API reads from the §8 tables (``equipment_state``, ``downtime``,
``unit_event``, ``buffer_level``, ``ckd_stock``, ``telemetry``), built directly from model
records. Used to validate the fast model against the simulator and as test fixtures; an
integration test checks that the database adapter returns the same inputs.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from qost_sim.model import PlantModel, Rec
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState
from twin_core.events import FINISHED_RESULTS, FIRST_EXIT_RESULTS
from twin_core.forecast.calibration import (
    WIP_LOOKBACK,
    CalibrationInputs,
    LineShiftFacts,
    Stop,
    UnitExit,
    calibration_window,
    held_from_exits,
    month_bounds,
    month_of,
)
from twin_core.forecast.params import KitLot, OpenDown, PlantState

_DOWN = {EquipmentState.DOWN_UNPLANNED.value, EquipmentState.DOWN_PLANNED.value}
_PRODUCING = {EquipmentState.RUNNING.value, EquipmentState.DEGRADED.value}
_REJECTS = frozenset({"defect", "scrap"})


@dataclass(frozen=True, slots=True)
class Interval:
    start: float
    end: float | None
    """``None`` = still open at the cut-off."""
    state: str
    reason: str | None


def state_intervals(
    records: Iterable[Rec], entity_type: str, *, until: float
) -> dict[str, list[Interval]]:
    """State intervals per entity from ``state`` records with ``t <= until``."""
    open_: dict[str, tuple[float, str, str | None]] = {}
    out: dict[str, list[Interval]] = defaultdict(list)
    for rec in records:
        if rec.kind != "state" or rec.entity_type != entity_type or rec.t > until:
            continue
        prev = open_.get(rec.entity)
        if prev is not None and rec.t > prev[0]:
            out[rec.entity].append(Interval(prev[0], rec.t, prev[1], prev[2]))
        open_[rec.entity] = (rec.t, rec.data["state"], rec.data.get("reason_code"))
    for entity, (start, state, reason) in open_.items():
        out[entity].append(Interval(start, None, state, reason))
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def calibration_inputs(
    model: PlantModel,
    records: Sequence[Rec],
    *,
    as_of: datetime | None = None,
    window_days: int | None = None,
) -> CalibrationInputs:
    """Calibration facts over the window before ``as_of`` (default: the model's "now").

    ``records`` must cover the model run from its start (state intervals begin there).
    """
    cfg = model.cfg
    moment = ensure_utc(as_of) if as_of is not None else model.now
    w_from, w_to, days = calibration_window(cfg, moment, window_days)
    t_now = model.sec(moment)
    w0, w1 = model.sec(w_from), model.sec(w_to)
    threshold = cfg.rules.thresholds.microstop_threshold_s

    eq_int = state_intervals(records, "equipment", until=t_now)
    operating_h: dict[str, float] = {}
    stops: list[Stop] = []
    for code in cfg.equipment:
        hours = 0.0
        for iv in eq_int.get(code, []):
            end = t_now if iv.end is None else iv.end
            if iv.state == EquipmentState.RUNNING.value:
                hours += _overlap(iv.start, end, w0, w1) / 3600.0
            elif iv.state in _DOWN and w0 <= iv.start < w1:
                if iv.end is not None and iv.end - iv.start < threshold:
                    continue  # microstop
                reason = iv.reason or "UNK"
                known = cfg.reasons.get(reason)
                planned = (
                    known.planned
                    if known is not None
                    else iv.state == EquipmentState.DOWN_PLANNED.value
                )
                stops.append(
                    Stop(
                        equipment=code,
                        start=model.at(iv.start),
                        end=None if iv.end is None else model.at(iv.end),
                        planned=planned,
                        reason=reason,
                    )
                )
        operating_h[code] = hours

    shifts = cfg.calendar.shifts_between(w_from, w_to, working_only=True)
    shifts = [s for s in shifts if s.start >= w_from and s.end <= w_to]
    spans = [(model.sec(s.start), model.sec(s.end)) for s in shifts]
    starts = [a for a, _ in spans]
    line_int = state_intervals(records, "line", until=t_now)
    apt: dict[tuple[str, int], float] = defaultdict(float)
    degraded: dict[tuple[str, int], float] = defaultdict(float)
    for line in cfg.flow_lines:
        for iv in line_int.get(line, []):
            end = t_now if iv.end is None else iv.end
            productive = iv.state in _PRODUCING or (
                iv.state == EquipmentState.DOWN_UNPLANNED.value
                and iv.end is not None
                and iv.end - iv.start < threshold
            )
            if not productive:
                continue
            i = max(0, bisect.bisect_right(starts, iv.start) - 1)
            while i < len(spans) and spans[i][0] < end:
                part = _overlap(iv.start, end, *spans[i])
                if part > 0:
                    apt[(line, i)] += part
                    if iv.state == EquipmentState.DEGRADED.value:
                        degraded[(line, i)] += part
                i += 1

    exits: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    defects: Counter[tuple[str, int]] = Counter()
    repaint: Counter[tuple[str, int]] = Counter()
    codes: dict[str, Counter[str]] = defaultdict(Counter)
    for rec in records:
        if rec.kind != "unit" or not w0 <= rec.t < w1:
            continue
        result = rec.data["result"]
        if result not in FIRST_EXIT_RESULTS:
            continue
        i = bisect.bisect_right(starts, rec.t) - 1
        if i < 0 or rec.t >= spans[i][1]:
            continue
        line = rec.data["line"]
        key = (line, i)
        exits[key][rec.data["product"]] += 1
        if result in _REJECTS:
            defects[key] += 1
            defect_code = rec.data.get("defect_code")
            if defect_code is not None:
                codes[line][defect_code] += 1
                if defect_code in cfg.defects and cfg.defects[defect_code].repaint:
                    repaint[key] += 1

    facts = [
        LineShiftFacts(
            line=line,
            shift_date=shift.shift_date,
            shift_code=shift.code,
            apt_s=apt.get((line, i), 0.0),
            degraded_s=degraded.get((line, i), 0.0),
            exits=dict(exits.get((line, i), {})),
            defects=defects.get((line, i), 0),
            repaint_defects=repaint.get((line, i), 0),
        )
        for i, shift in enumerate(shifts)
        for line in cfg.flow_lines
    ]
    return CalibrationInputs(
        window_from=w_from,
        window_to=w_to,
        working_days=days,
        operating_h=operating_h,
        stops=tuple(stops),
        line_shifts=tuple(facts),
        defect_codes={line: dict(c) for line, c in codes.items()},
    )


def finished_output(
    cfg: TwinConfig, model: PlantModel, records: Iterable[Rec], start: datetime, end: datetime
) -> int:
    """Finished cars (last flow line, ``FINISHED_RESULTS``) with ``start <= ts < end``."""
    last = cfg.flow_lines[-1]
    t0, t1 = model.sec(start), model.sec(end)
    return sum(
        1
        for rec in records
        if rec.kind == "unit"
        and rec.data["line"] == last
        and rec.data["result"] in FINISHED_RESULTS
        and t0 <= rec.t < t1
    )


def plant_state(model: PlantModel, records: Iterable[Rec]) -> PlantState:
    """Exact plant state at the model's "now" (``records`` must cover the month so far)."""
    cfg = model.cfg
    now = model.now
    month_start, _ = month_bounds(cfg, month_of(cfg, now))
    open_downs = [
        OpenDown(
            equipment=code,
            state=unit.state.value,  # type: ignore[arg-type]
            reason=unit.reason,
            since=model.at(unit.since),
        )
        for code, unit in model.units.items()
        if unit.state.value in _DOWN
    ]
    filter_dp = {
        code: unit.filter_dp() for code, unit in model.units.items() if unit.filters is not None
    }
    transit = [
        KitLot(product=lot.product, qty=lot.qty, dispatched=now, arrival=model.at(lot.arrival))
        for lot in sorted(model.ckd.transit, key=lambda x: (x.arrival, x.seq))
    ]
    since = model.sec(now - WIP_LOOKBACK)
    exits = [
        UnitExit(r.data["body_id"], r.data["line"], model.at(r.t), r.data["result"])
        for r in records
        if r.kind == "unit" and r.t >= since
    ]
    buffers = {code: float(b.level) for code, b in model.buffers.items()}
    return PlantState(
        as_of=now,
        mtd_output=finished_output(cfg, model, records, month_start, now),
        held=held_from_exits(cfg, exits, buffers),
        buffers=buffers,
        open_downs=open_downs,
        filter_dp=filter_dp,
        kits={p: float(k) for p, k in model.ckd.kits.items()},
        kits_in_transit=transit,
        source="sim",
    )
