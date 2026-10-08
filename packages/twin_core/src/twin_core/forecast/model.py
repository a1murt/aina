"""Fast Monte Carlo model of the month's output (SPEC §10.2, FR-FC-01).

Vectorised over runs with NumPy, one step = up to one hour of working time:

1. Repairs in progress lose the non-working time before the step (repairs go on off-shift);
   planned maintenance due at a shift start is added.
2. New failures per unit ~ Poisson(lambda x step) by the inverse CDF of one uniform; durations
   lognormal x ``mttr_multiplier``, starting at a uniform offset after the unit's current repair;
   what does not fit into the step is carried over.
3. Paint-booth filters: pressure drop grows with operating time; at the limit the booth stops
   for a swap. ``predictive_shift_change`` swaps at the end of a shift when the limit would be
   reached during the next one.
4. Line capacity = (step - sum of unit downtime x (1 - degraded_capacity)) x 60 / (ICT x mix
   cycle factor) x shift efficiency; PAINT-1 loses ``repaint_share x p`` for second passes.
5. Material flow, fluid approximation: forward pass (material), backward pass (blocking by the
   downstream buffer; bodies under repair hold their place in it), forward pass again;
   buffers, repair queues (released one step later) and CKD kits are updated. The last line's
   output is finished cars; work in progress inside lines starts in their repair queues.

Common random numbers: every draw is keyed by a calendar slot (:mod:`twin_core.forecast.rng`),
so lower failure rates, lower defect shares, larger buffers, shorter cycle times and extra
shifts never lower the output of any run compared to the baseline with the same seed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import numpy.typing as npt
from scipy.special import ndtr, ndtri

from twin_core.clock import ensure_utc
from twin_core.config import TwinConfig
from twin_core.domain import FilterPolicy
from twin_core.forecast.horizon import Horizon, build_horizon
from twin_core.forecast.overrides import CLASSES, Overrides
from twin_core.forecast.params import CalibrationParams, PlantState
from twin_core.forecast.rng import Stream, generator

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

FILTER_LIVES = 48
"""Pre-drawn filter lives per booth and run (a month has at most ~25)."""
_MAX_P = 0.95
_DAY_S = 86_400.0


@dataclass(frozen=True)
class CkdSetup:
    products: tuple[str, ...]
    mix: FloatArray
    kits: FloatArray
    lot_product: IntArray
    lot_qty: FloatArray
    lot_dispatch: FloatArray
    """Epoch seconds."""
    lot_key: IntArray
    lot_extra_days: FloatArray
    transit_product: IntArray
    transit_qty: FloatArray
    transit_dispatch: FloatArray
    transit_arrival: FloatArray
    """Epoch seconds; NaN = unknown (drawn conditionally on not having arrived yet)."""
    transit_extra_days: FloatArray
    delay_min: float
    delay_mode: float
    delay_max: float
    wait_policy: bool


@dataclass(frozen=True)
class Setup:
    """Everything one forecast needs, as arrays (built by :func:`build_setup`)."""

    horizon: Horizon
    mtd: int
    equipment: tuple[str, ...]
    lines: tuple[str, ...]
    areas: tuple[str, ...]
    loss_w: FloatArray
    """(units, lines): 1 - degraded_capacity in the unit's line column (0 for class C)."""
    lam_shape: FloatArray
    lam_rate: FloatArray
    lam_point: FloatArray
    mtbf_mult: FloatArray
    rep_mu: FloatArray
    rep_sigma: FloatArray
    mttr_mult: FloatArray
    pm_steps: tuple[tuple[int, int, float], ...]
    """(step, unit index, minutes)."""
    open_kind: IntArray
    """Per unit: 0 not down, 1 repair (lognormal residual), 2 fixed residual, 3 filter swap."""
    open_elapsed: FloatArray
    open_fixed: FloatArray
    rate_per_min: FloatArray
    """(lines,): units per minute at 100% = 60 / (ICT x mix cycle factor)."""
    eff_alpha: FloatArray
    eff_beta: FloatArray
    p_alpha: FloatArray
    p_beta: FloatArray
    p_point: FloatArray
    p_scale: FloatArray
    p_fixed: FloatArray
    """Per line: NaN, or a fixed defect share (override over a zero posterior mean)."""
    repaint: FloatArray
    scrap: FloatArray
    rw_stations: FloatArray
    rw_min: FloatArray
    buf_cap: FloatArray
    buf_init: FloatArray
    held_init: FloatArray
    """(lines,): work in progress inside each line (released downstream like repairs)."""
    booth: IntArray
    """Unit indices of filter booths."""
    dp0: FloatArray
    """Per booth: pressure drop now (NaN = unknown, drawn uniformly)."""
    dp_start: float
    dp_limit: float
    rate_mean: float
    rate_sd: float
    swap_mu: float
    swap_sigma: float
    filter_policy: FilterPolicy
    service_loss_min: float
    ckd: CkdSetup
    uncertainty: bool
    extra_shift_count: int
    overrides: Overrides = field(default_factory=Overrides)


