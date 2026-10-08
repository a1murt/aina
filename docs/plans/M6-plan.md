# M6 plan: forecast — calibration, fast Monte Carlo, what-if, levers, effect. SPEC §10, §12.2, FR-FC-01..03, NFR-02, T-FC

Status: approved and implemented (07.10.2026). Backend only; the /director what-if UI comes after M4/M5. No Alembic migration: `forecast_run` and `calibration_snapshot` (rev 0002) are enough. Decisions on Q1–Q12 and deviations are recorded in `docs/PROGRESS.md` (Решения, M6). Deviations: AL-P1 got a pure evaluator `AlertEvaluator.plan_risk` and the switch `rules.yaml: thresholds.plan_risk_target` (default `line_plan`); levers also return `shifts_needed_for_target` per candidate set (`saturdays`, `weekends_and_holidays`); repair queues hold their place in the downstream buffer (keeps FR-FC-03 monotone); work in progress inside lines (`PlantState.held`) is part of the state; per-run monotonicity in MTBF holds up to 0.5 car except paint booths (filter wear grows with operating time; P50/mean are monotone); the validation runs as `python -m qost_sim.forecast_validation` (60 seeds) instead of `tools/forecast_validate.py`.

**Principles.** `twin_core.forecast` is pure: numpy/scipy, mypy strict, no I/O, and "now" is a parameter. Every plant number comes from `TwinConfig`. The model never sees M3 internals. It takes typed `CalibrationInputs` (history facts) and `PlantState` (now). Adapters build them from the DB (api), from sim records (validation, fixtures), and later from M3 `live:*`.

**Measured on this Mac (Apple M4).** A numpy prototype of the core loop (5 000 runs × 240 h × 14 units; Poisson, lognormal, 3-pass fluid) takes **0.29 s**. The SimPy sim without telemetry runs 01.09 → 01.11 in 0.5 s per seed. Its October total for seeds 1–3 is 4 745–4 773, close to the naive 4 746.

## 1. Module layout
- **`twin_core/forecast/`:** `params.py` (pydantic frozen `CalibrationParams`, `PlantState`, `Targets`), `calibration.py` (`CalibrationInputs`, `calibrate()`), `horizon.py` (calendar → steps), `rng.py` (keyed CRN streams), `model.py` (`Setup` arrays, `simulate()`), `overrides.py`, `result.py`, `levers.py`, `effect.py`.
- **`twin_core/schedule.py`:** PM due list, CKD delivery days and lots, working-day index. These move out of `qost_sim.model.plant/ckd` and the sim calls the helper; the existing sim determinism and scenario tests guard the behaviour (Q12). `FINISHED_RESULTS = {pass, rework_pass}` moves to `twin_core.events`.
- **`qost_api/forecast/`:** `data.py` (SQL → `CalibrationInputs`, `PlantState`, `Targets`) and `service.py` (snapshot get-or-create, caches, thread offload, persistence, audit). Routes go in `routes/forecast.py`, `routes/calibration.py`, `routes/effect.py`.
- **`qost_sim/forecast_inputs.py`:** sim records → `CalibrationInputs`, `PlantModel` → `PlantState`. Same contract as the DB adapter. Also `tools/forecast_validate.py` and `make forecast-validate`.

## 2. Calibration (§10.1) → `CalibrationParams` (every value carries `n` and `source: data|prior`)
- **Window:** the last `window_days` (20) complete working days before the as-of day, so one snapshot serves the whole day.
- **`CalibrationInputs`** are raw §8 facts only, with no dependency on M3 `kpi_shift` versions:
  - `operating_h[eq]`: Σ overlap of RUNNING `equipment_state` with the window (SQL aggregate).
  - `stops`: `downtime` rows of config equipment with `microstop=false`, including planned rows and reasons; operator reclassification (FR-ENG-03) is respected.
  - Per (line, shift): `apt_s` (in-shift `RUNNING|DEGRADED|DOWN_UNPLANNED<threshold`), `degraded_s`, `pri_pq_s` from first exits (shift windows go to SQL as `unnest` arrays; the `shift` table is M4). Per line: PQ, defects and code counts from `unit_event`.
