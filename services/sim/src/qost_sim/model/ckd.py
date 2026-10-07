"""CKD kit supply and model sequencing at the first line (SPEC §6.2, §6.6).

* Stock per product starts at ``ckd_supply.initial_kits``; one kit per body created.
* Heijunka: goal chasing over ``process.product_mix`` — each step every candidate model earns its
  share of credit and the model with the most credit is built (ties: plant.yaml order).
* ``ckd_shortage_policy``: ``resequence`` — models without kits are skipped (they earn no credit,
  so there is no catch-up burst later); the line starves (``MAT-SHORTAGE``) only when no model has
  kits. ``wait`` — the sequenced model is kept and the line waits for its kit (SPEC §6.2 literal).
* Lots: on every ``delivery_every_working_days``-th working day at the first shift start, one lot
  per product of ``lot_days_of_plan`` days of plan, arriving after a triangular delay (days).
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import simpy

from qost_sim.model.line import Body

if TYPE_CHECKING:
    from qost_sim.model.plant import PlantModel

_DAY = 86_400.0
SHORTAGE_REASON = "MAT-SHORTAGE"


@dataclass(slots=True)
class Lot:
    arrival: float
    product: str
    qty: int
    seq: int


class Ckd:
    def __init__(self, model: PlantModel) -> None:
        sim = model.sim
        self.model = model
        self.env = model.env
        supply = sim.ckd_supply
        self.supply = supply
        self.policy = sim.process.ckd_shortage_policy
        self.order = [p.code for p in model.cfg.plant.products]
        self.shares = {p: sim.process.product_mix.get(p, 0.0) for p in self.order}
        self.kits = {p: supply.initial_kits.get(p, 0) for p in self.order}
        self.credit = dict.fromkeys(self.order, 0.0)
        self.pending: str | None = None
        self.transit: list[Lot] = []
        self.extra_delay_days = dict.fromkeys(self.order, 0.0)
        self.rng = model.streams.get("ckd")
        self._seq = 0
        self.kits_changed = self.env.event()
        self.consumed = 0
        self.shortage_reason = SHORTAGE_REASON
        self._idle = self.env.event()
        self._started = False
        self.proc = self.env.process(self._arrivals())

    # ------------------------------------------------------------------ sequencing

    def _heijunka(self, candidates: list[str]) -> str:
        total = sum(self.shares[p] for p in candidates)
        for p in candidates:
            self.credit[p] += self.shares[p] / total
        best = candidates[0]
        for p in candidates[1:]:
            if self.credit[p] > self.credit[best]:
                best = p
        self.credit[best] -= 1.0
        return best

    def next_model_for_wip(self) -> str:
        """Model of an initial work-in-progress body (no kit consumed)."""
        return self._heijunka([p for p in self.order if self.shares[p] > 0])

    def take_body(self) -> Body | None:
        """Next body for the first line, consuming a kit; None if the line must wait."""
        if self.policy == "wait":
            if self.pending is None:
                self.pending = self._heijunka([p for p in self.order if self.shares[p] > 0])
            product = self.pending
            if self.kits[product] <= 0:
                return None
            self.pending = None
        else:
            available = [p for p in self.order if self.shares[p] > 0 and self.kits[p] > 0]
            if not available:
                return None
            product = self._heijunka(available)
        self.kits[product] -= 1
        self.consumed += 1
        self.model.emit_ckd(product, self.kits[product], "consume")
        return Body(self.model.new_body_id(), product)

    # ------------------------------------------------------------------ stock changes

    def _changed(self) -> None:
        event, self.kits_changed = self.kits_changed, self.env.event()
        event.succeed()

    def set_kits(self, product: str, kits: int) -> None:
        self.kits[product] = kits
        self.model.emit_ckd(product, kits, "set")
        self._changed()

    def delay_next_lot(self, product: str, days: float) -> None:
        lots = [lot for lot in self.transit if lot.product == product]
        if lots:
            lot = min(lots, key=lambda x: (x.arrival, x.seq))
            lot.arrival += days * _DAY
            self._wake()
        else:
            self.extra_delay_days[product] += days

    def daily_plan(self, day: date) -> dict[str, float]:
        """Planned kits per working day for ``day``'s month (plan.yaml, else line rate x mix)."""
        cfg = self.model.cfg
        month = f"{day.year:04d}-{day.month:02d}"
        per_month: dict[str, float] = {}
        for entry in cfg.plant.plan:
            if entry.month == month and entry.level == "line_model" and entry.product:
                per_month[entry.product] = per_month.get(entry.product, 0.0) + entry.qty
        if per_month:
            days = len(cfg.calendar.working_days_in_month(day.year, day.month)) or 1
            return {p: per_month.get(p, 0.0) / days for p in self.order}
        first_line = cfg.lines[cfg.flow_lines[0]]
        per_day = first_line.plan_rate_per_shift * len(cfg.calendar.shift_codes)
        return {p: self.shares[p] * per_day for p in self.order}

    def dispatch(self, day: date) -> None:
        """Send one lot per product (called at the first shift start of a delivery day)."""
        plan = self.daily_plan(day)
        delay = self.supply.delay_days
        for product in self.order:
            qty = round(self.supply.lot_days_of_plan * plan[product])
            days = self.rng.triangular(delay.min, delay.max, delay.mode)
            days += self.extra_delay_days[product]
            self.extra_delay_days[product] = 0.0
            if qty <= 0:
                continue
            self._seq += 1
            self.transit.append(Lot(self.env.now + days * _DAY, product, qty, self._seq))
        self._wake()

    def _wake(self) -> None:
        if self._started and self.proc is not self.env.active_process:
            self.proc.interrupt("lots")

    def _arrivals(self) -> Generator[simpy.Event, Any, None]:
        self._started = True
        while True:
            if not self.transit:
                with contextlib.suppress(simpy.Interrupt):
                    yield self._idle
                continue
            lot = min(self.transit, key=lambda x: (x.arrival, x.seq))
            try:
                yield self.env.timeout(max(0.0, lot.arrival - self.env.now))
            except simpy.Interrupt:
                continue
            self.transit.remove(lot)
            self.kits[lot.product] += lot.qty
            self.model.emit_ckd(lot.product, self.kits[lot.product], "delivery")
            self._changed()