@dataclass(frozen=True)
class Paths:
    """Per-run outcomes of :func:`simulate`."""

    total: FloatArray
    """MTD + finished cars in the horizon, per run."""
    daily: FloatArray
    """(days, runs): cumulative total at the end of each day of the horizon."""
    rework: FloatArray
    """(runs, lines): defective first passes sent to repair or repaint (not scrapped)."""


def _per_unit(values: dict[str, float], cfg: TwinConfig, codes: tuple[str, ...]) -> FloatArray:
    out = np.ones(len(codes))
    for i, code in enumerate(codes):
        crit = cfg.equipment[code].criticality
        if crit in values:
            out[i] = values[crit]
        if code in values:
            out[i] = values[code]
    return out


def build_setup(
    cfg: TwinConfig,
    params: CalibrationParams,
    state: PlantState,
    *,
    month_start: datetime,
    month_end: datetime,
    overrides: Overrides | None = None,
) -> Setup:
    """Compile calibration, state and overrides into model arrays."""
    ov = overrides or Overrides()
    fc = cfg.simulation.forecast
    horizon = build_horizon(
        cfg,
        as_of=state.as_of,
        month_start=month_start,
        month_end=month_end,
        extra_shifts=ov.extra_shifts,
    )
    codes = tuple(params.equipment)
    lines = tuple(params.lines)
    index = {c: i for i, c in enumerate(codes)}
    line_pos = {c: i for i, c in enumerate(lines)}
    loss_w = np.zeros((len(codes), len(lines)))
    for i, code in enumerate(codes):
        eq = params.equipment[code]
        if eq.criticality != "C" and eq.line in line_pos:
            loss_w[i, line_pos[eq.line]] = 1.0 - eq.degraded_capacity
    eqp = [params.equipment[c] for c in codes]

    # open repairs at as_of
    open_kind = np.zeros(len(codes), dtype=np.int64)
    open_elapsed = np.zeros(len(codes))
    open_fixed = np.zeros(len(codes))
    pm_minutes: dict[tuple[str, str], float] = {}
    for pm in cfg.simulation.planned_maintenance:
        pm_minutes[(pm.equipment_type, pm.reason)] = pm.duration_min
    filt = params.filters
    for down in state.open_downs:
        if down.equipment not in index:
            continue
        i = index[down.equipment]
        elapsed = max(0.0, (ensure_utc(state.as_of) - ensure_utc(down.since)).total_seconds() / 60)
        open_elapsed[i] = elapsed
        planned_min = pm_minutes.get((params.equipment[down.equipment].type, down.reason or ""))
        if down.state == "DOWN_PLANNED" and planned_min is not None:
            open_kind[i] = 2
            open_fixed[i] = max(0.0, planned_min - elapsed)
        elif filt is not None and down.equipment in filt.equipment and down.reason == filt.reason:
            open_kind[i] = 3
        else:
            open_kind[i] = 1

    # lines
    lp = [params.lines[c] for c in lines]
    ict = np.array([ov.ict_seconds.get(c, p.ict_seconds) for c, p in zip(lines, lp, strict=True)])
    cf = np.array([p.cycle_factor_mix for p in lp])
    areas = tuple(p.area for p in lp)
    defects = [params.areas[a] for a in areas]
    p_point = np.array([d.rate.mean for d in defects])
    p_scale = np.ones(len(areas))
    p_fixed = np.full(len(areas), math.nan)
    for i, area in enumerate(areas):
        if area not in ov.defect_rate:
            continue
        if p_point[i] > 0:
            p_scale[i] = ov.defect_rate[area] / p_point[i]
        else:  # a zero posterior mean cannot be rescaled: the override is the value
            p_fixed[i] = ov.defect_rate[area]
    buffers = list(params.buffers.values())
    buf_cap = np.array([float(ov.buffer_capacity.get(b.code, b.capacity)) for b in buffers])
    buf_init = np.array([max(0.0, state.buffers.get(b.code, float(b.initial))) for b in buffers])

    # filters
    if filt is not None:
        booth = np.array([index[c] for c in filt.equipment if c in index], dtype=np.int64)
        dp0 = np.array(
            [state.filter_dp.get(c, math.nan) for c in filt.equipment if c in index], dtype=float
        )
    else:
        booth = np.zeros(0, dtype=np.int64)
        dp0 = np.zeros(0)
    policy = ov.filter_policy or (
        cfg.simulation.paint_filters.policy if cfg.simulation.paint_filters else "on_limit"
    )

    # CKD
    supply = cfg.simulation.ckd_supply
    products = tuple(cfg.products)
    ppos = {p: i for i, p in enumerate(products)}
    mix = np.array([cfg.simulation.process.product_mix.get(p, 0.0) for p in products])
    kits = np.array([float(state.kits.get(p, supply.initial_kits.get(p, 0))) for p in products])
    lots = horizon.lots
    transit = state.kits_in_transit
    extra = dict(ov.ckd_delay_days)
    # the next lot of a product (in transit first, else scheduled) carries the extra delay
    transit_extra = np.zeros(len(transit))
    lot_extra = np.zeros(len(lots))
    for product, days in extra.items():
        t_idx = [i for i, lot in enumerate(transit) if lot.product == product]
        if t_idx:
            transit_extra[t_idx[0]] = days
            continue
        s_idx = [i for i, lot in enumerate(lots) if lot.product == product]
        if s_idx:
            lot_extra[s_idx[0]] = days
    ckd = CkdSetup(
        products=products,
        mix=mix,
        kits=kits,
        lot_product=np.array([ppos[lot.product] for lot in lots], dtype=np.int64),
        lot_qty=np.array([float(lot.qty) for lot in lots]),
        lot_dispatch=np.array([lot.dispatched.timestamp() for lot in lots]),
        lot_key=np.array([lot.key for lot in lots], dtype=np.int64),
        lot_extra_days=lot_extra,
        transit_product=np.array([ppos[lot.product] for lot in transit], dtype=np.int64),
        transit_qty=np.array([float(lot.qty) for lot in transit]),
        transit_dispatch=np.array([ensure_utc(lot.dispatched).timestamp() for lot in transit]),
        transit_arrival=np.array(
            [
                ensure_utc(lot.arrival).timestamp() if lot.arrival is not None else math.nan
                for lot in transit
            ]
        ),
        transit_extra_days=transit_extra,
        delay_min=supply.delay_days.min,
        delay_mode=supply.delay_days.mode,
        delay_max=supply.delay_days.max,
        wait_policy=cfg.simulation.process.ckd_shortage_policy == "wait",
    )
    pm_steps = tuple(
        (ev.step, index[ev.equipment], ev.minutes) for ev in horizon.pm if ev.equipment in index
    )
    for key in [*ov.mtbf_multiplier, *ov.mttr_multiplier]:
        if key not in index and key not in CLASSES:
            raise ValueError(f"unknown equipment or class '{key}'")

    return Setup(
        horizon=horizon,
        mtd=state.mtd_output,
        equipment=codes,
        lines=lines,
        areas=areas,
        loss_w=loss_w,
        lam_shape=np.array([e.failures.shape for e in eqp]),
        lam_rate=np.array([max(e.failures.rate_h, 1e-9) for e in eqp]),
        lam_point=np.array([e.failures.per_h for e in eqp]),
        mtbf_mult=_per_unit(dict(ov.mtbf_multiplier), cfg, codes),
        rep_mu=np.array([e.repair.mu for e in eqp]),
        rep_sigma=np.array([e.repair.sigma for e in eqp]),
        mttr_mult=_per_unit(dict(ov.mttr_multiplier), cfg, codes),
        pm_steps=pm_steps,
        open_kind=open_kind,
        open_elapsed=open_elapsed,
        open_fixed=open_fixed,
        rate_per_min=60.0 / (ict * cf),
        eff_alpha=np.array([p.efficiency.alpha for p in lp]),
        eff_beta=np.array([p.efficiency.beta for p in lp]),
        p_alpha=np.array([d.rate.alpha for d in defects]),
        p_beta=np.array([d.rate.beta for d in defects]),
        p_point=p_point,
        p_scale=p_scale,
        p_fixed=p_fixed,
        repaint=np.array([p.repaint_share for p in lp]),
        scrap=np.array([d.scrap_share for d in defects]),
        rw_stations=np.array([float(p.rework_stations) for p in lp]),
        rw_min=np.array([p.rework_mean_min for p in lp]),
        buf_cap=buf_cap,
        buf_init=buf_init,
        held_init=np.array([max(0.0, state.held.get(c, 0.0)) for c in lines]),
        booth=booth,
        dp0=dp0,
        dp_start=filt.dp_start_pa if filt else 0.0,
        dp_limit=filt.dp_limit_pa if filt else 1.0,
        rate_mean=filt.rate_mean_pa_h if filt else 1.0,
        rate_sd=filt.rate_sd_pa_h if filt else 0.0,
        swap_mu=filt.replacement.mu if filt else 0.0,
        swap_sigma=filt.replacement.sigma if filt else 0.0,
        filter_policy=policy,
        service_loss_min=fc.predictive_filter.service_loss_min,
        ckd=ckd,
        uncertainty=fc.parameter_uncertainty,
        extra_shift_count=len(horizon.extra_shifts),
        overrides=ov,
    )