- **λ[eq]:** failures are unplanned stops except the filter-replacement reason, because filters are modelled separately. With n ≥ 3, λ = n/T and per-run draws use Gamma(n, T). Otherwise use Gamma(α0 + n, α0/λ0 + T), with λ0 = 1/`mtbf_h` (+ `chain_break`) and α0 = 2.
- **Repair (μ, σ):** lognormal of failure durations. Fallback: per equipment (n ≥ 3) → pooled by type → prior `failures[type].mttr`.
- **Line speed efficiency per shift:** (`pri_pq_s` + PRI × repaint defects) / (`apt_s` − `degraded_s` × (1 − min dc of B units)). It is net of the losses the model applies explicitly, so they are not counted twice (Q1); microstops stay inside eff. Beta by method of moments over shifts with APT ≥ 60 min, c clamped to [10, 10⁴]. With fewer than 5 shifts or v = 0, use a prior built from `cycle_noise` and microstops.
- **Defects[area]:** Beta(1 + d, 1 + PQ − d) as in SPEC; with PQ < 200, a prior around `defects[area].base`. `repaint_share`, rework stations/minutes (weighted by the window's code mix) and scrap share come from config.
- **Filters:** lives between `MT-FILTER` stops give rate = (limit − start)/operating hours. With ≥ 3 lives use data, else prior `dp_rate_pa_per_h`; replacement duration the same way. Buffers, PM plan and CKD supply come from config.
- **Persistence:** `calibration_snapshot(ts = Clock.now(), window_days, params incl. window, config hash, counts)`. Get-or-create key is (window_to, window_days, config hash); each insert writes audit `calibration.create`.

## 3. Current state → `PlantState` (adapter boundary to M3)
- **Fields:** `as_of`, `month`, `mtd_output`, `buffers{code: level}`, `open_downs{eq: (state, reason, since)}`, `filter_dp{booth}`, `kits{product}`, `kits_in_transit`, `warnings`.
- **DB adapter** (now; one indexed query per item):
  - MTD: `unit_event` on the last flow line with `FINISHED_RESULTS` since the local month start. The same function serves M4 `/plan/progress`.
  - Latest `buffer_level`, `ckd_stock`, and `telemetry` of `paint_filters.signal`; open `DOWN_*` intervals from `equipment_state` (`end_ts IS NULL`).
  - In-transit lots are inferred from the schedule (dispatched within `delay.max` days, no stock jump since). Stale or missing data falls back to the config default and adds a warning.
- **Sim adapter:** exact state from `PlantModel`. **At the M3 merge:** optional `state_from_live(read_snapshot())` for buffers and open downs. The DB path works as soon as the engine fills §8; the integration test is then re-run on real engine output.

## 4. Fast model (§10.2) — `simulate(setup, n_runs, seed)`
- **Horizon:** working steps of at most `step_min` (60) from as_of to the local month end (first step may be partial); `extra_shifts` are merged into the calendar's `extra_working_days` (union). Per step: minutes, the non-working `gap_min` before it (repairs continue off-shift, as in the sim), day/shift/hour slot, shift-end flag. PM minutes per (step, unit) come from `schedule.pm_due`; CKD lots from the plan.
- **Drawn once per run:** λ (÷ `mtbf_multiplier`), p[area], filter rate and swap duration per (booth, life), CKD delays (+ `ckd_delay_days`). Open repairs get a lognormal residual conditioned on D > elapsed (inverse CDF); PM residuals are deterministic.
- **Each step, vectorised over runs × units:**
  1. Carry −= gap.
  2. N ~ Poisson(λΔt) by inverse CDF of one uniform. Duration = N·exp(μ + σz)·`mttr_multiplier`, starting at an independent uniform offset; the remainder goes to carry (repairs are serial).
  3. Booth dp += rate × operating minutes; at the limit the swap goes to carry. Under `predictive_shift_change`, a booth that would hit the limit next shift is swapped at the shift end for `service_loss_min` (Q2).
  4. Line loss = min(step, Σ down × (1 − dc) + PM).
  5. Cap = (step − loss) × 60/(ICT × cf_mix) × eff[line, shift]; PAINT is also multiplied by (1 − repaint_share × p).
- **Fluid flow:**
  - x₁ ≤ Cap₁ and kits (Σ under `resequence`, min over products of kits/mix under `wait`). Forward xₖ = min(Capₖ, Bₖ₋₁ + inₖ₋₁), backward xₖ = min(xₖ, Kₖ − Bₖ + xₖ₊₁ − retₖ), then forward again; Bₖ += inₖ − xₖ₊₁.
  - inₖ = xₖ(1 − p) + repaint share + `ret`, where `ret` is a rework-station fluid queue (stations × step/rework_min) returned one step later; scrap = x·p·scrap_share. QC-1 output to FG = finished cars; total = MTD + Σ. Daily cumulative output per run and rework per area are kept.
- **CRN:** draws come from `Generator(PCG64(SeedSequence([seed, stream, slot])))`. Slots are calendar keys (hour since month start, shift instance, booth life, lot), so extra-shift, ICT or buffer overrides leave all other draws unchanged. Base and scenario share one seed (default `clock.random_seed`). λ↓, p↓, K↑ and Cap↑ never lower any run's output (min-plus monotone); this is tested pathwise.

## 5. Overrides FR-FC-02 (`Overrides`, pydantic `extra="forbid"`)
- **`defect_rate{area: 0..0.5}`:** a new mean; each run's draw is rescaled, which keeps the spread and stays monotone.
- **`mtbf_multiplier{equipment|A|B|C: (0, 1000]}`, `mttr_multiplier{…: (0, 10]}`:** an equipment key beats a class key; the MTTR multiplier applies to new failures only.
- **Other fields:** `ict_seconds{line: 30..3600}`; `buffer_capacity{buffer: 0..500}`; `extra_shifts[{date, shifts?}]` (in the month, after as_of, unique, not already worked); `filter_policy`; `ckd_delay_days{product: 0..31}` (next lot only, like S5; Q7).
- **Errors:** codes are checked against config with `aliases.closest_code` suggestions → 422 `validation` with `errors[{loc, msg, type}]`.

## 6. Result (`forecast_run.result`, returned by POST and GET)
- **Context:** `month`, `as_of`, `seed`, `n_runs`, `horizon{shifts, hours, extra_shifts}`, `mtd`, `targets{plant_target, line_plan}` (from `production_plan`, else `plant.yaml`), `required_rate` (via `kpi.required_rate`).
- **Distribution:** `summary{p10, p50, p90, mean, sd}`, `p_reach{plant_target, line_plan}`, `expected_shortfall`, `histogram{edges, counts}`, `fan[{date, p10, p50, p90, plan_cum}]`, `rework_expected{area}`.
- **Provenance and comparison:** `calibration_id`, `state` digest, normalised `overrides`, `warnings`. With overrides, also `base{summary, p_reach}` and `delta{p50, mean, p_reach, paired p10/p90}`.
- **No UI text in the API:** levers are a kind plus params, rendered via next-intl.

## 7. Levers §10.4 and effect §10.5
- **Levers** (`GET /forecast/levers?month`) are expressed as public overrides, so «Применить в сценарии» works: `mtbf_multiplier[eq] = 1000` per class A/B unit; `defect_rate[area] = 0.02` for each area above `defect_rate_limit`; `filter_policy = predictive_shift_change`; +1 shift on the first non-working Saturday after as_of (shift A); `buffer_capacity[PBS] + 10`.
- **Lever output:** Δ P50, Δ mean, Δ P(target), Δ P(line plan), and ₸ by the §10.5 formula over the remaining horizon. Levers are ranked by mean ₸ and the top 5 are flagged.
- **Lever runs:** 2 000 per lever, CRN against one base; about 16 levers take about 2 s. Results are cached in-process by (month, calibration id, state digest, seed, hour) and not persisted.
- **Effect** (`POST /effect {month, scenario?, assumptions?}`):
  - Default scenario «с системой» = `improvement_defaults`: MTTR × 0.85 for A/B/C, MTBF × 1/0.8 for A and B, `filter_policy`, and `defect_rate[PAINT] = min(calibrated, 0.03)`.
  - Horizon is a **full typical month** (as_of = month start, config initial buffers, MTD 0; Q4), so ×12 is meaningful. Per paired run: Δcars × price × margin + Σ Δrework[area] × cost − extra shifts × shift cost.
  - Returns month {p10, p50, p90, mean}, year ×12, components, and assumption flags/sources from `business.yaml`; no revenue field (`show_revenue=false`).

## 8. API (dev `require_roles`, replaced by JWT in M4; errors RFC 7807)
- **`POST /api/v1/forecast`** (director, admin) `{mode, month?, overrides, n_runs?, seed?}`:
  - `des` → 501 `not-implemented` (P1, M9). A past month → 422 `forecast-month`. A future month starts at the month start with current buffer levels.
  - Numpy runs in `anyio.to_thread` behind a semaphore of 2; the base run is LRU-cached, so a what-if request usually computes only the scenario. Persists `forecast_run` (status=done, progress=1, result, duration_ms, created_by) and writes audit `forecast.run`.
- **Other routes:** `GET /forecast/levers?month` (director, admin; declared before `/{id}`), `GET /forecast/{id}` (director, admin; 404 if missing), `GET /calibration` (director, admin, maintenance; snapshot params + id), `POST /effect` (director, admin).
- **Infrastructure:** without a DB → 503 `no-database`. Data access goes through a `ForecastDataSource` protocol on `app.state`; unit tests inject a sim-fixture source.

## 9. Config: optional `simulation.yaml: forecast` (crossref-validated; no new dependencies)
`window_days: 20`, `step_min: 60`, `seed: null`, `n_runs: {default: 5000, max: 20000, levers: 2000}`, `parameter_uncertainty: true`, `priors: {prior_strength: 2, min_failures: 3, min_repairs: 3, eff_min_shifts: 5, eff_concentration: 200, defect_min_units: 200, defect_concentration: 100, filter_min_lives: 3}`, `predictive_filter: {service_loss_min: 0}`, `histogram_bins: 30`, `levers: {failure_free_multiplier: 1000, extra_shift: {weekday: 6, shifts: [A]}, buffer: {code: PBS, add: 10}, top: 5, rank_by: effect_kzt}`, `max_concurrent: 2`.

## 10. Validation against the sim (stand-in for the §10.3 AC)
- **Run:** `tools/forecast_validate.py --seeds 30`. Per seed: run the sim from 01.09 to as_of; calibrate on its last 20 working days and take the exact `PlantState`; fast forecast with 5 000 runs; continue the sim to 01.11 for the actual output.
- **Report and acceptance:** two as_of points (01.10 00:00 and demo_start 16.10 07:00). It shows bias as a share of the **remaining** output, P10–P90 coverage, and timing. Pass: |bias| ≤ 3% and coverage 65–95%.
- **Where it runs:** under a new marker `validation` (about 30 s; `make test`, not `make check`), with the numbers recorded in PROGRESS. `make check` keeps a deterministic 1-seed check at ±5%.
- **If the bias is larger:** I stop and report. Options are `step_min: 30` (about 0.6 s) or revisiting the eff/rework assumptions.
- **Golden-ish:** at month start with MTD 0, `required_rate` = 114.2857 / 130.9524 (golden `plan`). Degenerate params (λ = 0, p = 0, all lines at 113.0/shift) from 01.10 → 4 746 ± 1 (golden `naive_month_projection`). P(5 500) = 0 when the ceiling is 123.6 × 42 = 5 191.

## 11. Tests (`tests/forecast/`, `tests/services/test_api_forecast.py`, `tests/integration/test_forecast_api.py`)
- **Calibration:** 0/2/3+ failures → prior/posterior/data; repair fallback chain; method of moments with v = 0 and the c clamp; the defect prior path; filter lives. 20 sim days recover config-level λ, `calibration_targets` defect rates, and a filter rate of 7 ± 1.
- **Model:**
  - Fluid invariants with hypothesis (0 ≤ B ≤ K, x ≤ Cap, conservation); determinism by seed; CRN: an unrelated override leaves results bit-identical.
  - **FR-FC-03** (P50 and pathwise): PAINT 0.03 → 0.06 ⇒ P50 not ↑; +shift on 17.10 ⇒ not ↓; MTBF{A: 1.5} ⇒ not ↓; plus hypothesis pairs at 300 runs.
  - **FR-FC-01:** 5 000 runs from 09.10 07:00 (15 working days); warm-up, then median of 3 must be ≤ 1.5 s for base + scenario (`FORECAST_TIMING_BUDGET_S`); the measured value is recorded.
  - Scenario cases: an S1-like open repair lowers P50 by about 55 min of output; S2-like dp 370 → a swap within 12 h; predictive ≥ on_limit.
- **Overrides, levers, effect:** bounds; unknown codes with suggestions; date rules; lever → overrides round-trip; effect on a hand-calculated case; ×12; no revenue key.
- **API** (ASGI + fixture source, in `make check`): 403 for operator/quality (and maintenance on POST), 422 shapes, 501 for des, POST timing.
- **Integration** (own DB `qost_it_m6` at alembic head; never `make up` or compose): load sim-derived rows into §8 tables; the DB adapter must equal the sim adapter; POST persists the run and its audit row; GET by id; the calibration snapshot is reused; levers and effect run end to end.

## 12. Open questions (proposed resolution; all config-switchable)
- **Q1 Efficiency:** use net speed efficiency, not ISO E, so B-degraded and repaint losses are not counted twice. Switch: `eff_basis: net|iso`.
- **Q2 Planned filter swap** at a shift change costs `service_loss_min` = 0, matching the `limits.maintenance_saving` default. This makes the lever worth about 2–3% of PAINT capacity. The stricter alternative is 0 only across non-working gaps.
- **Q3 P(5 500) is structurally ≈ 0.** We show both targets and rank levers by ₸. AL-P1 (engine/M4) should also use `line_plan`, otherwise it stays critical all month.
- **Q4 Horizons:** effect over a full typical month; levers over the remaining horizon.
- **Q5** `defect_rate` is a new mean that keeps the posterior spread. **Q6** Parameter uncertainty is on.
- **Q7** `ckd_delay_days` delays the next lot only.
- **Q8** GET /calibration persists the snapshot as an audited cache side effect (POST /calibration is not in §12.2).
- **Q9** MTD = QC-1 `pass` + `rework_pass` from events, since imports have no QC-1. M4 should reuse the same function.
- **Q10** A lagged rework-station queue (beyond §10.2) keeps extreme defect what-ifs honest.
- **Q11** The shift-B defect factor and wear-dependent hazard are not modelled; the PdM `prediction` hook is deferred to M7b.
- **Q12** Behaviour-preserving sim refactor (PM/CKD schedule → `twin_core.schedule`): is it OK to touch `services/sim` in this branch?

## 13. Order of work
`twin_core.schedule` → params, calibration and sim adapter → horizon, rng, model with FR-FC tests and timing → overrides and result → levers and effect → API data/service/routes with unit tests → integration tests → validation numbers → PROGRESS, `make check`, commit `M6: прогноз — калибровка, Монте-Карло, what-if, рычаги, эффект`.
