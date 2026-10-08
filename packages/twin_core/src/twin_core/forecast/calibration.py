"""Calibration of the fast model from history (SPEC §10.1).

:class:`CalibrationInputs` are raw facts over the window — operating hours, stops, per-shift line
time and output, defect counts — that an adapter reads from the §8 tables (``qost_api``) or from
virtual-plant records (``qost_sim.forecast_inputs``). :func:`calibrate` turns them into
:class:`~twin_core.forecast.params.CalibrationParams`:

* failure rate per unit = failures / operating hours; with fewer than ``min_failures`` failures
  the posterior of a Gamma prior built from ``simulation.yaml: failures`` (Bayesian fallback);
  filter replacements are not failures (filters are modelled separately);
* repair time lognormal (mu, sigma) per unit, else pooled by equipment type, else the prior;
* line speed efficiency per shift: Beta by the method of moments (``eff_basis: net`` removes the
  losses the model applies itself: B-class degraded time and repaint passes);
* defect share per area: Beta posterior with a Beta(1, 1) prior over first-pass counts;
* filters: pressure-drop growth per operating hour from consecutive replacements.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from statistics import fmean, stdev

from twin_core.calendar import PlantCalendar
from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.config.simulation import FailureModel
from twin_core.forecast.params import (
    AreaDefects,
    BetaShare,
    BufferParams,
    CalibrationParams,
    EquipmentParams,
    FilterParams,
    GammaRate,
    LineParams,
    LogNormalMinutes,
    RepairBasis,
    Source,
    Targets,
)

_ONE_DAY = timedelta(days=1)
_MIN_SIGMA = 0.05
_MAX_EFF = 0.9999
_SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class Stop:
    """A stop of one unit (``downtime`` row that is not a microstop)."""

    equipment: str
    start: datetime
    end: datetime | None
    """``None`` while still open."""
    planned: bool
    reason: str

    @property
    def duration_min(self) -> float | None:
        if self.end is None:
            return None
        return (self.end - self.start).total_seconds() / 60.0


@dataclass(frozen=True, slots=True)
class LineShiftFacts:
    """Production time and output of one line in one closed working shift."""

    line: str
    shift_date: date
    shift_code: str
    apt_s: float
    """Production time: ``RUNNING`` + ``DEGRADED`` + microstops (ISO 22400 APT)."""
    degraded_s: float
    exits: Mapping[str, int]
    """First exits (PQ) by product."""
    defects: int
    """First exits with a defect (defect or scrap)."""
    repaint_defects: int
    """Defects whose code needs a full repaint (``defect_codes.yaml: repaint``)."""


@dataclass(frozen=True, slots=True)
class CalibrationInputs:
    """Raw history facts over ``[window_from, window_to)``."""

    window_from: datetime
    window_to: datetime
    working_days: int
    operating_h: Mapping[str, float]
    """``RUNNING`` hours per unit."""
    stops: Sequence[Stop]
    """Stops (planned or not, no microstops) that started in the window."""
    line_shifts: Sequence[LineShiftFacts]
    defect_codes: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    """Line -> defect code -> first-pass defects with that code."""


# --------------------------------------------------------------------------- window and targets


def calibration_window(
    cfg: TwinConfig, as_of: datetime, window_days: int | None = None
) -> tuple[datetime, datetime, int]:
    """``(from, to, working days)``: the last N complete working days before ``as_of``'s day.

    Both ends are local midnights (UTC); ``to`` is the start of ``as_of``'s local day, so the
    window (and the calibration snapshot) stays the same for a whole day.
    """
    n = window_days or cfg.simulation.forecast.window_days
    cal = cfg.calendar
    tz = cfg.timezone
    today = ensure_utc(as_of).astimezone(tz).date()
    day = today - _ONE_DAY
    found: list[date] = []
    for _ in range(n * 7 + 31):
        if len(found) >= n:
            break
        if cal.is_working_day(day):
            found.append(day)
        day -= _ONE_DAY
    first = found[-1] if found else today
    start = datetime.combine(first, time(0), tzinfo=tz)
    end = datetime.combine(today, time(0), tzinfo=tz)
    return ensure_utc(start), ensure_utc(end), len(found)


def config_hash(cfg: TwinConfig) -> str:
    """Hash of the configuration parts the forecast depends on (snapshot cache key)."""
    h = hashlib.sha256()
    for model in (cfg.plant, cfg.simulation, cfg.defect_codes, cfg.rules, cfg.business):
        h.update(model.model_dump_json().encode())
    return h.hexdigest()[:16]


def month_bounds(cfg: TwinConfig, month: str) -> tuple[datetime, datetime]:
    """UTC bounds of a plant-local month ``YYYY-MM``."""
    year, mon = (int(part) for part in month.split("-"))
    tz = cfg.timezone
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    return ensure_utc(start), ensure_utc(end)


def month_of(cfg: TwinConfig, instant: datetime) -> str:
    local = ensure_utc(instant).astimezone(cfg.timezone)
    return f"{local.year:04d}-{local.month:02d}"


def targets_from_config(cfg: TwinConfig, month: str) -> Targets:
    """Targets of ``month`` from ``plant.yaml: plan`` (the database copy wins when present)."""
    plant: int | None = None
    line_total = 0
    has_line = False
    for entry in cfg.plant.plan:
        if entry.month != month:
            continue
        if entry.level == "plant_target":
            plant = entry.qty
        else:
            line_total += entry.qty
            has_line = True
    return Targets(plant_target=plant, line_plan=line_total if has_line else None, source="config")


# --------------------------------------------------------------------------- estimators


def _prior_rate(model: FailureModel | None) -> float:
    if model is None:
        return 0.0
    rate = 1.0 / model.mtbf_h
    if model.chain_break is not None:
        rate += 1.0 / model.chain_break.mtbf_h
    return rate


def _prior_repair(model: FailureModel | None) -> tuple[float, float]:
    """(mu, sigma) of the prior repair time; a chain break is mixed in by moment matching."""
    if model is None:
        return math.log(30.0), 0.5
    mu, sigma = math.log(model.mttr.median), model.mttr.sigma
    if model.chain_break is None:
        return mu, sigma
    w_main = 1.0 / model.mtbf_h
    w_cb = 1.0 / model.chain_break.mtbf_h
    total = w_main + w_cb
    parts = [
        (w_main / total, mu, sigma),
        (w_cb / total, math.log(model.chain_break.mttr.median), model.chain_break.mttr.sigma),
    ]
    m = sum(w * m_i for w, m_i, _ in parts)
    var = sum(w * (s_i**2 + m_i**2) for w, m_i, s_i in parts) - m**2
    return m, math.sqrt(max(var, _MIN_SIGMA**2))


def _lognormal_fit(durations: Sequence[float]) -> tuple[float, float]:
    logs = [math.log(max(d, 1e-3)) for d in durations]
    mu = fmean(logs)
    sigma = stdev(logs) if len(logs) > 1 else _MIN_SIGMA
    return mu, max(sigma, _MIN_SIGMA)


def _beta_moments(values: Sequence[float], *, c_min: float, c_max: float) -> tuple[float, float]:
    m = min(max(fmean(values), 1e-6), _MAX_EFF)
    v = stdev(values) ** 2 if len(values) > 1 else 0.0
    c = c_max if v <= 0 else m * (1.0 - m) / v - 1.0
    c = min(max(c, c_min), c_max)
    return m * c, (1.0 - m) * c


def working_seconds_between(calendar: PlantCalendar, start: datetime, end: datetime) -> float:
    """Working-shift seconds inside ``[start, end)``."""
    total = 0.0
    for shift in calendar.shifts_between(start, end, working_only=True):
        lo, hi = max(shift.start, ensure_utc(start)), min(shift.end, ensure_utc(end))
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def _prior_efficiency(cfg: TwinConfig, line: str) -> float:
    """Speed (1 / mean cycle noise) x (1 - microstop share of the line's units)."""
    sim = cfg.simulation
    noise = sim.process.cycle_noise
    speed = 1.0 / (noise.median_factor * math.exp(noise.sigma**2 / 2.0))
    cap_min = cfg.rules.thresholds.microstop_threshold_s / 60.0
    loss_per_h = 0.0
    for eq in cfg.lines[line].equipment:
        micro = sim.microstops.get(eq.type)
        if micro is None or eq.criticality == "C":
            continue
        mean = min(micro.duration.median * math.exp(micro.duration.sigma**2 / 2.0), cap_min)
        loss_per_h += (1.0 / micro.mtbf_h) * mean * (1.0 - eq.degraded_capacity)
    return speed * max(0.0, 1.0 - loss_per_h / 60.0)


def calibrate(
    cfg: TwinConfig, inputs: CalibrationInputs, *, computed_at: datetime
) -> CalibrationParams:
    """Estimate the model parameters from ``inputs`` (pure; see the module docstring)."""
    sim = cfg.simulation
    fc = sim.forecast
    pr = fc.priors
    pf = sim.paint_filters
    threshold_min = cfg.rules.thresholds.microstop_threshold_s / 60.0
    warnings: list[str] = []
    filter_reason = pf.replacement.reason if pf is not None else None
    filter_type = pf.equipment_type if pf is not None else None

    stops_by_eq: dict[str, list[Stop]] = defaultdict(list)
    for stop in inputs.stops:
        duration = stop.duration_min
        if duration is not None and duration < threshold_min:
            continue  # a microstop: part of line efficiency
        stops_by_eq[stop.equipment].append(stop)

    def is_failure(eq_type: str, stop: Stop) -> bool:
        return not stop.planned and not (eq_type == filter_type and stop.reason == filter_reason)

    # ---- failures and repairs
    durations_by_type: dict[str, list[float]] = defaultdict(list)
    for code, eq in cfg.equipment.items():
        for stop in stops_by_eq.get(code, []):
            d = stop.duration_min
            if is_failure(eq.type, stop) and d is not None:
                durations_by_type[eq.type].append(d)

    equipment: dict[str, EquipmentParams] = {}
    for code, eq in cfg.equipment.items():
        model = sim.failures.get(eq.type)
        fails = [s for s in stops_by_eq.get(code, []) if is_failure(eq.type, s)]
        n = len(fails)
        hours = float(inputs.operating_h.get(code, 0.0))
        lam0 = _prior_rate(model)
        rate: GammaRate
        if n >= pr.min_failures and hours > 0:
            rate = GammaRate(
                n=n, operating_h=hours, per_h=n / hours, shape=float(n), rate_h=hours, source="data"
            )
        elif lam0 > 0:
            a0 = pr.prior_strength
            shape, rate_h = a0 + n, a0 / lam0 + hours
            rate = GammaRate(
                n=n,
                operating_h=hours,
                per_h=shape / rate_h,
                shape=shape,
                rate_h=rate_h,
                source="prior",
            )
        else:
            per_h = n / hours if hours > 0 else 0.0
            rate = GammaRate(
                n=n,
                operating_h=hours,
                per_h=per_h,
                shape=float(n),
                rate_h=max(hours, 1e-9),
                source="data",
            )
        if hours <= 0:
            warnings.append(f"no operating time of {code} in the window")

        own = [d for s in fails if (d := s.duration_min) is not None]
        pooled = durations_by_type.get(eq.type, [])
        basis: RepairBasis
        source: Source
        if len(own) >= pr.min_repairs:
            mu, sigma = _lognormal_fit(own)
            basis, source, n_rep = "equipment", "data", len(own)
        elif len(pooled) >= pr.min_repairs:
            mu, sigma = _lognormal_fit(pooled)
            basis, source, n_rep = "type", "data", len(pooled)
        else:
            mu, sigma = _prior_repair(model)
            basis, source, n_rep = "prior", "prior", len(own)
        equipment[code] = EquipmentParams(
            code=code,
            line=cfg.line_of_equipment(code).code,
            type=eq.type,
            criticality=eq.criticality,
            degraded_capacity=eq.degraded_capacity,
            failures=rate,
            repair=LogNormalMinutes(n=n_rep, mu=mu, sigma=sigma, source=source, basis=basis),
        )

    # ---- lines: efficiency and rework
    mix = sim.process.product_mix
    cf_mix = sum(share * cfg.products[p].cycle_factor for p, share in mix.items()) or 1.0
    shifts_by_line: dict[str, list[LineShiftFacts]] = defaultdict(list)
    for fact in inputs.line_shifts:
        shifts_by_line[fact.line].append(fact)

    lines: dict[str, LineParams] = {}
    areas: dict[str, AreaDefects] = {}
    for line_code in cfg.flow_lines:
        lc = cfg.lines[line_code]
        area = cfg.area_of_line(line_code).code
        ict = float(lc.ict_seconds)
        b_caps = [eq.degraded_capacity for eq in lc.equipment if eq.criticality == "B"]
        b_loss = 1.0 - min(b_caps) if b_caps else 0.0
        repaint_share = lc.rework.repaint_share or 0.0
        values: list[float] = []
        for fact in shifts_by_line.get(line_code, []):
            if fact.apt_s < pr.eff_min_apt_min * 60.0:
                continue
            pri = sum(ict * cfg.products[p].cycle_factor * k for p, k in fact.exits.items())
            denom = fact.apt_s
            if fc.eff_basis == "net":
                if repaint_share > 0:
                    pri += fact.repaint_defects * ict * cf_mix
                denom -= fact.degraded_s * b_loss
            if denom > 0:
                values.append(min(pri / denom, _MAX_EFF))
        if len(values) >= pr.eff_min_shifts:
            a, b = _beta_moments(
                values, c_min=pr.eff_concentration_min, c_max=pr.eff_concentration_max
            )
            eff = BetaShare(n=len(values), alpha=a, beta=b, source="data")
        else:
            m0 = _prior_efficiency(cfg, line_code)
            c = pr.eff_concentration
            eff = BetaShare(n=len(values), alpha=m0 * c, beta=(1.0 - m0) * c, source="prior")
            warnings.append(f"line {line_code}: {len(values)} shifts, efficiency from the prior")

        # rework minutes weighted by the window's defect-code mix (repaint codes excluded)
        codes = inputs.defect_codes.get(line_code, {})
        weights: dict[str, float] = {
            c_: float(k)
            for c_, k in codes.items()
            if c_ in cfg.defects and not cfg.defects[c_].repaint
        }
        if not weights:
            area_def = sim.defects.per_area.get(area)
            if area_def is not None:
                weights = {c_: w for c_, w in area_def.types.items() if not cfg.defects[c_].repaint}
        total_w = sum(weights.values())
        if total_w > 0:
            rework_min = (
                sum(
                    w * (cfg.defects[c_].rework_min or lc.rework.minutes_median)
                    for c_, w in weights.items()
                )
                / total_w
            )
        else:
            rework_min = float(lc.rework.minutes_median)
        lines[line_code] = LineParams(
            code=line_code,
            area=area,
            ict_seconds=ict,
            cycle_factor_mix=cf_mix,
            efficiency=eff,
            rework_stations=lc.rework.stations,
            rework_mean_min=max(rework_min, 1e-3),
            repaint_share=repaint_share,
        )

        pq = sum(sum(f.exits.values()) for f in shifts_by_line.get(line_code, []))
        d = sum(f.defects for f in shifts_by_line.get(line_code, []))
        area_def = sim.defects.per_area.get(area)
        if pq >= pr.defect_min_units:
            share = BetaShare(n=pq, alpha=1.0 + d, beta=1.0 + pq - d, source="data")
        else:
            base = area_def.base if area_def is not None else 0.0
            k = pr.defect_concentration
            share = BetaShare(
                n=pq,
                alpha=max(base * k, 1e-3) + d,
                beta=max((1.0 - base) * k, 1e-3) + pq - d,
                source="prior",
            )
        areas[area] = AreaDefects(
            area=area,
            line=line_code,
            pq=pq,
            defects=d,
            rate=share,
            scrap_share=sim.defects.scrap_share_of_defects,
        )

    # ---- filters
    filters: FilterParams | None = None
    if pf is not None:
        units = [code for code, eq in cfg.equipment.items() if eq.type == pf.equipment_type]
        span = pf.dp_limit_pa - pf.dp_start_pa
        rates: list[float] = []
        swaps: list[float] = []
        for code in units:
            changes = sorted(
                (s for s in stops_by_eq.get(code, []) if s.reason == pf.replacement.reason),
                key=lambda s: s.start,
            )
            swaps.extend(d for s in changes if (d := s.duration_min) is not None)
            others = [s for s in stops_by_eq.get(code, []) if s.reason != pf.replacement.reason]
            for prev, nxt in pairwise(changes):
                if prev.end is None:
                    continue
                seconds = working_seconds_between(cfg.calendar, prev.end, nxt.start)
                for other in others:
                    if other.end is None:
                        continue
                    lo, hi = max(other.start, prev.end), min(other.end, nxt.start)
                    if hi > lo:
                        seconds -= (hi - lo).total_seconds()
                if seconds > 0:
                    rates.append(span / (seconds / _SECONDS_PER_HOUR))
        if len(rates) >= pr.filter_min_lives:
            rate_mean = fmean(rates)
            rate_sd = stdev(rates) if len(rates) > 1 else pf.dp_rate_pa_per_h.sd
            rate_source: Source = "data"
        else:
            rate_mean, rate_sd = pf.dp_rate_pa_per_h.mean, pf.dp_rate_pa_per_h.sd
            rate_source = "prior"
        if len(swaps) >= pr.min_repairs:
            mu, sigma = _lognormal_fit(swaps)
            repl = LogNormalMinutes(n=len(swaps), mu=mu, sigma=sigma, source="data")
        else:
            repl = LogNormalMinutes(
                n=len(swaps),
                mu=math.log(pf.replacement.median),
                sigma=pf.replacement.sigma,
                source="prior",
                basis="prior",
            )
        filters = FilterParams(
            equipment=units,
            signal=pf.signal,
            reason=pf.replacement.reason,
            dp_start_pa=pf.dp_start_pa,
            dp_limit_pa=pf.dp_limit_pa,
            rate_mean_pa_h=rate_mean,
            rate_sd_pa_h=rate_sd,
            n_lives=len(rates),
            rate_source=rate_source,
            replacement=repl,
        )

    buffers = {
        b.code: BufferParams(
            code=b.code,
            from_line=b.from_line,
            to_line=b.to_line,
            capacity=b.capacity,
            initial=min(sim.process.initial_buffers.get(b.code, 0), b.capacity),
        )
        for b in cfg.plant.buffers
    }
    return CalibrationParams(
        window_from=ensure_utc(inputs.window_from),
        window_to=ensure_utc(inputs.window_to),
        working_days=inputs.working_days,
        computed_at=ensure_utc(computed_at),
        config_hash=config_hash(cfg),
        eff_basis=fc.eff_basis,
        equipment=equipment,
        lines=lines,
        areas=areas,
        filters=filters,
        buffers=buffers,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- work in progress

WIP_LOOKBACK = timedelta(days=3)
"""Unit events this far back are enough to place every body that is still in the plant."""


@dataclass(frozen=True, slots=True)
class UnitExit:
    """One ``unit_event`` row (a body leaving a line)."""

    body_id: str
    line: str
    ts: datetime
    result: str


def held_from_exits(
    cfg: TwinConfig, exits: Sequence[UnitExit], buffers: Mapping[str, float]
) -> dict[str, float]:
    """Work in progress per line from body-level exits and buffer levels.

    A body whose last exit is a defect is in its line's repair or repaint queue; bodies that
    left line k (pass / rework_pass) beyond the level of the buffer after it are inside line
    k + 1. The first line's body in process is not visible (at most one body).
    """
    flow = list(cfg.flow_lines)
    index = {line: i for i, line in enumerate(flow)}
    buffer_after = {b.from_line: b.code for b in cfg.plant.buffers}
    last: dict[str, UnitExit] = {}
    for item in sorted(exits, key=lambda e: (e.ts, index.get(e.line, -1))):
        if item.line in index:
            last[item.body_id] = item
    held = dict.fromkeys(flow, 0.0)
    passed = dict.fromkeys(flow, 0)
    for item in last.values():
        if item.result == "defect":
            held[item.line] += 1.0
        elif item.result in ("pass", "rework_pass"):
            passed[item.line] += 1
    for i, line in enumerate(flow[:-1]):
        code = buffer_after.get(line)
        level = buffers.get(code, 0.0) if code is not None else 0.0
        held[flow[i + 1]] += max(0.0, passed[line] - level)
    return held