def _lognormal_residual(
    u: FloatArray, mu: FloatArray, sigma: FloatArray, elapsed: FloatArray
) -> FloatArray:
    """Remaining minutes of a lognormal duration D given D > elapsed (inverse CDF of ``u``)."""
    with np.errstate(divide="ignore"):
        z0 = (np.log(np.maximum(elapsed, 1e-9)) - mu) / sigma
    f0 = ndtr(z0)
    q = np.minimum(f0 + u * (1.0 - f0), 1.0 - 1e-12)
    total = np.exp(mu + sigma * ndtri(q))
    return np.asarray(np.maximum(total - elapsed, 0.0), dtype=np.float64)


def _triangular_ppf(u: FloatArray, a: float, c: float, b: float) -> FloatArray:
    if b <= a:
        return np.full_like(u, a)
    fc = (c - a) / (b - a)
    left = a + np.sqrt(u * (b - a) * (c - a))
    right = b - np.sqrt((1.0 - u) * (b - a) * (b - c))
    return np.asarray(np.where(u < fc, left, right), dtype=np.float64)


def _triangular_cdf(x: float, a: float, c: float, b: float) -> float:
    if b <= a or x >= b:
        return 1.0
    if x <= a:
        return 0.0
    if x <= c:
        return (x - a) ** 2 / ((b - a) * (c - a)) if c > a else 0.0
    return 1.0 - (b - x) ** 2 / ((b - a) * (b - c)) if b > c else 1.0


