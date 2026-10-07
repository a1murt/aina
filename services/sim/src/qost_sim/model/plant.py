"""The virtual plant as one deterministic SimPy model (SPEC §6.2–6.6, FR-SIM-01).

Time: ``env.now`` = plant seconds since ``start`` (UTC). The model never reads a wall clock and
does no I/O; outputs pull records with :meth:`PlantModel.drain`. Infra (live pacing, backfill,
OPC UA, MQTT) drives it with :meth:`run_until` and :meth:`apply`.

Determinism contract: same config + seed + start + interventions [(plant time, inject)] give the
same record stream, however the run is chunked and whether telemetry is sampled or not.
"""

from __future__ import annotations

import math
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import simpy

from qost_sim.model.ckd import Ckd
from qost_sim.model.equipment import Unit
from qost_sim.model.expr import CompiledExpr, ExpressionError, compile_expr
from qost_sim.model.line import Body, Buffer, Line
from qost_sim.model.quality import Outcome
from qost_sim.model.records import ORACLE, Rec
from qost_sim.model.rng import Streams
from twin_core.calendar import ShiftInstance
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.config.simulation import (
    CkdInject,
    DefectMultiplierInject,
    FailureInject,
    SetStateInject,
)
from twin_core.domain import EquipmentState

_EPS = 1e-6
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FILTER_VARIABLES = ("dp_start", "rate", "hours_since_change")
BASE_VARIABLES = ("d", "t", "phase")

Inject = FailureInject | SetStateInject | DefectMultiplierInject | CkdInject


class ModelConfigError(ValueError):
    """simulation.yaml cannot be turned into a model (e.g. a bad signal expression)."""


@dataclass(frozen=True, slots=True)
class SignalModel:
    code: str
    expr: CompiledExpr
    unit: str
    lo: float
    hi: float


@dataclass(slots=True)
class Multiplier:
    areas: frozenset[str]
    factor: float
    until: float


@dataclass(slots=True)
class Intervention:
    t: float
    inject: Inject
    scenario_id: str | None = None


@dataclass(slots=True)
class _StateHist:
    rec: Rec
    prev: Rec | None
    batch: int


@dataclass
class Counters:
    bodies: int = 0
    wip_initial: int = 0
    fg: int = 0
    scrapped: int = 0
    extra: dict[str, int] = field(default_factory=dict)


def compile_signal_models(cfg: TwinConfig) -> dict[str, list[SignalModel]]:
    """Compile ``telemetry`` expressions per equipment type (raises ModelConfigError)."""
    sim = cfg.simulation
    pf = sim.paint_filters
    result: dict[str, list[SignalModel]] = {}
    problems: list[str] = []
    for type_code, models in sim.telemetry.per_type.items():
        signals = {s.code: s for s in cfg.equipment_types[type_code].signals}
        variables = list(BASE_VARIABLES)
        if pf is not None and pf.equipment_type == type_code:
            variables.extend(FILTER_VARIABLES)
        compiled: list[SignalModel] = []
        for code, text in models.items():
            try:
                expr = compile_expr(text, variables)
            except ExpressionError as exc:
                problems.append(f"telemetry.{type_code}.{code}: {exc}")
                continue
            sig = signals[code]
            compiled.append(SignalModel(code, expr, sig.unit, sig.lo, sig.hi))
            variables.append(code)
        result[type_code] = compiled
    if problems:
        raise ModelConfigError("invalid signal models:\n  " + "\n  ".join(problems))
    return result


