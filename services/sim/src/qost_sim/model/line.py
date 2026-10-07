"""Lines, buffers and bodies (SPEC §6.2, §5.5, §5.6).

A line is a single work-based station: a body needs ``ict x cycle_factor x lognormal noise``
seconds of work, done at the line's current capacity (0 outside shifts or with a class A unit
down, ``degraded_capacity`` with a class B unit down, 1 otherwise). Capacity changes interrupt
the process only while it is processing, so a normal cycle costs one timeout.
"""

from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import simpy
from simpy.resources.store import StoreGet, StorePut

from qost_sim.model.quality import LineQuality, Outcome
from qost_sim.model.rng import lognormal
from qost_sim.model.states import derive_line_state
from twin_core.domain import EquipmentState

if TYPE_CHECKING:
    from qost_sim.model.equipment import Unit
    from qost_sim.model.plant import PlantModel
    from twin_core.config.plant import Line as LineConfig

_EPS = 1e-6


@dataclass(slots=True)
class Body:
    body_id: str
    product: str
    repaint_pass: bool = False


class LevelStore(simpy.Store):
    """``simpy.Store`` that reports its level after every change."""

    def __init__(
        self, env: simpy.Environment, capacity: int, on_change: Callable[[int], None]
    ) -> None:
        super().__init__(env, capacity)
        self._on_change = on_change

    def _do_put(self, event: StorePut) -> bool | None:
        before = len(self.items)
        result = super()._do_put(event)
        if len(self.items) != before:
            self._on_change(len(self.items))
        return result

    def _do_get(self, event: StoreGet) -> bool | None:
        before = len(self.items)
        result = super()._do_get(event)
        if len(self.items) != before:
            self._on_change(len(self.items))
        return result


class Buffer:
    def __init__(self, model: PlantModel, code: str, capacity: int) -> None:
        self.model = model
        self.code = code
        self.capacity = capacity
        self.level = 0
        self.store = LevelStore(model.env, capacity, self._changed)

    def _changed(self, level: int) -> None:
        if level != self.level:
            self.level = level
            self.model.emit_buffer(self)