def _arrival_steps(setup: Setup, n_runs: int, seed: int) -> tuple[IntArray, FloatArray, IntArray]:
    """Per lot (scheduled then in transit): product, qty and the arrival step per run (R, lots)."""
    ck = setup.ckd
    h = setup.horizon
    arrivals: list[FloatArray] = []
    for i in range(ck.lot_qty.shape[0]):
        g = generator(seed, Stream.CKD_LOT, int(ck.lot_key[i]))
        delay = g.triangular(
            ck.delay_min, ck.delay_mode, max(ck.delay_max, ck.delay_mode + 1e-9), n_runs
        )
        arrivals.append(ck.lot_dispatch[i] + (delay + ck.lot_extra_days[i]) * _DAY_S)
    g_t = generator(seed, Stream.CKD_TRANSIT, 0)
    as_of = h.as_of.timestamp()
    for i in range(ck.transit_qty.shape[0]):
        u = g_t.random(n_runs)
        if math.isnan(ck.transit_arrival[i]):
            elapsed_d = max(0.0, (as_of - ck.transit_dispatch[i]) / _DAY_S)
            f0 = _triangular_cdf(elapsed_d, ck.delay_min, ck.delay_mode, ck.delay_max)
            q = f0 + u * (1.0 - f0)
            delay = _triangular_ppf(q, ck.delay_min, ck.delay_mode, ck.delay_max)
            arr = ck.transit_dispatch[i] + delay * _DAY_S
        else:
            arr = np.full(n_runs, ck.transit_arrival[i])
        arrivals.append(arr + ck.transit_extra_days[i] * _DAY_S)
    products = np.concatenate([ck.lot_product, ck.transit_product]).astype(np.int64)
    qty = np.concatenate([ck.lot_qty, ck.transit_qty])
    if not arrivals:
        return products, qty, np.zeros((n_runs, 0), dtype=np.int64)
    arr_all = np.stack(arrivals, axis=1)  # (R, lots)
    steps = np.searchsorted(h.step_start, arr_all, side="left").astype(np.int64)
    return products, qty, steps


