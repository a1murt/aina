"""PdM features (SPEC §11.1) — one implementation for the offline table and the online engine.

:func:`compute_features` evaluates a unit's history at any number of times ``T`` (vectorised NumPy):

* batch (``make ml-dataset``): every 15 min over a year of history;
* online (engine, stage M7b): :func:`features_at` — one ``T`` = "now" floored to the 5-min grid,
  history built from DB records by :func:`history_from_records`.

Everything at ``T`` uses only data strictly before ``T``: telemetry slots ``[T − W, T)`` and stops /
line exits that ended before ``T``. So truncating the history at ``T`` never changes the features
(tested: offline and online values are identical).

Telemetry lives on a regular grid (``simulation.yaml: telemetry.backfill_sample_period_s``, 5 min,
SPEC §6.5): each slot holds the latest sample at or before the slot time (one grid step back at
most, NaN if none) — 300 s backfill samples map 1:1, 60 s live samples are resampled.

Per signal and window W ∈ {1, 4, 24} h: ``mean``, ``std`` (population), ``max`` and ``slope`` =
Theil–Sen slope × W, i.e. the robust change over the window in signal units (the 24 h window is
fitted on 24 hourly means, the 4 h window on 10-minute means). Rate signals (events per hour) also
get ``count_4h`` = mean × 4 h.
Event features: microstops in 24 h, hours since the last planned maintenance / any repair / a
wear-reason repair, line cycles since the last planned maintenance, the current shift and the
scheduled working hours within the label horizon (from the calendar, known in advance).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import numpy.typing as npt

from qost_ml.spec import (
    COUNT_WINDOW_H,
    MICROSTOP_WINDOW_H,
    PdmSpec,
    assert_no_oracle,
)
from twin_core.calendar import PlantCalendar
from twin_core.clock import ensure_utc
from twin_core.domain import EquipmentState

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]
B = npt.NDArray[np.bool_]

US = 1_000_000
_HOUR_US = 3600 * US
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SLOPE_POINTS = 24
"""Theil–Sen is fitted on at most 24 points (4 h → 10-min means, 24 h → hourly means): 276 pairs."""
_CHUNK = 4096
_TOLERANCE_US = 1000
"""Samples up to 1 ms after a grid instant belong to it (float rounding of simulator time)."""


def to_us(instant: datetime) -> int:
    return (ensure_utc(instant) - _EPOCH) // timedelta(microseconds=1)


def from_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(value))


# --------------------------------------------------------------------------- history


@dataclass(frozen=True, slots=True)
class StopRecord:
    """A completed stop of a unit (state interval ``DOWN_*``)."""

    start: datetime
    end: datetime
    state: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class Stops:
    """Stops of one unit as arrays sorted by end time, classified for the features."""

    start_us: I64
    end_us: I64
    pm: B
    repair: B
    """Unplanned stop of at least ``microstop_threshold_s``."""
    wear_repair: B
    """``repair`` with a wear reason of the unit's type (what the label predicts)."""
    microstop: B


def classify_stops(
    start_us: Sequence[int] | I64,
    end_us: Sequence[int] | I64,
    states: Sequence[str],
    reasons: Sequence[str | None],
    *,
    spec: PdmSpec,
    equipment_type: str,
) -> Stops:
    start = np.asarray(start_us, dtype=np.int64)
    end = np.asarray(end_us, dtype=np.int64)
    if not (len(start) == len(end) == len(states) == len(reasons)):
        raise ValueError("stop arrays must have the same length")
    wear = spec.types[equipment_type].wear_reasons
    planned = np.array(
        [
            s == EquipmentState.DOWN_PLANNED or (r is not None and r in spec.planned_reasons)
            for s, r in zip(states, reasons, strict=True)
        ],
        dtype=np.bool_,
    )
    unplanned = np.array([s == EquipmentState.DOWN_UNPLANNED for s in states], dtype=np.bool_)
    unplanned &= ~planned
    long_enough = (end - start) >= round(spec.min_failure_s * US)
    repair = unplanned & long_enough
    is_wear = np.array([r is not None and r in wear for r in reasons], dtype=np.bool_)
    order = np.argsort(end, kind="stable")
    return Stops(
        start_us=start[order],
        end_us=end[order],
        pm=planned[order],
        repair=repair[order],
        wear_repair=(repair & is_wear)[order],
        microstop=(unplanned & ~long_enough)[order],
    )


@dataclass(frozen=True, slots=True)
class UnitHistory:
    """What is known about one unit: telemetry on the grid, stops and its line's exits."""

    equipment: str
    type: str
    grid0_us: int
    """Time of slot 0 (a multiple of the grid step since the epoch)."""
    values: Mapping[str, F64]
    """Signal → value per grid slot from ``grid0_us`` (NaN = no sample)."""
    stops: Stops
    exits_us: I64
    """Sorted first exits (PQ) of the unit's line — cycles since maintenance."""