class Line:
    def __init__(self, model: PlantModel, cfg: LineConfig, area_code: str, index: int) -> None:
        self.model = model
        self.env = model.env
        self.cfg = cfg
        self.code = cfg.code
        self.area = area_code
        self.index = index
        self.ict = cfg.ict_seconds
        self.units: list[Unit] = []
        self.input: Buffer | None = None
        self.output: Buffer | None = None
        self.first = index == 0
        noise = model.sim.process.cycle_noise
        self.noise_median = noise.median_factor
        self.noise_sigma = noise.sigma
        self.cycle_rng = model.streams.get(f"line:{cfg.code}:cycle")
        self.quality: LineQuality | None = None
        stations = cfg.rework.stations
        self.rework = simpy.Resource(self.env, capacity=stations) if stations > 0 else None
        self.repaint_queue: deque[Body] = deque()
        self._repaint_signal: simpy.Event | None = None

        self.rate = 0.0
        self.flow: EquipmentState | None = None
        self.flow_reason: str | None = None
        self.processing = False
        self.state = EquipmentState.IDLE_NO_PLAN
        self.reason: str | None = None
        self.since = 0.0
        self.produced = 0
        self.good = 0
        self.reject = 0
        self.last_body = ""
        self.last_product = ""
        self.cycle_time_s = 0.0
        self._idle = self.env.event()
        self.proc: simpy.Process | None = None

    def start(self) -> None:
        self.quality = LineQuality(self.model, self.cfg, self.area)
        self.proc = self.env.process(self._run())

    # ------------------------------------------------------------------ state

    def refresh(self) -> None:
        """Recompute state and capacity after a unit, flow or shift change."""
        status = derive_line_state(
            in_shift=self.model.in_shift,
            units=[u.condition() for u in self.units],
            flow=self.flow,
            flow_reason=self.flow_reason,
        )
        if status.state is not self.state or status.reason != self.reason:
            self.state = status.state
            self.reason = status.reason
            self.since = self.env.now
            self.model.emit_line_state(self)
        if status.capacity != self.rate:
            self.rate = status.capacity
            if (
                self.processing
                and self.proc is not None
                and self.proc is not self.env.active_process
            ):
                self.proc.interrupt("rate")

    def _set_flow(self, flow: EquipmentState | None, reason: str | None = None) -> None:
        if flow is not self.flow or reason != self.flow_reason:
            self.flow = flow
            self.flow_reason = reason
            self.refresh()

    # ------------------------------------------------------------------ process

    def _run(self) -> Generator[simpy.Event, Any, None]:
        while True:
            while not self.model.in_shift:  # bodies move only during working shifts
                yield self.model.shift_changed
            body = yield from self._take()
            product = self.model.products[body.product]
            work = self.ict * product.cycle_factor
            work *= lognormal(self.cycle_rng, self.noise_median, self.noise_sigma)
            started = self.env.now
            self.processing = True
            while work > _EPS:
                rate = self.rate
                if rate <= 0.0:
                    with contextlib.suppress(simpy.Interrupt):
                        yield self._idle
                    continue
                begin = self.env.now
                try:
                    yield self.env.timeout(work / rate)
                    work = 0.0
                except simpy.Interrupt:
                    work -= (self.env.now - begin) * rate
            self.processing = False
            self.cycle_time_s = self.env.now - started
            yield from self._finish(body)

    def _take(self) -> Generator[simpy.Event, Any, Body]:
        if self.first:
            while True:
                body = self.model.ckd.take_body()
                if body is not None:
                    self._set_flow(None)
                    return body
                self._set_flow(EquipmentState.STARVED, self.model.ckd.shortage_reason)
                yield self.model.ckd.kits_changed
        if self.repaint_queue:
            return self.repaint_queue.popleft()
        source = self.input
        assert source is not None
        if source.store.items:
            got = yield source.store.get()
            body_now: Body = got
            return body_now
        self._set_flow(EquipmentState.STARVED)
        while True:
            get = source.store.get()
            signal = self.env.event()
            self._repaint_signal = signal
            yield get | signal
            self._repaint_signal = None
            if get.triggered:
                self._set_flow(None)
                value: Body = cast(Body, get.value)
                return value
            get.cancel()
            if self.repaint_queue:
                self._set_flow(None)
                return self.repaint_queue.popleft()

    def _finish(self, body: Body) -> Generator[simpy.Event, Any, None]:
        model = self.model
        if body.repaint_pass:
            body.repaint_pass = False
            model.emit_unit(self, body, "rework_pass", None)
            yield from self._put(body)
            return
        assert self.quality is not None
        outcome = self.quality.inspect()
        self.produced += 1
        if outcome.result == "pass":
            self.good += 1
        else:
            self.reject += 1
        self.last_body = body.body_id
        self.last_product = body.product
        model.emit_unit(self, body, outcome.result, outcome.defect_code)
        if outcome.result == "pass":
            yield from self._put(body)
            return
        model.emit_defect(self, body, outcome)
        if outcome.result == "scrap":
            model.scrapped += 1
            return
        if outcome.repaint:
            body.repaint_pass = True
            self.repaint_queue.append(body)
            if self._repaint_signal is not None and not self._repaint_signal.triggered:
                self._repaint_signal.succeed()
            return
        self.env.process(self._rework(body, outcome))

    def _rework(self, body: Body, outcome: Outcome) -> Generator[simpy.Event, Any, None]:
        if self.rework is None:
            self.model.emit_unit(self, body, "rework_pass", outcome.defect_code)
            yield from self._put(body, track_flow=False)
            return
        with self.rework.request() as request:
            yield request
            yield from self.model.work_in_shift(outcome.rework_min * 60.0)
            self.model.emit_unit(self, body, "rework_pass", outcome.defect_code)
            yield from self._put(body, track_flow=False)

    def _put(self, body: Body, *, track_flow: bool = True) -> Generator[simpy.Event, Any, None]:
        target = self.output
        if target is None:
            self.model.finished(self, body)
            return
        store = target.store
        if track_flow and len(store.items) >= target.capacity:
            self._set_flow(EquipmentState.BLOCKED)
            yield store.put(body)
            self._set_flow(None)
        else:
            yield store.put(body)
