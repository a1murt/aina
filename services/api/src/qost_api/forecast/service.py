"""Forecast service of the API (SPEC §10, §12.2): calibration snapshots, forecasts with
comparison, levers and economic effect.

The NumPy work runs in worker threads behind a capacity limiter (``forecast.max_concurrent``)
so the event loop stays responsive. Caches (per process):

* calibration snapshot by (window end, window days, config hash) — the window changes once a
  day, and the database holds at most one snapshot per key;
* baseline paths by (month, calibration, state digest, seed, runs) — a what-if request then
  computes only the scenario;
* levers by (month, calibration, state digest, seed, runs, plant hour).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import anyio
from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field

from qost_api.auth import Principal
from qost_api.forecast.data import ForecastBackend, RunRecord, StoredRun, StoredSnapshot
from twin_core.clock import Clock, ensure_utc
from twin_core.config import TwinConfig
from twin_core.config.plant import Month
from twin_core.forecast.calibration import (
    calibrate,
    calibration_window,
    config_hash,
    month_bounds,
    month_of,
)
from twin_core.forecast.effect import EffectResult
from twin_core.forecast.levers import LeversResult
from twin_core.forecast.model import Paths
from twin_core.forecast.overrides import OverrideIssue, Overrides, check_overrides
from twin_core.forecast.params import CalibrationParams, PlantState
from twin_core.forecast.result import ForecastResult
from twin_core.forecast.runner import ForecastContext, default_seed, forecast, levers
from twin_core.forecast.runner import effect as run_effect

_BASE_CACHE = 16
_LEVERS_CACHE = 8


class ForecastRequest(BaseModel):
    """``POST /api/v1/forecast`` (FR-FC-01/02)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fast", "des"] = "fast"
    month: Month | None = None
    """``YYYY-MM``; default: the current plant month."""
    overrides: Overrides = Field(default_factory=Overrides)
    n_runs: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=0, le=(1 << 63) - 1)
    compare: bool = True
    """With overrides: also run the baseline with the same seed and return the deltas."""


class EffectRequest(BaseModel):
    """``POST /api/v1/effect`` (SPEC §10.5)."""

    model_config = ConfigDict(extra="forbid")

    month: Month | None = None
    scenario: Overrides | None = None
    """Default: ``business.yaml: improvement_defaults`` (scenario «с системой»)."""
    assumptions: dict[str, Any] | None = None
    """Values of ``business.yaml: params`` for this calculation only (UI-edited assumptions)."""
    n_runs: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=0, le=(1 << 63) - 1)


class ForecastRunView(BaseModel):
    """A stored forecast run."""

    id: int
    created_ts: datetime
    mode: str
    month: str
    status: str
    n_runs: int
    seed: int
    duration_ms: int | None
    overrides: dict[str, Any]
    result: ForecastResult | None


class CalibrationView(BaseModel):
    id: int
    ts: datetime
    window_days: int
    params: CalibrationParams


class ForecastError(Exception):
    """A request the service cannot serve; mapped to RFC 7807 by the routes."""

    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail
        self.errors = errors


@dataclass(frozen=True, slots=True)
class _Prepared:
    ctx: ForecastContext
    calibration: StoredSnapshot


