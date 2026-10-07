"""Equipment unit: wear, failures with lambda(d), microstops, filters, repairs, PM (SPEC §6.3).

One SimPy process per unit owns all of its state. *Operating time* = inside a working shift and
not down; wear and every hazard accrue only then (as in ``tools/sim_sanity_check.py``).

* Wear d in [0, 1] is a Gamma process: every operating hour d += Gamma(shape, mean/shape).
* Failures are competing risks over operating time, each with an Exp(1) threshold consumed by
  its hazard. Wear group (``wear_reasons`` + ``chain_break``):
  lambda_w(d) = lambda0_w * (1 + gain * d^3); random group: constant lambda_r; microstops: own
  threshold. Separate thresholds make the next wear failure time known in advance, which drives
  ``precursor(h)`` in telemetry — random failures never have a precursor.
* Filter units (``paint_filters.equipment_type``): dp = dp_start + rate * operating hours; at
  ``dp_limit_pa`` the unit stops for a replacement (``replacement.reason``).

External changes (shift start/end, planned maintenance, scenario injects) arrive as
``simpy.Interrupt`` with a command tuple as the cause.
"""

from __future__ import annotations

import math
from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import simpy

from qost_sim.model.rng import lognormal
from qost_sim.model.states import UnitCondition, equipment_state
from twin_core.config.plant import Equipment
from twin_core.domain import EquipmentState

if TYPE_CHECKING:
    from qost_sim.model.plant import PlantModel

_HOUR = 3600.0
_EPS = 1e-9

# Down kinds
FAIL = "fail"
MICRO = "micro"
PM = "pm"
FILTER = "filter"
INJECT = "inject"


@dataclass(slots=True)
class Down:
    kind: str
    reason: str
    planned: bool
    until: float
    wear: bool = False


