"""PdM serving of the engine (SPEC §11.1–11.2): p_failure(8 h), health index, top factors, and the
time to a signal limit (AL-M2), every ``rules.yaml: engine.pdm_tick_min`` plant minutes.

* :class:`PdmCache` + :class:`PdmFeed` — the data. The engine does not parse the 96% telemetry
  share of the event stream: the collector writes ``telemetry`` straight to the database, and the
  feed reads it (and the unit's stops and the line's exits) *incrementally* — one cold read of five
  days at start/reset, then only the new rows with a small overlap — into numpy arrays.
* :class:`PdmServing` — pure and synchronous (run it in a thread): per unit
  ``qost_ml.features.history_from_arrays`` → ``features_at`` → ``Predictor.predict`` and, for
  signals with ``limit_hi``, :func:`twin_core.limits.unit_limit_advice` on the working-time axis.
  The simulator's hidden wear state is never read (rule 5): features are built from the telemetry, the
  stops and the exits only.

``qost_ml`` (LightGBM, SHAP) is imported when the serving is created, not when the engine starts
without PdM (``ENGINE_PDM=false``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import asyncpg
import numpy as np
import numpy.typing as npt
import structlog

from qost_engine.core.pdm import LimitItem, PdmTick, UnitPrediction
from twin_core.calendar import WorkingTime
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.events import FIRST_EXIT_RESULTS
from twin_core.limits import unit_limit_advice

if TYPE_CHECKING:
    from qost_ml.spec import PdmSpec

log = structlog.get_logger("qost_engine.pdm")

US = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HISTORY_DAYS = 5
"""Telemetry kept per signal: 24 h for the features, six working hours of AL-M2 across a weekend."""
OVERLAP = timedelta(minutes=10)
STOPS_OVERLAP = timedelta(hours=1)
I64 = npt.NDArray[np.int64]
F64 = npt.NDArray[np.float64]


def to_us(instant: datetime) -> int:
    return (ensure_utc(instant) - _EPOCH) // timedelta(microseconds=1)


def from_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(value))


@dataclass
class PdmCache:
    """In-memory history of the PdM units (see the module docstring)."""

    series: dict[tuple[str, str], tuple[I64, F64]] = field(default_factory=dict)
    stops: dict[str, dict[int, tuple[int, str, str | None]]] = field(default_factory=dict)
    """Unit -> start (µs) -> (end µs, ``DOWN_PLANNED`` | ``DOWN_UNPLANNED``, reason)."""
    exits: dict[str, I64] = field(default_factory=dict)
    tel_until: int | None = None
    stops_until: int | None = None
    exits_until: int | None = None

    def clear(self) -> None:
        self.series.clear()
        self.stops.clear()
        self.exits.clear()
        self.tel_until = self.stops_until = self.exits_until = None


def _merge(old: tuple[I64, F64] | None, ts: I64, vals: F64, lo_us: int) -> tuple[I64, F64]:
    """Replace everything from the first new timestamp on and trim to ``lo_us``."""
    if old is None or len(old[0]) == 0:
        keep_ts, keep_v = ts, vals
    else:
        cut = int(np.searchsorted(old[0], ts[0], side="left")) if len(ts) else len(old[0])
        keep_ts = np.concatenate([old[0][:cut], ts])
        keep_v = np.concatenate([old[1][:cut], vals])
    start = int(np.searchsorted(keep_ts, lo_us, side="left"))
    return keep_ts[start:], keep_v[start:]


class PdmFeed:
    """Incremental reader of telemetry, stops and exits from the database."""

    def __init__(
        self,
        pool: Callable[[], Awaitable[asyncpg.Pool]],
        cfg: TwinConfig,
        units: dict[str, str],
        lines: dict[str, str],
        signals: dict[str, tuple[str, ...]],
    ) -> None:
        self._pool = pool
        self.cfg = cfg
        self.units = units
        """Unit -> equipment type of the units that need telemetry (PdM and limits)."""
        self.lines = lines
        """Unit -> line, for the exits of PdM units."""
        self.signals = signals
        """Type -> signal codes to read."""
        self.stops_days = cfg.rules.engine.pdm_stops_days

    async def refresh(self, cache: PdmCache, now: datetime) -> None:
        pool = await self._pool()
        now_us = to_us(now)
        async with pool.acquire() as conn:
            await self._telemetry(conn, cache, now, now_us)
            await self._stops(conn, cache, now, now_us)
            await self._exits(conn, cache, now, now_us)

    async def _telemetry(
        self, conn: asyncpg.Connection, cache: PdmCache, now: datetime, now_us: int
    ) -> None:
        horizon = now - timedelta(days=HISTORY_DAYS)
        lo = (
            horizon if cache.tel_until is None else max(horizon, from_us(cache.tel_until) - OVERLAP)
        )
        codes = sorted({s for t in self.units.values() for s in self.signals.get(t, ())})
        rows = await conn.fetch(
            "SELECT equipment, signal, ts, value FROM telemetry WHERE equipment = ANY($1) "
            "AND signal = ANY($2) AND ts > $3 AND ts <= $4 AND quality <> 'bad' ORDER BY ts",
            sorted(self.units),
            codes,
            lo,
            now,
        )
        grouped: dict[tuple[str, str], tuple[list[int], list[float]]] = {}
        for r in rows:
            if r["signal"] not in self.signals.get(self.units[r["equipment"]], ()):
                continue
            ts_list, v_list = grouped.setdefault((r["equipment"], r["signal"]), ([], []))
            ts_list.append(to_us(r["ts"]))
            v_list.append(float(r["value"]))
        lo_us = to_us(horizon)
        for key, (ts_list, v_list) in grouped.items():
            cache.series[key] = _merge(
                cache.series.get(key),
                np.array(ts_list, dtype=np.int64),
                np.array(v_list, dtype=np.float64),
                lo_us,
            )
        cache.tel_until = now_us

    async def _stops(
        self, conn: asyncpg.Connection, cache: PdmCache, now: datetime, now_us: int
    ) -> None:
        horizon = now - timedelta(days=self.stops_days)
        lo = horizon if cache.stops_until is None else from_us(cache.stops_until) - STOPS_OVERLAP
        rows = await conn.fetch(
            "SELECT entity, start_ts, end_ts, planned, reason_code FROM downtime "
            "WHERE entity = ANY($1) AND import_id IS NULL AND start_ts IS NOT NULL "
            "AND end_ts IS NOT NULL AND end_ts > $2 AND end_ts <= $3 ORDER BY end_ts",
            sorted(self.units),
            lo,
            now,
        )
        for r in rows:
            cache.stops.setdefault(r["entity"], {})[to_us(r["start_ts"])] = (
                to_us(r["end_ts"]),
                "DOWN_PLANNED" if r["planned"] else "DOWN_UNPLANNED",
                r["reason_code"],
            )
        cache.stops_until = now_us

    async def _exits(
        self, conn: asyncpg.Connection, cache: PdmCache, now: datetime, now_us: int
    ) -> None:
        horizon = now - timedelta(days=self.stops_days)
        lo = horizon if cache.exits_until is None else from_us(cache.exits_until) - OVERLAP
        lines = sorted(set(self.lines.values()))
        if not lines:
            return
        rows = await conn.fetch(
            "SELECT line, ts FROM unit_event WHERE line = ANY($1) AND ts > $2 AND ts <= $3 "
            "AND result = ANY($4) ORDER BY ts",
            lines,
            lo,
            now,
            sorted(FIRST_EXIT_RESULTS),
        )
        grouped: dict[str, list[int]] = {}
        for r in rows:
            grouped.setdefault(r["line"], []).append(to_us(r["ts"]))
        lo_us = to_us(horizon)
        for line, ts_list in grouped.items():
            new = np.array(ts_list, dtype=np.int64)
            old = cache.exits.get(line)
            if old is not None and len(old) and len(new):
                old = old[: int(np.searchsorted(old, new[0], side="left"))]
            merged = new if old is None else np.concatenate([old, new])
            cache.exits[line] = merged[int(np.searchsorted(merged, lo_us, side="left")) :]
        cache.exits_until = now_us


class Predicting(Protocol):
    """What the serving needs of :class:`qost_ml.predictor.Predictor` (tests pass a fake)."""

    spec: PdmSpec

    def predict(self, equipment_type: str, features: Any, *, explain: bool = ...) -> Any: ...


class PdmServing:
    """Predictions and limit advice for one tick (see the module docstring)."""

    def __init__(self, cfg: TwinConfig, predictor: Predicting) -> None:
        self.cfg = cfg
        self.predictor = predictor
        self.spec = predictor.spec
        limited = {
            code: eq.type
            for code, eq in cfg.equipment.items()
            if any(s.limit_hi is not None for s in cfg.equipment_types[eq.type].signals)
        }
        pdm_units = {code: t for code, (t, _line) in self.spec.equipment.items()}
        self.units = {**limited, **pdm_units}
        self.signals: dict[str, tuple[str, ...]] = {}
        for t in set(self.units.values()):
            self.signals[t] = tuple(s.code for s in cfg.equipment_types[t].signals)
        self.lines = {code: line for code, (_t, line) in self.spec.equipment.items()}
        self.limited = limited

    @classmethod
    def load(cls, cfg: TwinConfig) -> PdmServing:
        from qost_ml.predictor import Predictor

        return cls(cfg, Predictor.load(cfg))

    def feed(self, pool: Callable[[], Awaitable[asyncpg.Pool]]) -> PdmFeed:
        return PdmFeed(pool, self.cfg, self.units, self.lines, self.signals)

    # ------------------------------------------------------------------ evaluation

    def evaluate(self, cache: PdmCache, now: datetime, thresholds: Any | None = None) -> PdmTick:
        thr = thresholds or self.cfg.rules.thresholds
        units: list[UnitPrediction] = []
        for code in self.spec.equipment:
            try:
                units.append(self._predict(cache, code, now))
            except Exception as exc:  # one broken unit must not stop the others
                log.warning("pdm_unit_failed", equipment=code, error=str(exc)[:200])
        return PdmTick(
            ts=now,
            units=tuple(units),
            limits=tuple(self._limits(cache, now, thr)),
            lookahead_h=float(thr.telemetry_limit_lookahead_h),
        )

    def _predict(self, cache: PdmCache, code: str, now: datetime) -> UnitPrediction:
        from qost_ml.features import features_at, history_from_arrays

        equipment_type, line = self.spec.equipment[code]
        empty: tuple[I64, F64] = (np.empty(0, np.int64), np.empty(0, np.float64))
        telemetry = {
            s.code: cache.series.get((code, s.code), empty)
            for s in self.spec.types[equipment_type].signals
        }
        items = sorted(cache.stops.get(code, {}).items())
        history = history_from_arrays(
            code,
            spec=self.spec,
            until_us=to_us(now),
            telemetry=telemetry,
            stop_start_us=[k for k, _ in items],
            stop_end_us=[v[0] for _, v in items],
            stop_states=[v[1] for _, v in items],
            stop_reasons=[v[2] for _, v in items],
            exits_us=cache.exits.get(line, np.empty(0, np.int64)),
        )
        row = features_at(history, now, spec=self.spec, calendar=self.cfg.calendar)
        pred = self.predictor.predict(equipment_type, row)
        return UnitPrediction(
            equipment=code,
            p_failure=float(pred.p_failure),
            health_index=float(pred.health_index),
            horizon_h=float(pred.horizon_h),
            model_version=str(pred.model_version or "rules"),
            source=str(pred.source),
            factors=tuple(f.as_dict() for f in pred.top_factors),
        )

    def _limits(self, cache: PdmCache, now: datetime, thr: Any) -> list[LimitItem]:
        working = WorkingTime(self.cfg.calendar, now)
        lo_us = to_us(now - timedelta(days=HISTORY_DAYS))
        threshold_us = int(thr.microstop_threshold_s * US)
        out: list[LimitItem] = []
        for code, equipment_type in self.limited.items():
            series: dict[str, tuple[list[datetime], list[float]]] = {}
            for sig in self.cfg.equipment_types[equipment_type].signals:
                if sig.limit_hi is None or (code, sig.code) not in cache.series:
                    continue
                ts, vals = cache.series[(code, sig.code)]
                start = int(np.searchsorted(ts, lo_us))
                series[sig.code] = ([from_us(int(t)) for t in ts[start:]], vals[start:].tolist())
            if not series:
                continue
            ended = [
                end
                for start, (end, _state, _reason) in cache.stops.get(code, {}).items()
                if end - start >= threshold_us and end <= to_us(now)
            ]
            since = from_us(max(ended)) if ended else None
            advice = unit_limit_advice(
                self.cfg,
                code,
                series,
                now=now,
                since=since,
                working=working,
                lookahead_h=float(thr.telemetry_limit_lookahead_h),
            )
            names = {s.code: s for s in self.cfg.equipment_types[equipment_type].signals}
            for signal, adv in advice.items():
                f = adv.forecast
                out.append(
                    LimitItem(
                        equipment=code,
                        signal=signal,
                        signal_name_ru=names[signal].name_ru,
                        unit=names[signal].unit,
                        limit=f.limit,
                        level_now=f.level_now,
                        slope_per_h=f.slope_per_h,
                        hours_to_limit=f.hours_to_limit,
                        limit_at=f.limit_at,
                        window=adv.window,
                        saving_min=adv.saving.minutes,
                        saving_cars=adv.saving.cars,
                        alert=adv.alert,
                        n_points=f.n_points,
                    )
                )
        return out