class ForecastService:
    def __init__(self, cfg: TwinConfig, clock: Clock, backend: ForecastBackend) -> None:
        self.cfg = cfg
        self.clock = clock
        self.backend = backend
        fc = cfg.simulation.forecast
        self._limiter = anyio.CapacityLimiter(fc.max_concurrent)
        self._snapshots: dict[tuple[str, int, str], StoredSnapshot] = {}
        self._base: OrderedDict[tuple[Any, ...], Paths] = OrderedDict()
        self._levers: OrderedDict[tuple[Any, ...], LeversResult] = OrderedDict()

    # ------------------------------------------------------------------ helpers

    def _runs(self, requested: int | None, *, levers: bool = False) -> int:
        counts = self.cfg.simulation.forecast.n_runs
        n = requested if requested is not None else (counts.levers if levers else counts.default)
        if not counts.min <= n <= counts.max:
            raise ForecastError(
                422,
                "validation",
                "Request validation failed",
                f"n_runs must be within {counts.min}..{counts.max}",
                [
                    {
                        "loc": ["body", "n_runs"],
                        "msg": f"must be within {counts.min}..{counts.max}",
                        "type": "value_error",
                    }
                ],
            )
        return n

    def _month(self, month: str | None, now: datetime, *, allow_past: bool = False) -> str:
        current = month_of(self.cfg, now)
        chosen = month or current
        if not allow_past and chosen < current:
            raise ForecastError(
                422,
                "forecast-month",
                "Month already closed",
                f"{chosen} is before the current plant month {current}",
            )
        return chosen

    async def calibration(self, principal: Principal) -> StoredSnapshot:
        """Calibration for the window ending today (get-or-create, at most one per key)."""
        now = self.clock.now()
        w0, w1, days = calibration_window(self.cfg, now)
        h = config_hash(self.cfg)
        key = (ensure_utc(w1).isoformat(), days, h)
        cached = self._snapshots.get(key)
        if cached is not None:
            return cached
        found = await self.backend.find_snapshot(window_to=w1, window_days=days, config_hash=h)
        if found is None:
            inputs = await self.backend.calibration_inputs(
                self.cfg, window_from=w0, window_to=w1, working_days=days, as_of=now
            )
            params = calibrate(self.cfg, inputs, computed_at=now)
            found = await self.backend.save_snapshot(params, ts=now, principal=principal)
        self._snapshots = {key: found}
        return found

    async def _prepare(self, month: str, principal: Principal) -> _Prepared:
        now = self.clock.now()
        snapshot = await self.calibration(principal)
        state = await self.backend.plant_state(self.cfg, as_of=now)
        m0, _ = month_bounds(self.cfg, month)
        if m0 > ensure_utc(now):
            state = _future_state(state, m0)
        targets = await self.backend.targets(self.cfg, month)
        ctx = ForecastContext(self.cfg, snapshot.params, state, targets, month, snapshot.id)
        return _Prepared(ctx, snapshot)

    def _check(self, overrides: Overrides, ctx: ForecastContext, prefix: tuple[str, ...]) -> None:
        m0, m1 = ctx.bounds
        issues: list[OverrideIssue] = check_overrides(
            self.cfg, overrides, month_start=m0, month_end=m1, as_of=ctx.state.as_of
        )
        if issues:
            raise ForecastError(
                422,
                "validation",
                "Request validation failed",
                "; ".join(i.msg for i in issues),
                [i.as_problem(prefix) for i in issues],
            )

    # ------------------------------------------------------------------ use cases

    async def forecast(self, request: ForecastRequest, principal: Principal) -> ForecastRunView:
        if request.mode == "des":
            raise ForecastError(
                501,
                "not-implemented",
                "Not implemented",
                "mode 'des' (detailed DES forecast, SPEC §10.3) is P1 and planned for stage M9; "
                "use mode 'fast'",
            )
        now = self.clock.now()
        month = self._month(request.month, now)
        n_runs = self._runs(request.n_runs)
        seed = request.seed if request.seed is not None else default_seed(self.cfg)
        prepared = await self._prepare(month, principal)
        ctx = prepared.ctx
        self._check(request.overrides, ctx, ("body", "overrides"))
        base_key = (month, prepared.calibration.id, ctx.state.digest(), seed, n_runs)
        cached_base = self._base.get(base_key)
        overrides = request.overrides
        compare = request.compare

        def work() -> tuple[ForecastResult, Paths, Paths | None, int]:
            started = time.perf_counter()
            result, paths, base = forecast(
                ctx, overrides, n_runs=n_runs, seed=seed, base=cached_base, compare=compare
            )
            return result, paths, base, round((time.perf_counter() - started) * 1000)

        result, paths, base, duration_ms = await to_thread.run_sync(work, limiter=self._limiter)
        baseline = paths if overrides.is_empty() else base
        if baseline is not None:
            self._remember(self._base, base_key, baseline, _BASE_CACHE)
        result = result.model_copy(update={"duration_ms": duration_ms})
        record = RunRecord(
            created_ts=now,
            mode=request.mode,
            month=month,
            overrides=overrides.normalized(),
            n_runs=n_runs,
            seed=seed,
            status="done",
            progress=1.0,
            result=result.model_dump(mode="json"),
            duration_ms=duration_ms,
        )
        run_id = await self.backend.save_run(record, principal=principal)
        return _view(run_id, record, result)

    async def outlook(self, month: str | None, principal: Principal) -> ForecastResult:
        """Baseline forecast of a month without storing a run (shift reports, M8).

        Same calibration, state, seed and run count as ``POST /forecast``; the baseline paths go
        to the cache, so a later what-if of the director computes only its scenario.
        """
        now = self.clock.now()
        chosen = self._month(month, now)
        n_runs = self._runs(None)
        seed = default_seed(self.cfg)
        prepared = await self._prepare(chosen, principal)
        ctx = prepared.ctx

        def work() -> tuple[ForecastResult, Paths]:
            result, paths, _ = forecast(ctx, None, n_runs=n_runs, seed=seed, compare=False)
            return result, paths

        result, paths = await to_thread.run_sync(work, limiter=self._limiter)
        key = (chosen, prepared.calibration.id, ctx.state.digest(), seed, n_runs)
        self._remember(self._base, key, paths, _BASE_CACHE)
        return result

    async def get(self, run_id: int) -> ForecastRunView | None:
        stored: StoredRun | None = await self.backend.get_run(run_id)
        if stored is None:
            return None
        rec = stored.record
        result = ForecastResult.model_validate(rec.result) if rec.result is not None else None
        return _view(stored.id, rec, result)

    async def levers(
        self, month: str | None, principal: Principal, *, n_runs: int | None = None
    ) -> LeversResult:
        now = self.clock.now()
        chosen = self._month(month, now)
        runs = self._runs(n_runs, levers=True)
        seed = default_seed(self.cfg)
        prepared = await self._prepare(chosen, principal)
        ctx = prepared.ctx
        hour = ensure_utc(now).replace(minute=0, second=0, microsecond=0).isoformat()
        key = (chosen, prepared.calibration.id, ctx.state.digest(), seed, runs, hour)
        cached = self._levers.get(key)
        if cached is not None:
            return cached

        def work() -> LeversResult:
            started = time.perf_counter()
            out = levers(ctx, n_runs=runs, seed=seed)
            return out.model_copy(
                update={"duration_ms": round((time.perf_counter() - started) * 1000)}
            )

        result = await to_thread.run_sync(work, limiter=self._limiter)
        self._remember(self._levers, key, result, _LEVERS_CACHE)
        return result

    async def effect(self, request: EffectRequest, principal: Principal) -> EffectResult:
        now = self.clock.now()
        month = self._month(request.month, now, allow_past=True)
        n_runs = self._runs(request.n_runs)
        seed = request.seed if request.seed is not None else default_seed(self.cfg)
        snapshot = await self.calibration(principal)
        targets = await self.backend.targets(self.cfg, month)
        m0, m1 = month_bounds(self.cfg, month)
        if request.scenario is not None:
            issues = check_overrides(
                self.cfg, request.scenario, month_start=m0, month_end=m1, as_of=m0
            )
            if issues:
                raise ForecastError(
                    422,
                    "validation",
                    "Request validation failed",
                    "; ".join(i.msg for i in issues),
                    [i.as_problem(("body", "scenario")) for i in issues],
                )
        scenario = request.scenario
        assumptions = request.assumptions

        def work() -> EffectResult:
            started = time.perf_counter()
            try:
                out = run_effect(
                    self.cfg,
                    snapshot.params,
                    month=month,
                    targets=targets,
                    scenario=scenario,
                    assumptions=assumptions,
                    n_runs=n_runs,
                    seed=seed,
                )
            except ValueError as exc:
                raise ForecastError(
                    422,
                    "validation",
                    "Request validation failed",
                    str(exc),
                    [{"loc": ["body", "assumptions"], "msg": str(exc), "type": "value_error"}],
                ) from exc
            return out.model_copy(
                update={"duration_ms": round((time.perf_counter() - started) * 1000)}
            )

        return await to_thread.run_sync(work, limiter=self._limiter)

    @staticmethod
    def _remember[K, V](cache: OrderedDict[K, V], key: K, value: V, size: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > size:
            cache.popitem(last=False)


def _future_state(state: PlantState, month_start: datetime) -> PlantState:
    """A later month starts from its first day: nothing produced, no repairs in progress,
    buffers, kits and filters as they are now."""
    return state.model_copy(
        update={
            "as_of": month_start,
            "mtd_output": 0,
            "open_downs": [],
            "kits_in_transit": [],
            "warnings": [*state.warnings, "future month: starts from the current buffers"],
        }
    )


def _view(run_id: int, rec: RunRecord, result: ForecastResult | None) -> ForecastRunView:
    return ForecastRunView(
        id=run_id,
        created_ts=rec.created_ts,
        mode=rec.mode,
        month=rec.month,
        status=rec.status,
        n_runs=rec.n_runs,
        seed=rec.seed,
        duration_ms=rec.duration_ms,
        overrides=rec.overrides,
        result=result,
    )