class Unit:
    """One piece of equipment (robot, conveyor, booth, ...)."""

    def __init__(self, model: PlantModel, eq: Equipment, line_code: str, index: int) -> None:
        sim = model.sim
        self.model = model
        self.env = model.env
        self.code = eq.code
        self.type = eq.type
        self.criticality = eq.criticality
        self.degraded_capacity = eq.degraded_capacity
        self.line_code = line_code
        self.index = index
        """Global position in plant.yaml (planned-maintenance stagger)."""
        streams = model.streams
        self._fail_rng = streams.get(f"eq:{eq.code}:fail")
        self._micro_rng = streams.get(f"eq:{eq.code}:micro")
        self._wear_rng = streams.get(f"eq:{eq.code}:wear")
        self._repair_rng = streams.get(f"eq:{eq.code}:repair")
        self._filter_rng = streams.get(f"eq:{eq.code}:filter")
        self.phase = streams.get(f"eq:{eq.code}:static").uniform(0.0, 2.0 * math.pi)

        # --- wear
        self.wear_params = sim.degradation.per_type.get(eq.type)
        self.gain = sim.degradation.wear_hazard_gain
        self.d = self.wear_params.reset_after_repair if self.wear_params else 0.0
        self.op_since_wear_update = 0.0

        # --- failures (rates per second at d = 0)
        model_f = sim.failures.get(eq.type)
        self.wear_reasons: frozenset[str] = frozenset()
        self.wear_choices: list[tuple[str, float, float, float]] = []
        """(reason, base rate, mttr median min, mttr sigma) of the wear group."""
        self.rand_choices: list[tuple[str, float, float, float]] = []
        if model_f is not None:
            per_s = 1.0 / (model_f.mtbf_h * _HOUR)
            wear = set(model_f.wear_reasons)
            for reason, share in model_f.reasons.items():
                item = (reason, share * per_s, model_f.mttr.median, model_f.mttr.sigma)
                (self.wear_choices if reason in wear else self.rand_choices).append(item)
            if model_f.chain_break is not None:
                cb = model_f.chain_break
                self.wear_choices.append(
                    (cb.reason, 1.0 / (cb.mtbf_h * _HOUR), cb.mttr.median, cb.mttr.sigma)
                )
                wear.add(cb.reason)
            self.wear_reasons = frozenset(wear)
        self.lam_w0 = sum(c[1] for c in self.wear_choices)
        self.lam_r = sum(c[1] for c in self.rand_choices)
        self.e_wear = self._fail_rng.expovariate(1.0)
        self.e_rand = self._fail_rng.expovariate(1.0)

        micro = sim.microstops.get(eq.type)
        self.micro = micro
        self.lam_m = 1.0 / (micro.mtbf_h * _HOUR) if micro is not None else 0.0
        self.micro_cap_min = (model.microstop_threshold_s - 1.0) / 60.0
        self.e_micro = self._micro_rng.expovariate(1.0)

        # --- filters
        pf = sim.paint_filters
        self.filters = pf if pf is not None and pf.equipment_type == eq.type else None
        self.filter_op_s = 0.0
        self.filter_rate_s = 0.0
        if self.filters is not None:
            self.filter_rate_s = self._draw_filter_rate()
            life_s = (self.filters.dp_limit_pa - self.filters.dp_start_pa) / self.filter_rate_s
            self.filter_op_s = self._filter_rng.uniform(0.0, 1.0) * life_s

        # --- state
        self.down: Down | None = None
        self.state = EquipmentState.IDLE_NO_PLAN
        self.reason: str | None = None
        self.since = 0.0
        self.alarm_code: str | None = None
        self.last_values: dict[str, float] = {}
        self._wait_start = 0.0
        self._wait_operating = False
        self._idle = self.env.event()
        self._started = False
        self._pending: list[tuple[Any, ...]] = []
        self.proc = self.env.process(self._run())

    # ------------------------------------------------------------------ derived values

    @property
    def operating(self) -> bool:
        return self.model.in_shift and self.down is None

    def lam_w(self) -> float:
        return self.lam_w0 * (1.0 + self.gain * self.d**3)

    def _elapsed_now(self) -> float:
        return self.env.now - self._wait_start if self._wait_operating else 0.0

    def filter_dp(self) -> float:
        """Current (true) filter pressure drop, Pa; 0 for units without filters."""
        if self.filters is None:
            return 0.0
        op_s = self.filter_op_s + self._elapsed_now()
        return self.filters.dp_start_pa + self.filter_rate_s * op_s

    def filter_hours(self) -> float:
        return (self.filter_op_s + self._elapsed_now()) / _HOUR

    def precursor(self, hours: float) -> float:
        """0 → 1 over the last ``hours`` operating hours before the next wear failure."""
        if self.down is not None or self.lam_w0 <= 0 or hours <= 0:
            return 0.0
        remaining = self.e_wear / self.lam_w() - self._elapsed_now()
        return min(1.0, max(0.0, 1.0 - remaining / (hours * _HOUR)))

    @property
    def alarm(self) -> bool:
        return self.alarm_code is not None

    def condition(self) -> UnitCondition:
        return UnitCondition(
            self.criticality, self.degraded_capacity, self.state, self.reason, self.since
        )

    # ------------------------------------------------------------------ commands

    def command(self, *cause: Any) -> None:
        """Deliver a command (shift / pm / fail / set) to the unit process."""
        if not self._started or self.proc is self.env.active_process:
            self._pending.append(cause)
            return
        self.proc.interrupt(cause)

    # ------------------------------------------------------------------ process

    def _run(self) -> Generator[simpy.Event, Any, None]:
        self._started = True
        while True:
            while self._pending:  # commands that arrived while this process was not interruptible
                self._on_command(self._pending.pop(0))
            dt, what = self._next_timer()
            self._wait_start = self.env.now
            self._wait_operating = self.operating
            try:
                if dt is None:
                    yield self._idle
                else:
                    yield self.env.timeout(max(0.0, dt))
            except simpy.Interrupt as intr:
                self._elapse(self.env.now - self._wait_start)
                self._on_command(intr.cause)
                continue
            self._elapse(self.env.now - self._wait_start)
            self._on_timer(what)

    def _next_timer(self) -> tuple[float | None, str]:
        if self.down is not None:
            return self.down.until - self.env.now, "repair"
        if not self.model.in_shift:
            return None, ""
        best: float = math.inf
        what = ""
        if self.wear_params is not None:
            best, what = _HOUR - self.op_since_wear_update, "wear"
        if self.lam_w0 > 0:
            t = self.e_wear / self.lam_w()
            if t < best:
                best, what = t, "wear_fail"
        if self.lam_r > 0:
            t = self.e_rand / self.lam_r
            if t < best:
                best, what = t, "rand_fail"
        if self.lam_m > 0:
            t = self.e_micro / self.lam_m
            if t < best:
                best, what = t, "micro"
        if self.filters is not None:
            t = (self.filters.dp_limit_pa - self.filter_dp()) / self.filter_rate_s
            if t < best:
                best, what = t, "filter"
        return (None, "") if math.isinf(best) else (best, what)

    def _elapse(self, dt: float) -> None:
        if not self._wait_operating or dt <= 0:
            self._wait_operating = False
            return
        self._wait_operating = False
        lam_w = self.lam_w()
        self.e_wear -= lam_w * dt
        self.e_rand -= self.lam_r * dt
        self.e_micro -= self.lam_m * dt
        self.op_since_wear_update += dt
        if self.filters is not None:
            self.filter_op_s += dt

    def _on_timer(self, what: str) -> None:
        if what == "repair":
            self._repair_done()
        elif what == "wear":
            params = self.wear_params
            assert params is not None
            step = self._wear_rng.gammavariate(params.shape, params.mean_rate_per_h / params.shape)
            self.d = min(1.0, self.d + step)
            self.op_since_wear_update = 0.0
        elif what == "wear_fail":
            reason, median, sigma = self._choose(self.wear_choices)
            self.e_wear = self._fail_rng.expovariate(1.0)
            duration = lognormal(self._repair_rng, median, sigma) * 60.0
            self._go_down(Down(FAIL, reason, False, self.env.now + duration, wear=True))
        elif what == "rand_fail":
            reason, median, sigma = self._choose(self.rand_choices)
            self.e_rand = self._fail_rng.expovariate(1.0)
            duration = lognormal(self._repair_rng, median, sigma) * 60.0
            self._go_down(Down(FAIL, reason, False, self.env.now + duration))
        elif what == "micro":
            micro = self.micro
            assert micro is not None
            self.e_micro = self._micro_rng.expovariate(1.0)
            minutes = min(
                lognormal(self._micro_rng, micro.duration.median, micro.duration.sigma),
                self.micro_cap_min,
            )
            self._go_down(Down(MICRO, micro.reason, False, self.env.now + minutes * 60.0))
        elif what == "filter":
            pf = self.filters
            assert pf is not None
            minutes = lognormal(self._repair_rng, pf.replacement.median, pf.replacement.sigma)
            self._go_down(Down(FILTER, pf.replacement.reason, False, self.env.now + minutes * 60.0))

    def _choose(self, choices: list[tuple[str, float, float, float]]) -> tuple[str, float, float]:
        total = sum(c[1] for c in choices)
        u = self._fail_rng.random() * total
        acc = 0.0
        for reason, rate, median, sigma in choices:
            acc += rate
            if u < acc:
                return reason, median, sigma
        reason, _rate, median, sigma = choices[-1]
        return reason, median, sigma

    def _on_command(self, cause: Any) -> None:
        if not isinstance(cause, tuple) or not cause:
            return
        name = cause[0]
        if name == "shift":
            if self.down is None:
                self._set_state(equipment_state(in_shift=self.model.in_shift, down=None), None)
        elif name == "pm":
            _, duration_s, reason = cause
            if self.down is None and self.model.in_shift:
                self._go_down(Down(PM, reason, True, self.env.now + duration_s))
        elif name == "fail":
            _, reason, duration_s = cause
            self._go_down(
                Down(
                    INJECT,
                    reason,
                    False,
                    self.env.now + duration_s,
                    wear=reason in self.wear_reasons,
                )
            )
        elif name == "set":
            _, values = cause
            self._set_values(values)

    def _set_values(self, values: dict[str, float]) -> None:
        for key, value in values.items():
            if key == "degradation":
                self.d = min(1.0, max(0.0, float(value)))
            elif self.filters is not None and key == self.filters.signal:
                dp = max(self.filters.dp_start_pa, float(value))
                self.filter_op_s = (dp - self.filters.dp_start_pa) / self.filter_rate_s
        self.model.emit_oracle(self)

    # ------------------------------------------------------------------ down / up

    def _go_down(self, down: Down) -> None:
        self.down = down
        state = EquipmentState.DOWN_PLANNED if down.planned else EquipmentState.DOWN_UNPLANNED
        self._set_state(state, down.reason)

    def _repair_done(self) -> None:
        down = self.down
        assert down is not None
        self.down = None
        if down.wear and self.wear_params is not None:
            self.d = min(self.d, self.wear_params.reset_after_repair)
        if down.kind == PM and self.wear_params is not None:
            self.d = max(self.wear_params.pm_floor, self.d - self.wear_params.pm_reduction)
        if down.kind == FILTER:
            self.filter_op_s = 0.0
            self.filter_rate_s = self._draw_filter_rate()
        self._set_state(equipment_state(in_shift=self.model.in_shift, down=None), None)

    def _draw_filter_rate(self) -> float:
        pf = self.filters
        assert pf is not None
        per_h = 0.0
        while per_h <= 0.0:  # truncated normal: resample the (practically impossible) tail
            per_h = self._filter_rng.gauss(pf.dp_rate_pa_per_h.mean, pf.dp_rate_pa_per_h.sd)
        return per_h / _HOUR

    def _set_state(self, state: EquipmentState, reason: str | None) -> None:
        if state is self.state and reason == self.reason:
            return
        self.state = state
        self.reason = reason
        self.since = self.env.now
        self.model.emit_unit_state(self)
        alarm_code = reason if state is EquipmentState.DOWN_UNPLANNED else None
        if alarm_code != self.alarm_code:
            if self.alarm_code is not None:
                self.model.emit_alarm(self, self.alarm_code, active=False)
            if alarm_code is not None:
                self.model.emit_alarm(self, alarm_code, active=True)
            self.alarm_code = alarm_code
        self.model.on_unit_change(self)