_OFFSET_SCALE = float(1 << 24)


def flow_step(
    cap: FloatArray,
    buffers: FloatArray,
    held: FloatArray,
    capacity: FloatArray,
    pass_f: FloatArray,
    queue_f: FloatArray,
    ret: FloatArray,
    first_limit: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """One step of the fluid flow through lines ``0..L-1`` with buffers between them.

    Args:
        cap: (runs, lines) capacity of each line in the step, units.
        buffers: (runs, lines - 1) buffer levels before the step.
        held: (runs, lines) bodies in each line's repair queue before the step; they hold
            their place in the downstream buffer (so more defects never add storage).
        capacity: (lines - 1,) buffer capacities.
        pass_f: (runs, lines) share of a line's first passes that goes downstream now.
        queue_f: (runs, lines) share that goes to the line's repair queue.
        ret: (runs, lines) repaired bodies released into the downstream buffer this step
            (``ret <= held``).
        first_limit: (runs,) material available to the first line (CKD kits).

    Returns:
        ``(x, buffers_after, finished)``: first passes per line, new buffer levels and the
        last line's output (finished units).

    Forward pass (material), backward pass (blocking: buffer level + held repairs may not
    exceed the capacity), forward pass again (SPEC §10.2).
    """
    n_lines = cap.shape[1]
    x = np.empty_like(cap)
    x[:, 0] = np.minimum(cap[:, 0], first_limit)
    for k in range(1, n_lines):
        x[:, k] = np.minimum(
            cap[:, k], buffers[:, k - 1] + x[:, k - 1] * pass_f[:, k - 1] + ret[:, k - 1]
        )
    for k in range(n_lines - 2, -1, -1):
        # occupancy after the step = max(B + held + x(pass + queue) - x_next,
        #                                held - ret + x queue)  (the latter if x_next is
        # cut by material later) — both must stay within the capacity
        room = capacity[k] - buffers[:, k] - held[:, k] + x[:, k + 1]
        limit = np.maximum(room, 0.0) / (pass_f[:, k] + queue_f[:, k])
        q = queue_f[:, k]
        q_room = np.maximum(capacity[k] - held[:, k] + ret[:, k], 0.0)
        q_limit = np.where(q > 0, q_room / np.maximum(q, 1e-12), np.inf)
        x[:, k] = np.minimum(x[:, k], np.minimum(limit, q_limit))
    for k in range(1, n_lines):
        x[:, k] = np.minimum(
            x[:, k], buffers[:, k - 1] + x[:, k - 1] * pass_f[:, k - 1] + ret[:, k - 1]
        )
    inflow = x * pass_f + ret
    after = np.maximum(buffers + inflow[:, : n_lines - 1] - x[:, 1:], 0.0)
    return x, after, inflow[:, n_lines - 1]


def _poisson_cuts(m: FloatArray) -> tuple[FloatArray, FloatArray, FloatArray]:
    """CDF of Poisson(m) at 0, 1, 2 (u above a cut adds one failure; at most 3 per step)."""
    e0 = np.exp(-m)
    c1 = e0 * (1.0 + m)
    c2 = c1 + e0 * m * m * 0.5
    return e0, c1, c2


def _full_step(h: Horizon) -> float:
    """The most common step length (all steps but partial ones)."""
    if h.n_steps == 0:
        return 0.0
    return float(np.max(h.step_min))


def simulate(setup: Setup, n_runs: int, seed: int) -> Paths:
    """Run the fast model ``n_runs`` times (see the module docstring)."""
    h = setup.horizon
    R = n_runs
    E = len(setup.equipment)
    L = len(setup.lines)
    S = h.n_steps
    nb = setup.booth.shape[0]

    # ---- per-run parameters
    g = generator(seed, Stream.LAMBDA)
    if setup.uncertainty:
        lam = g.gamma(np.maximum(setup.lam_shape, 0.0), 1.0, size=(R, E)) / setup.lam_rate
    else:
        lam = np.broadcast_to(setup.lam_point, (R, E)).copy()
    lam_h = lam / setup.mtbf_mult

    g = generator(seed, Stream.DEFECTS)
    if setup.uncertainty:
        p = g.beta(setup.p_alpha, setup.p_beta, size=(R, L))
    else:
        p = np.broadcast_to(setup.p_point, (R, L)).copy()
    p = np.clip(p * setup.p_scale, 0.0, _MAX_P)
    fixed = ~np.isnan(setup.p_fixed)
    if fixed.any():
        p[:, fixed] = setup.p_fixed[fixed]
    sc = setup.scrap
    rs = setup.repaint
    pass_f = (1.0 - p) + p * rs * (1.0 - sc)
    queue_f = p * (1.0 - rs) * (1.0 - sc)
    rework_f = p * (1.0 - sc)
    cap_f = 1.0 - rs * p * (1.0 - sc)

    n_shifts = len(h.shifts)
    eff = np.empty((max(n_shifts, 1), R, L))
    for j in range(n_shifts):
        ge = generator(seed, Stream.EFFICIENCY, int(h.shift_keys[j]))
        eff[j] = ge.beta(setup.eff_alpha, setup.eff_beta, size=(R, L))

    # filters
    if nb:
        gr = generator(seed, Stream.FILTER_RATE)
        rates = gr.normal(setup.rate_mean, setup.rate_sd, size=(R, nb, FILTER_LIVES))
        rates = np.maximum(rates, 0.2 * setup.rate_mean)
        gs = generator(seed, Stream.FILTER_SWAP)
        swaps = np.exp(setup.swap_mu + setup.swap_sigma * gs.standard_normal((R, nb, FILTER_LIVES)))
        g0 = generator(seed, Stream.FILTER_START)
        u0 = g0.random((R, nb))
        dp = np.where(
            np.isnan(setup.dp0),
            setup.dp_start + u0 * (setup.dp_limit - setup.dp_start),
            np.minimum(setup.dp0, setup.dp_limit),
        )
        life = np.zeros((R, nb), dtype=np.int64)
        r_idx = np.arange(R)[:, None]
        b_idx = np.arange(nb)[None, :]
        rate_now = rates[:, :, 0].copy()
    else:
        rates = swaps = np.zeros((R, 0, FILTER_LIVES))
        dp = np.zeros((R, 0))
        life = np.zeros((R, 0), dtype=np.int64)
        r_idx = np.arange(R)[:, None]
        b_idx = np.zeros((1, 0), dtype=np.int64)
        rate_now = np.zeros((R, 0))
    predictive = setup.filter_policy == "predictive_shift_change"

    # repairs in progress
    carry = np.zeros((R, E))
    kinds = setup.open_kind
    if (kinds > 0).any():
        go = generator(seed, Stream.OPEN_REPAIR)
        u = go.random((R, E))
        for i in np.flatnonzero(kinds):
            if kinds[i] == 2:
                carry[:, i] = setup.open_fixed[i]
            else:
                if kinds[i] == 3:
                    mu_i, sigma_i = setup.swap_mu, setup.swap_sigma
                else:
                    mu_i, sigma_i = float(setup.rep_mu[i]), float(setup.rep_sigma[i])
                carry[:, i] = _lognormal_residual(
                    u[:, i],
                    np.full(R, mu_i),
                    np.full(R, sigma_i),
                    np.full(R, setup.open_elapsed[i]),
                )
                if kinds[i] == 3 and nb:
                    b = int(np.flatnonzero(setup.booth == i)[0])
                    dp[:, b] = setup.dp_start

    pm_by_step: dict[int, list[tuple[int, float]]] = {}
    for step_i, unit_i, minutes in setup.pm_steps:
        pm_by_step.setdefault(step_i, []).append((unit_i, minutes))

    # CKD
    ck = setup.ckd
    kits = np.broadcast_to(ck.kits, (R, ck.kits.shape[0])).copy()
    lot_prod, lot_qty, lot_step = _arrival_steps(setup, R, seed)
    lot_active = [
        (int(lot_step[:, i].min()), int(lot_step[:, i].max()), i) for i in range(lot_qty.shape[0])
    ]
    mix = ck.mix
    mix_pos = mix > 0

    B = np.broadcast_to(setup.buf_init, (R, L - 1)).copy()
    K = setup.buf_cap
    Q = np.broadcast_to(setup.held_init, (R, L)).copy()
    fg = np.zeros(R)
    rework = np.zeros((R, L))
    days = len(h.days)
    daily = np.empty((days, R))
    day_marks: dict[int, list[int]] = {}
    for d_i in range(days):
        day_marks.setdefault(int(h.day_last_step[d_i]), []).append(d_i)
    for d_i in day_marks.get(-1, []):
        daily[d_i] = setup.mtd

    mu = setup.rep_mu
    sigma = setup.rep_sigma
    full_step = _full_step(h) or 60.0
    e0_full, c1_full, c2_full = _poisson_cuts(lam_h * (full_step / 60.0))
    mttr = setup.mttr_mult
    loss_w = setup.loss_w
    rate_min = setup.rate_per_min

    for s in range(S):
        step = float(h.step_min[s])
        gap = float(h.gap_min[s])
        if gap > 0:
            np.maximum(carry - gap, 0.0, out=carry)
        for unit_i, minutes in pm_by_step.get(s, ()):
            carry[:, unit_i] += minutes

        # failures: N ~ Poisson(lambda x step) by the inverse CDF of u; duration from v
        gs_ = generator(seed, Stream.FAILURES, int(h.slot[s]))
        u = gs_.random((R, E))
        v = gs_.random((R, E))
        if step == full_step:
            e0, c1, c2 = e0_full, c1_full, c2_full
        else:
            e0, c1, c2 = _poisson_cuts(lam_h * (step / 60.0))
        cd = np.minimum(carry, step)
        carry -= cd
        down = cd
        hit = np.flatnonzero(u > e0)
        if hit.size:
            uh = u.ravel()[hit]
            nn = 1.0 + (uh > c1.ravel()[hit]) + (uh > c2.ravel()[hit])
            unit = hit % E
            dur = nn * np.exp(mu[unit] + sigma[unit] * ndtri(v.ravel()[hit])) * mttr[unit]
            # start: uniform offset (low-order bits of u, independent of the rate), after the
            # unit's current repair
            start = np.maximum((uh * _OFFSET_SCALE) % 1.0 * step, cd.ravel()[hit])
            nd = np.clip(np.minimum(dur, step - start), 0.0, None)
            carry.ravel()[hit] += dur - nd
            down = cd.copy()
            down.ravel()[hit] += nd

        if nb:
            db = down[:, setup.booth]
            op = np.maximum(step - db, 0.0)
            dp_new = dp + rate_now * op / 60.0
            limit_hit = dp_new >= setup.dp_limit
            if limit_hit.any():
                t_hit = np.clip((setup.dp_limit - dp) / rate_now * 60.0, 0.0, None)
                swap = swaps[r_idx, b_idx, np.minimum(life, FILTER_LIVES - 1)]
                avail = np.maximum(op - t_hit, 0.0)
                in_step = np.where(limit_hit, np.minimum(swap, avail), 0.0)
                rest = np.where(limit_hit, swap - in_step, 0.0)
                down[:, setup.booth] += in_step
                carry[:, setup.booth] += rest
                life = life + limit_hit.astype(np.int64)
                dp_new = np.where(limit_hit, setup.dp_start, dp_new)
                rate_now = np.where(
                    limit_hit, rates[r_idx, b_idx, np.minimum(life, FILTER_LIVES - 1)], rate_now
                )
            dp = dp_new

        loss = np.minimum(down @ loss_w, step)
        cap = (step - loss) * rate_min * eff[h.shift_index[s]] * cap_f

        # CKD deliveries and the first line's material limit
        for lo, hi, lot_i in lot_active:
            if lo <= s <= hi:
                arrived = lot_step[:, lot_i] == s
                if arrived.any():
                    kits[:, lot_prod[lot_i]] += np.where(arrived, lot_qty[lot_i], 0.0)
        if ck.wait_policy:
            avail_kits = np.min(kits[:, mix_pos] / mix[mix_pos], axis=1)
        else:
            avail_kits = kits.sum(axis=1)

        # rework returns (one-step lag)
        rw_cap = np.where(setup.rw_stations > 0, setup.rw_stations * step / setup.rw_min, np.inf)
        ret = np.minimum(Q, rw_cap)

        x, B, fg_add = flow_step(cap, B, Q, K, pass_f, queue_f, ret, avail_kits)
        fg += fg_add
        Q += x * queue_f - ret
        rework += x * rework_f

        # kits consumed by the first line
        x0 = x[:, 0]
        if ck.wait_policy:
            kits -= x0[:, None] * mix[None, :]
        else:
            want = x0[:, None] * mix[None, :]
            take = np.minimum(want, kits)
            short = x0 - take.sum(axis=1)
            rest_k = kits - take
            total_rest = rest_k.sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                extra = np.where(
                    total_rest[:, None] > 0, rest_k * (short / total_rest)[:, None], 0.0
                )
            kits = rest_k - extra
        np.maximum(kits, 0.0, out=kits)

        if nb and predictive and h.shift_last[s] and h.next_shift_h[s] > 0:
            hours_left = (setup.dp_limit - dp) / rate_now
            swap_now = hours_left < h.next_shift_h[s]
            if swap_now.any():
                life = life + swap_now.astype(np.int64)
                dp = np.where(swap_now, setup.dp_start, dp)
                rate_now = np.where(
                    swap_now, rates[r_idx, b_idx, np.minimum(life, FILTER_LIVES - 1)], rate_now
                )
                if setup.service_loss_min > 0:
                    carry[:, setup.booth] += np.where(swap_now, setup.service_loss_min, 0.0)

        for d_i in day_marks.get(s, ()):
            daily[d_i] = setup.mtd + fg

    return Paths(total=setup.mtd + fg, daily=daily, rework=rework)