def to_grid(
    ts_us: Sequence[int] | I64,
    values: Sequence[float] | F64,
    *,
    grid0_us: int,
    n_slots: int,
    grid_s: int,
) -> F64:
    """Sample-and-hold onto the grid: slot k = the latest sample in ``(g_k − step, g_k]``."""
    ts = np.asarray(ts_us, dtype=np.int64)
    vals = np.asarray(values, dtype=np.float64)
    out = np.full(n_slots, np.nan)
    if len(ts) == 0 or n_slots <= 0:
        return out
    order = np.argsort(ts, kind="stable")
    ts, vals = ts[order], vals[order]
    step = grid_s * US
    slots = grid0_us + np.arange(n_slots, dtype=np.int64) * step
    idx = np.searchsorted(ts, slots + _TOLERANCE_US, side="right") - 1
    ok = idx >= 0
    safe = np.where(ok, idx, 0)
    ok &= ts[safe] > slots - step + _TOLERANCE_US
    out[ok] = vals[safe[ok]]
    return out


def history_from_records(
    equipment: str,
    *,
    spec: PdmSpec,
    until: datetime,
    telemetry: Mapping[str, tuple[Sequence[datetime], Sequence[float]]],
    stops: Sequence[StopRecord],
    exits: Sequence[datetime],
) -> UnitHistory:
    """Online history for :func:`features_at` (engine, M7b): plain records up to ``until``.

    ``telemetry`` needs the last 24 h of each signal (any period, e.g. 60 s live samples); ``stops``
    — completed stops at least since the last planned maintenance; ``exits`` — the line's first
    exits since then. Extra (older or later) records are harmless.
    """
    return history_from_arrays(
        equipment,
        spec=spec,
        until_us=to_us(until),
        telemetry={
            code: (np.array([to_us(t) for t in ts], dtype=np.int64), np.asarray(vals, np.float64))
            for code, (ts, vals) in telemetry.items()
        },
        stop_start_us=[to_us(s.start) for s in stops],
        stop_end_us=[to_us(s.end) for s in stops],
        stop_states=[s.state for s in stops],
        stop_reasons=[s.reason for s in stops],
        exits_us=np.array([to_us(e) for e in exits], dtype=np.int64),
    )


def history_from_arrays(
    equipment: str,
    *,
    spec: PdmSpec,
    until_us: int,
    telemetry: Mapping[str, tuple[I64, F64]],
    stop_start_us: Sequence[int] | I64,
    stop_end_us: Sequence[int] | I64,
    stop_states: Sequence[str],
    stop_reasons: Sequence[str | None],
    exits_us: Sequence[int] | I64,
) -> UnitHistory:
    """:func:`history_from_records` on microsecond arrays (the engine keeps its cache this way)."""
    equipment_type, _line = spec.equipment[equipment]
    step = spec.grid_s * US
    t_us = floor_to_grid(until_us, spec.grid_s)
    grid0 = t_us - spec.max_window_h * _HOUR_US
    n_slots = (t_us - grid0) // step
    values: dict[str, F64] = {}
    for sig in spec.types[equipment_type].signals:
        ts, vals = telemetry.get(sig.code, (np.empty(0, np.int64), np.empty(0, np.float64)))
        values[sig.code] = to_grid(ts, vals, grid0_us=grid0, n_slots=n_slots, grid_s=spec.grid_s)
    classified = classify_stops(
        stop_start_us,
        stop_end_us,
        stop_states,
        stop_reasons,
        spec=spec,
        equipment_type=equipment_type,
    )
    return UnitHistory(
        equipment,
        equipment_type,
        grid0,
        values,
        classified,
        np.sort(np.asarray(exits_us, np.int64)),
    )


