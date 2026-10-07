"""Inspection at a line exit: defect probability and outcome (SPEC §5.5, §6.4).

p = (base + robot wear term + filter dp term + humidity term) x later-shift factor x scenario
multipliers. Every inspection draws the same fixed set of random numbers from the line's
``inspect`` stream, so a baseline and a scenario run see the same draws for the k-th body
(common random numbers).

Repaint: ``rework.repaint_share`` decides whether a defect is a full repaint; the defect type is
then chosen by weight inside the repaint (``repaint: true``) or the spot-repair group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qost_sim.model.expr import BoundExpr

if TYPE_CHECKING:
    from qost_sim.model.equipment import Unit
    from qost_sim.model.plant import PlantModel
    from twin_core.config.plant import Line as LineConfig


@dataclass(frozen=True, slots=True)
class Outcome:
    result: str
    """pass | defect | scrap"""
    defect_code: str | None = None
    disposition: str | None = None
    repaint: bool = False
    rework_min: float = 0.0


PASS = Outcome("pass")


class LineQuality:
    def __init__(self, model: PlantModel, line: LineConfig, area_code: str) -> None:
        sim = model.sim
        self.model = model
        self.area = area_code
        self.rng = model.streams.get(f"line:{line.code}:inspect")
        self.params = sim.defects.per_area.get(area_code)
        self.scrap_share = sim.defects.scrap_share_of_defects
        self.repaint_share = line.rework.repaint_share
        self.line_rework_min = line.rework.minutes_median
        self.later_shift_factor = sim.process.shift_b_defect_factor
        defects = model.cfg.defects
        self.repaint_types: list[tuple[str, float]] = []
        self.spot_types: list[tuple[str, float]] = []
        if self.params is not None:
            for code, weight in self.params.types.items():
                group = self.repaint_types if defects[code].repaint else self.spot_types
                group.append((code, weight))
        area_units = [u for u in model.units.values() if model.area_of_unit(u.code) == area_code]
        params = self.params
        self.wear_units: list[Unit] = (
            [u for u in area_units if u.type == params.wear_equipment_type]
            if params is not None and params.wear_equipment_type is not None
            else []
        )
        self.filter_units: list[Unit] = [u for u in area_units if u.filters is not None]
        pf = sim.paint_filters
        self.dp_limit = pf.dp_limit_pa if pf is not None else 0.0
        self.humidity: list[tuple[Unit, BoundExpr, float, float]] = []
        if params is not None and params.humidity_signal is not None:
            for unit in area_units:
                compiled = model.signal_expr(unit.type, params.humidity_signal)
                band = model.signal_band(unit.type, params.humidity_signal)
                if compiled is not None and band is not None:
                    bound = compiled.bind(self.rng, unit.precursor)
                    self.humidity.append((unit, bound, band[0], band[1]))

    def probability(self) -> float:
        params = self.params
        if params is None:
            return 0.0
        p = params.base
        if params.robot_wear_gain is not None and self.wear_units:
            mean_d = sum(u.d for u in self.wear_units) / len(self.wear_units)
            p += params.robot_wear_gain * mean_d
        if params.dp_gain is not None and params.dp_from_pa is not None and self.filter_units:
            dp = max(u.filter_dp() for u in self.filter_units)
            span = self.dp_limit - params.dp_from_pa
            if span > 0:
                p += params.dp_gain * min(1.0, max(0.0, (dp - params.dp_from_pa) / span))
        if params.humidity_out_of_spec_add is not None and self.humidity:
            out = False
            for unit, bound, low, high in self.humidity:
                value = bound.evaluate(self.model.unit_variables(unit))
                out = out or not low <= value <= high
            if out:
                p += params.humidity_out_of_spec_add
        if self.model.shift_order > 0:
            p *= self.later_shift_factor
        p *= self.model.defect_multiplier(self.area)
        return min(1.0, max(0.0, p))

    def inspect(self) -> Outcome:
        rng = self.rng
        u_defect, u_scrap, u_repaint, u_type = (
            rng.random(),
            rng.random(),
            rng.random(),
            rng.random(),
        )
        p = self.probability()
        if u_defect >= p or (not self.repaint_types and not self.spot_types):
            return PASS
        if self.repaint_types and self.spot_types:
            repaint = u_repaint < self.repaint_share
        else:
            repaint = bool(self.repaint_types)
        group = self.repaint_types if repaint else self.spot_types
        total = sum(w for _, w in group)
        target = u_type * total
        acc = 0.0
        code = group[-1][0]
        for candidate, weight in group:
            acc += weight
            if target < acc:
                code = candidate
                break
        defect = self.model.cfg.defects[code]
        if u_scrap < self.scrap_share or defect.disposition == "scrap":
            return Outcome("scrap", code, "scrap")
        rework_min = defect.rework_min if defect.rework_min > 0 else self.line_rework_min
        return Outcome("defect", code, "rework", repaint=repaint, rework_min=rework_min)