class PlantModel:
    def __init__(
        self,
        cfg: TwinConfig,
        *,
        start: datetime,
        seed: int | None = None,
        telemetry_period_s: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.sim = cfg.simulation
        self.seed = cfg.simulation.clock.random_seed if seed is None else seed
        self.t0 = ensure_utc(start)
        self.env = simpy.Environment()
        self.streams = Streams(self.seed)
        self.calendar = cfg.calendar
        self.tz = cfg.timezone
        self.site = cfg.plant.site.code
        self.products = cfg.products
        self.microstop_threshold_s = cfg.rules.thresholds.microstop_threshold_s
        self.signal_models = compile_signal_models(cfg)
        self.anchor = self.sim.clock.backfill_from.astimezone(self.tz).date()

        self.outbox: list[Rec] = []
        self._batch = 0
        self._state_hist: dict[str, _StateHist] = {}
        self.counters = Counters()
        self.interventions: list[Intervention] = []
        self.multipliers: list[Multiplier] = []
        self.telemetry_period_s: float | None = None
        self._body_day: date | None = None
        self._body_seq = 0
        self._wd_cache: tuple[date, int] | None = None

        # shift state
        self.in_shift = False
        self.shift: ShiftInstance | None = None
        self.shift_end_s = 0.0
        self.shift_order = 0
        self.shift_changed = self.env.event()

        # assets
        self._unit_area: dict[str, str] = {}
        self.units: dict[str, Unit] = {}
        self.lines: dict[str, Line] = {}
        index = 0
        for area in cfg.plant.areas:
            for line_cfg in area.lines:
                for eq in line_cfg.equipment:
                    self._unit_area[eq.code] = area.code
                    self.units[eq.code] = Unit(self, eq, line_cfg.code, index)
                    index += 1
        for li, line_code in enumerate(cfg.flow_lines):
            line_cfg = cfg.lines[line_code]
            line = Line(self, line_cfg, cfg.area_of_line(line_code).code, li)
            line.units = [self.units[eq.code] for eq in line_cfg.equipment]
            self.lines[line_code] = line
        self.units_by_type: dict[str, list[Unit]] = {}
        for unit in self.units.values():
            self.units_by_type.setdefault(unit.type, []).append(unit)
        self.buffers: dict[str, Buffer] = {}
        for buf_cfg in cfg.plant.buffers:
            buffer = Buffer(self, buf_cfg.code, buf_cfg.capacity)
            self.buffers[buf_cfg.code] = buffer
            self.lines[buf_cfg.from_line].output = buffer
            self.lines[buf_cfg.to_line].input = buffer
        self.ckd = Ckd(self)
        self._bound: dict[str, list[tuple[SignalModel, Any]]] = {}
        for unit in self.units.values():
            self._bound[unit.code] = [
                (sm, sm.expr.bind(self.streams.get(f"tele:{unit.code}:{sm.code}"), unit.precursor))
                for sm in self.signal_models.get(unit.type, [])
            ]

        self._emit_initial()
        for line in self.lines.values():
            line.start()
        self.env.process(self._calendar())
        if telemetry_period_s is not None:
            self.start_telemetry(telemetry_period_s)

    # ================================================================== time helpers

    @property
    def now(self) -> datetime:
        return self.at(self.env.now)

    def at(self, t: float) -> datetime:
        return self.t0 + timedelta(seconds=t)

    def sec(self, instant: datetime) -> float:
        return (ensure_utc(instant) - self.t0).total_seconds()

    def local_hour(self, t: float) -> float:
        local = self.at(t).astimezone(self.tz)
        return local.hour + local.minute / 60 + (local.second + local.microsecond / 1e6) / 3600

    def working_day_index(self, day: date) -> int:
        """Working days from ``clock.backfill_from``'s date to ``day`` (PM / delivery cycles)."""
        cal = self.calendar
        if self._wd_cache is not None and day >= self._wd_cache[0]:
            current, idx = self._wd_cache
        elif day >= self.anchor:
            current, idx = self.anchor, 0
        else:
            idx = 0
            current = day
            while current < self.anchor:
                if cal.is_working_day(current):
                    idx -= 1
                current += timedelta(days=1)
            self._wd_cache = (day, idx)
            return idx
        while current < day:
            if cal.is_working_day(current):
                idx += 1
            current += timedelta(days=1)
        self._wd_cache = (day, idx)
        return idx

    # ================================================================== running

    def run_until(self, t: float) -> None:
        """Advance plant time to ``t`` seconds after start (no-op if already there)."""
        if t > self.env.now:
            self.env.run(until=t)

    def run_until_time(self, instant: datetime) -> None:
        self.run_until(self.sec(instant))

    def drain(self) -> list[Rec]:
        records = [r for r in self.outbox if r.kind != "void"]
        self.outbox = []
        self._batch += 1
        return records

    # ================================================================== calendar

    def _calendar(self) -> Generator[simpy.Event, Any, None]:
        cal = self.calendar
        current = cal.shift_at(self.t0, working_only=True)
        while True:
            self._enter(current)
            if current is not None:
                boundary = current.end
            else:
                nxt = cal.next_shift_change(self.at(self.env.now))
                if nxt is None:  # pragma: no cover - a calendar without working days
                    return
                boundary = nxt
            yield self.env.timeout(max(0.0, self.sec(boundary) - self.env.now))
            current = cal.shift_at(boundary, working_only=True)

    def _enter(self, shift: ShiftInstance | None) -> None:
        was_in = self.in_shift
        self.shift = shift
        self.in_shift = shift is not None
        if shift is not None:
            self.shift_end_s = self.sec(shift.end)
            self.shift_order = self.calendar.shift_codes.index(shift.code)
            if abs(self.sec(shift.start) - self.env.now) < _EPS:
                self._on_shift_start(shift)
        if was_in != self.in_shift:
            for unit in self.units.values():
                unit.command("shift")
            for line in self.lines.values():
                line.refresh()
        event, self.shift_changed = self.shift_changed, self.env.event()
        event.succeed()

    def _on_shift_start(self, shift: ShiftInstance) -> None:
        wd = self.working_day_index(shift.shift_date)
        for pm in self.sim.planned_maintenance:
            if pm.shift != shift.code:
                continue
            for unit in self.units_by_type.get(pm.equipment_type, []):
                offset = unit.index if pm.stagger else 0
                if (wd + offset) % pm.every_working_days == 0:
                    unit.command("pm", pm.duration_min * 60.0, pm.reason)
        first = self.calendar.shifts_on(shift.shift_date, working_only=True)
        delivery_day = wd % self.sim.ckd_supply.delivery_every_working_days == 0
        if first and first[0].code == shift.code and delivery_day:
            self.ckd.dispatch(shift.shift_date)

    def work_in_shift(self, seconds: float) -> Generator[simpy.Event, Any, None]:
        """Spend ``seconds`` of working-shift time (pauses outside shifts)."""
        remaining = seconds
        while remaining > _EPS:
            if not self.in_shift:
                yield self.shift_changed
                continue
            dt = min(remaining, self.shift_end_s - self.env.now)
            if dt <= _EPS:
                yield self.shift_changed
                continue
            yield self.env.timeout(dt)
            remaining -= dt

    # ================================================================== flow helpers

    def area_of_unit(self, code: str) -> str:
        return self._unit_area[code]

    def new_body_id(self) -> str:
        day = self.now.astimezone(self.tz).date()
        if day != self._body_day:
            self._body_day = day
            self._body_seq = 0
        self._body_seq += 1
        self.counters.bodies += 1
        return f"B{day:%y%m%d}{self._body_seq:04d}"

    def finished(self, line: Line, body: Body) -> None:
        self.counters.fg += 1

    @property
    def scrapped(self) -> int:
        return self.counters.scrapped

    @scrapped.setter
    def scrapped(self, value: int) -> None:
        self.counters.scrapped = value

    def on_unit_change(self, unit: Unit) -> None:
        self.lines[unit.line_code].refresh()

    def defect_multiplier(self, area: str) -> float:
        if not self.multipliers:
            return 1.0
        now = self.env.now
        self.multipliers = [m for m in self.multipliers if m.until > now]
        factor = 1.0
        for m in self.multipliers:
            if area in m.areas:
                factor *= m.factor
        return factor

    def signal_expr(self, type_code: str, signal: str) -> CompiledExpr | None:
        for sm in self.signal_models.get(type_code, []):
            if sm.code == signal:
                return sm.expr
        return None

    def signal_band(self, type_code: str, signal: str) -> tuple[float, float] | None:
        for sig in self.cfg.equipment_types[type_code].signals:
            if sig.code == signal and sig.warn_lo is not None and sig.warn_hi is not None:
                return sig.warn_lo, sig.warn_hi
        return None

    def unit_variables(self, unit: Unit, hour: float | None = None) -> dict[str, float]:
        variables = dict(unit.last_values)
        variables["d"] = unit.d
        variables["t"] = self.local_hour(self.env.now) if hour is None else hour
        variables["phase"] = unit.phase
        if unit.filters is not None:
            variables["dp_start"] = unit.filters.dp_start_pa
            variables["rate"] = unit.filter_rate_s * 3600.0
            variables["hours_since_change"] = unit.filter_hours()
        return variables

    # ================================================================== emission

    def _emit_initial(self) -> None:
        for unit in self.units.values():
            self.emit_unit_state(unit)
        for line in self.lines.values():
            self.emit_line_state(line)
        initial = self.sim.process.initial_buffers
        for code, buffer in self.buffers.items():
            level = min(initial.get(code, 0), buffer.capacity)
            for _ in range(level):
                body = Body(self.new_body_id(), self.ckd.next_model_for_wip())
                self.counters.wip_initial += 1
                buffer.store.items.append(body)
            buffer.level = level
            self.emit_buffer(buffer)
        for product in self.ckd.order:
            self.emit_ckd(product, self.ckd.kits[product], "set")

    def _emit_state(self, entity_type: str, code: str, data: dict[str, Any]) -> None:
        now = self.env.now
        hist = self._state_hist.get(code)
        if (
            hist is not None
            and hist.rec.t == now
            and hist.batch == self._batch
            and hist.rec.kind != "void"
        ):
            # same-instant change: rewrite the record instead of a zero-length interval
            if hist.prev is not None and hist.prev.data == data:
                hist.rec.kind = "void"
                self._state_hist[code] = _StateHist(hist.prev, None, -1)
                return
            hist.rec.data = data
            return
        if hist is not None and hist.rec.data == data:
            return
        rec = Rec(now, "state", entity_type, code, data)
        self.outbox.append(rec)
        self._state_hist[code] = _StateHist(rec, hist.rec if hist else None, self._batch)

    def emit_unit_state(self, unit: Unit) -> None:
        data: dict[str, Any] = {"state": unit.state.value}
        if unit.reason is not None:
            data["reason_code"] = unit.reason
            data["alarm_code"] = unit.reason
        self._emit_state("equipment", unit.code, data)

    def emit_alarm(self, unit: Unit, code: str, *, active: bool) -> None:
        reason = self.cfg.reasons.get(code)
        data = {"code": code, "active": active, "text": reason.name_ru if reason else ""}
        self.outbox.append(Rec(self.env.now, "alarm", "equipment", unit.code, data))

    def emit_line_state(self, line: Line) -> None:
        data: dict[str, Any] = {"state": line.state.value}
        if line.reason is not None:
            data["reason_code"] = line.reason
        self._emit_state("line", line.code, data)

    def emit_unit(self, line: Line, body: Body, result: str, defect_code: str | None) -> None:
        data: dict[str, Any] = {
            "line": line.code,
            "body_id": body.body_id,
            "product": body.product,
            "result": result,
        }
        if defect_code is not None:
            data["defect_code"] = defect_code
        extra = {
            "produced": line.produced,
            "good": line.good,
            "reject": line.reject,
            "cycle_time_s": line.cycle_time_s,
        }
        self.outbox.append(Rec(self.env.now, "unit", "line", line.code, data, extra))

    def emit_defect(self, line: Line, body: Body, outcome: Outcome) -> None:
        assert outcome.defect_code is not None
        data = {
            "line": line.code,
            "defect_code": outcome.defect_code,
            "qty": 1,
            "body_id": body.body_id,
            "disposition": outcome.disposition or "rework",
        }
        self.outbox.append(Rec(self.env.now, "defect", "line", line.code, data))

    def emit_buffer(self, buffer: Buffer) -> None:
        data = {"buffer": buffer.code, "level": buffer.level, "capacity": buffer.capacity}
        self.outbox.append(Rec(self.env.now, "buffer_level", "buffer", buffer.code, data))

    def emit_ckd(self, product: str, kits: int, event: str) -> None:
        data = {"product": product, "kits": kits, "event": event}
        self.outbox.append(Rec(self.env.now, "ckd", "product", product, data))

    def emit_oracle(self, unit: Unit) -> None:
        if unit.wear_params is not None:
            self.outbox.append(
                Rec(self.env.now, ORACLE, "equipment", unit.code, {"degradation": unit.d})
            )

    # ================================================================== telemetry

    def start_telemetry(self, period_s: float) -> None:
        """Sample every signal every ``period_s`` plant seconds, aligned to the UTC epoch."""
        if self.telemetry_period_s is not None:
            raise RuntimeError("telemetry already started")
        self.telemetry_period_s = period_s
        self.env.process(self._telemetry(period_s))

    def _telemetry(self, period: float) -> Generator[simpy.Event, Any, None]:
        epoch_now = (self.at(self.env.now) - _EPOCH).total_seconds()
        first = math.ceil(epoch_now / period - 1e-9) * period
        t_next = self.env.now + (first - epoch_now)
        while True:
            yield self.env.timeout(max(0.0, t_next - self.env.now))
            self.sample_telemetry()
            t_next += period

    def sample_telemetry(self) -> None:
        now = self.env.now
        hour = self.local_hour(now)
        for unit in self.units.values():
            bound = self._bound[unit.code]
            if not bound:
                continue
            variables = self.unit_variables(unit, hour)
            for sm, expr in bound:
                value = expr.evaluate(variables)
                value = min(sm.hi, max(sm.lo, value))
                variables[sm.code] = value
                unit.last_values[sm.code] = value
                self.outbox.append(
                    Rec(
                        now,
                        "telemetry",
                        "equipment",
                        unit.code,
                        {"signal": sm.code, "value": round(value, 4), "unit": sm.unit},
                    )
                )
            self.emit_oracle(unit)

    # ================================================================== scenarios

    def apply(self, inject: Inject, *, scenario_id: str | None = None) -> None:
        """Apply an intervention at the current plant time (processed on the next run)."""
        self.interventions.append(Intervention(self.env.now, inject, scenario_id))
        self.env.process(self._apply_at(self.env.now, inject))

    def schedule(self, t: float, inject: Inject, *, scenario_id: str | None = None) -> None:
        """Apply an intervention at plant time ``t`` (seconds after start)."""
        self.interventions.append(Intervention(t, inject, scenario_id))
        self.env.process(self._apply_at(t, inject))

    def _apply_at(self, t: float, inject: Inject) -> Generator[simpy.Event, Any, None]:
        yield self.env.timeout(max(0.0, t - self.env.now))
        if isinstance(inject, FailureInject):
            self.units[inject.equipment].command("fail", inject.reason, inject.duration_min * 60)
        elif isinstance(inject, SetStateInject):
            self.units[inject.equipment].command("set", inject.values)
        elif isinstance(inject, DefectMultiplierInject):
            self.multipliers.append(
                Multiplier(
                    frozenset(inject.areas),
                    inject.factor,
                    self.env.now + inject.duration_min * 60,
                )
            )
        else:
            if inject.set_kits is not None:
                self.ckd.set_kits(inject.product, inject.set_kits)
            if inject.delay_next_lot_days is not None:
                self.ckd.delay_next_lot(inject.product, inject.delay_next_lot_days)

    def active_multipliers(self) -> list[Multiplier]:
        return [m for m in self.multipliers if m.until > self.env.now]

    # ================================================================== snapshot

    def snapshot(self) -> dict[str, Any]:
        """Current values of everything the OPC UA address space shows."""
        return {
            "t": self.env.now,
            "in_shift": self.in_shift,
            "shift": None if self.shift is None else (self.shift.shift_date, self.shift.code),
            "units": {
                code: {
                    "state": u.state,
                    "reason": u.reason,
                    "since": u.since,
                    "alarm": u.alarm,
                    "degradation": u.d if u.wear_params is not None else None,
                    "values": dict(u.last_values),
                }
                for code, u in self.units.items()
            },
            "lines": {
                code: {
                    "state": ln.state,
                    "reason": ln.reason,
                    "since": ln.since,
                    "produced": ln.produced,
                    "good": ln.good,
                    "reject": ln.reject,
                    "last_body": ln.last_body,
                    "last_product": ln.last_product,
                    "cycle_time_s": ln.cycle_time_s,
                }
                for code, ln in self.lines.items()
            },
            "buffers": {
                code: {"level": b.level, "capacity": b.capacity} for code, b in self.buffers.items()
            },
            "kits": dict(self.ckd.kits),
        }

    def wip(self) -> int:
        """Bodies currently inside the plant (buffers, lines, rework, repaint queues)."""
        c = self.counters
        return c.bodies - c.fg - c.scrapped

    def state_of(self, code: str) -> EquipmentState:
        if code in self.units:
            return self.units[code].state
        return self.lines[code].state

    def kits(self) -> Mapping[str, int]:
        return dict(self.ckd.kits)