def floor_to_grid(t_us: int, grid_s: int) -> int:
    step = grid_s * US
    return (t_us // step) * step


# --------------------------------------------------------------------------- calendar


@dataclass(frozen=True, slots=True)
class ShiftTimeline:
    """Working shifts as arrays: current shift and scheduled working time ahead (vectorised)."""

    start_us: I64
    end_us: I64
    code_idx: I64
    cum_us: I64
    """Working microseconds before each shift start."""

    @classmethod
    def build(
        cls, calendar: PlantCalendar, shift_codes: Sequence[str], first_us: int, last_us: int
    ) -> ShiftTimeline:
        first_day = calendar.local_date(from_us(first_us)) - timedelta(days=1)
        last_day = calendar.local_date(from_us(last_us)) + timedelta(days=1)
        shifts = [s for s in calendar.materialize(first_day, last_day) if s.working]
        shifts.sort(key=lambda s: s.start)
        index = {code: i for i, code in enumerate(shift_codes)}
        start = np.array([to_us(s.start) for s in shifts], dtype=np.int64)
        end = np.array([to_us(s.end) for s in shifts], dtype=np.int64)
        dur = end - start
        cum = np.concatenate([[0], np.cumsum(dur)[:-1]]).astype(np.int64) if len(dur) else dur
        codes = np.array([index[s.code] for s in shifts], dtype=np.int64)
        return cls(start, end, codes, cum)

    def _working_before(self, t: I64) -> I64:
        idx = np.searchsorted(self.start_us, t, side="right") - 1
        safe = np.maximum(idx, 0)
        if len(self.start_us) == 0:
            return np.zeros(len(t), dtype=np.int64)
        within = np.clip(t - self.start_us[safe], 0, self.end_us[safe] - self.start_us[safe])
        return np.where(idx >= 0, self.cum_us[safe] + within, 0)

    def working_us(self, t_from: I64, t_to: I64) -> I64:
        return self._working_before(t_to) - self._working_before(t_from)

    def shift_index(self, t: I64, n_codes: int) -> I64:
        """Index of the working shift containing ``t``; ``n_codes`` outside working shifts."""
        if len(self.start_us) == 0:
            return np.full(len(t), n_codes, dtype=np.int64)
        idx = np.searchsorted(self.start_us, t, side="right") - 1
        safe = np.maximum(idx, 0)
        inside = (idx >= 0) & (t < self.end_us[safe])
        return np.where(inside, self.code_idx[safe], n_codes)


# --------------------------------------------------------------------------- window statistics


def _gather(values: F64, slots: I64, n: int) -> F64:
    """Rows = windows ``[s − n, s)`` of ``values`` for each slot index ``s`` (NaN outside)."""
    pad_front = n
    pad_back = max(0, int(slots.max(initial=0)) - len(values))
    padded = np.concatenate([np.full(pad_front, np.nan), values, np.full(pad_back, np.nan)])
    starts = slots - n + pad_front
    rows: F64 = padded[starts[:, None] + np.arange(n)]
    return rows


def _median_rows(a: F64) -> F64:
    """Row medians ignoring NaN (NaN for rows without values)."""
    out = np.full(a.shape[0], np.nan)
    nan_rows = np.isnan(a).any(axis=1)
    clean = ~nan_rows
    if clean.any():
        out[clean] = np.median(a[clean], axis=1)
    if nan_rows.any():
        sub = np.sort(a[nan_rows], axis=1)  # NaN sorts last
        k = (~np.isnan(sub)).sum(axis=1)
        has = k > 0
        lo = np.maximum((k - 1) // 2, 0)
        hi = np.maximum(k // 2, 0)
        rows = np.arange(sub.shape[0])
        med = (sub[rows, lo] + sub[rows, hi]) / 2.0
        out_sub = np.where(has, med, np.nan)
        out[nan_rows] = out_sub
    return out


def theil_sen_rows(win: F64, step_h: float, max_points: int = SLOPE_POINTS) -> F64:
    """Theil–Sen slope per hour for every row of equally spaced samples (NaN-aware).

    Rows longer than ``max_points`` are averaged into ``max_points`` buckets first.
    """
    rows, n = win.shape
    factor = max(1, math.ceil(n / max_points))
    if factor > 1:
        m = math.ceil(n / factor)
        pad = m * factor - n
        blocks = np.concatenate([np.full((rows, pad), np.nan), win], axis=1).reshape(
            rows, m, factor
        )
        valid = np.isfinite(blocks)
        cnt = valid.sum(axis=2)
        sums = np.where(valid, blocks, 0.0).sum(axis=2)
        y = np.divide(sums, cnt, out=np.full((rows, m), np.nan), where=cnt > 0)
        x = (np.arange(m) * factor - pad + (factor - 1) / 2.0) * step_h
    else:
        y = win
        x = np.arange(n) * step_h
    i, j = np.triu_indices(len(x), k=1)
    slopes = (y[:, j] - y[:, i]) / (x[j] - x[i])
    enough = np.isfinite(y).sum(axis=1) >= 2
    return np.where(enough, _median_rows(slopes), np.nan)


def window_stats(win: F64, step_h: float, min_count: int) -> tuple[F64, F64, F64, F64]:
    """(mean, std, max, slope per hour) per row; NaN where fewer than ``min_count`` samples."""
    valid = np.isfinite(win)
    cnt = valid.sum(axis=1)
    ok = cnt >= max(2, min_count)
    nan = np.full(win.shape[0], np.nan)
    sums = np.where(valid, win, 0.0).sum(axis=1)
    mean = np.divide(sums, cnt, out=nan.copy(), where=ok)
    dev = np.where(valid, win - mean[:, None], 0.0)
    var = np.divide((dev * dev).sum(axis=1), cnt, out=nan.copy(), where=ok)
    std = np.sqrt(var)
    mx = np.where(ok, np.where(valid, win, -np.inf).max(axis=1, initial=-np.inf), np.nan)
    slope = np.where(ok, theil_sen_rows(win, step_h), np.nan)
    return mean, std, mx, slope


# --------------------------------------------------------------------------- features


def _last_before(times: I64, t: I64) -> tuple[I64, B]:
    idx = np.searchsorted(times, t, side="left") - 1
    found = idx >= 0
    return times[np.maximum(idx, 0)] if len(times) else np.zeros(len(t), dtype=np.int64), found


def compute_features(
    history: UnitHistory,
    at_us: Sequence[int] | I64,
    *,
    spec: PdmSpec,
    calendar: PlantCalendar,
    timeline: ShiftTimeline | None = None,
) -> dict[str, F64]:
    """Features of ``history`` at each time in ``at_us`` (multiples of the grid step).

    Columns follow :meth:`PdmSpec.feature_names` for the unit's type.
    """
    at = np.asarray(at_us, dtype=np.int64)
    step = spec.grid_s * US
    rel = at - history.grid0_us
    if np.any(rel % step):
        raise ValueError("evaluation times must lie on the feature grid")
    slots = rel // step
    ts = spec.types[history.type]
    names = spec.feature_names(history.type)
    out: dict[str, F64] = {name: np.full(len(at), np.nan) for name in names}
    step_h = spec.grid_s / 3600.0

    for sig in ts.signals:
        values = history.values.get(sig.code)
        if values is None:
            continue
        for w in spec.windows_h:
            n = w * 3600 // spec.grid_s
            for lo in range(0, len(at), _CHUNK):
                part = slice(lo, lo + _CHUNK)
                win = _gather(values, slots[part], n)
                mean, std, mx, slope = window_stats(win, step_h, min_count=(n + 1) // 2)
                out[f"{sig.code}_mean_{w}h"][part] = mean
                out[f"{sig.code}_std_{w}h"][part] = std
                out[f"{sig.code}_max_{w}h"][part] = mx
                out[f"{sig.code}_slope_{w}h"][part] = slope * w
        if sig.is_rate:
            out[f"{sig.code}_count_{COUNT_WINDOW_H}h"] = (
                out[f"{sig.code}_mean_{COUNT_WINDOW_H}h"] * COUNT_WINDOW_H
            )

    stops = history.stops
    micro_end = stops.end_us[stops.microstop]
    window_us = MICROSTOP_WINDOW_H * _HOUR_US
    out[f"microstops_{MICROSTOP_WINDOW_H}h"] = (
        np.searchsorted(micro_end, at, side="left")
        - np.searchsorted(micro_end, at - window_us, side="left")
    ).astype(np.float64)

    pm_end, has_pm = _last_before(stops.end_us[stops.pm], at)
    out["hours_since_pm"] = np.where(has_pm, (at - pm_end) / _HOUR_US, np.nan)
    rep_end, has_rep = _last_before(stops.end_us[stops.repair], at)
    out["hours_since_repair"] = np.where(has_rep, (at - rep_end) / _HOUR_US, np.nan)
    wear_end, has_wear = _last_before(stops.end_us[stops.wear_repair], at)
    out["hours_since_wear_repair"] = np.where(has_wear, (at - wear_end) / _HOUR_US, np.nan)
    exits = history.exits_us
    cycles = np.searchsorted(exits, at, side="left") - np.searchsorted(exits, pm_end, side="left")
    out["cycles_since_pm"] = np.where(has_pm, cycles.astype(np.float64), np.nan)

    horizon_us = round(spec.horizon_h * _HOUR_US)
    if timeline is None and len(at):
        first = int(at.min())
        last = int(at.max()) + horizon_us
        timeline = ShiftTimeline.build(calendar, spec.shift_codes, first, last)
    if timeline is not None:
        out["shift"] = timeline.shift_index(at, len(spec.shift_codes)).astype(np.float64)
        out["work_hours_ahead"] = timeline.working_us(at, at + horizon_us) / _HOUR_US
    assert_no_oracle(tuple(out))
    return out


def features_at(
    history: UnitHistory, now: datetime, *, spec: PdmSpec, calendar: PlantCalendar
) -> dict[str, float]:
    """Online entry point: the feature row of one unit at ``now`` floored to the grid."""
    t_us = floor_to_grid(to_us(now), spec.grid_s)
    cols = compute_features(history, [t_us], spec=spec, calendar=calendar)
    return {name: float(col[0]) for name, col in cols.items()}
