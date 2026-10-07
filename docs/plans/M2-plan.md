# M2 plan: virtual plant (`services/sim`). SPEC §6, §5.2–5.7, §7.2, FR-SIM-01..03, T-SIM

Status: approved and implemented (07.10.2026). Decisions on Q1–Q11 and deviations are recorded in `docs/PROGRESS.md` (Решения, M2); CKD shortage policy became `process.ckd_shortage_policy` (default `resequence`), precursors only before wear-reason failures, and an in-process OPC UA smoke test runs in `make check`.

**Principles.** All parameters come from `TwinConfig`. The model is pure SimPy over plant seconds (`env.now`, t0 = run start, UTC), with no wall clock and no I/O. Infra (pacing, OPC UA, MQTT, Redis, HTTP) talks to it only via `run_until(t)`, `drain()`, `apply(inject)` and `snapshot()`.

## 1. Module layout
- `twin_core/events.py` (mypy strict): §7.2 schema with `Event` subclasses discriminated by `kind`, typed `data`, `parse_event`/`dumps`, aware-UTC timestamps with a "Z" suffix.
  - ULID is in-house (Crockford base32, no new dependency): `RandomIds` (collector) and `DeterministicIds(seed, nonce)` = `ULID(ts_ms, blake2b(seed|nonce|seq)[:10])`.
- `twin_core/event_sink.py`: async `EventSink` Protocol (`write(batch)`, `aclose()`) with `MemorySink`, `JsonlSink`, `NullSink`. `open_sink("jsonl:PATH" | "memory:" | "null:")` is a registry; M3/M4 register `db:`, which is how `make demo` plugs in the DB writer.
- `qost_sim/model/`: `rng`, `expr` (telemetry expression compiler), `calendar_proc`, `equipment`, `line`, `ckd`, `quality`, `telemetry`, `states` (pure §5.3), `scenarios`, `records` (slotted records → events), `plant` (`PlantModel` facade).
- `qost_sim/`: `address_space` (pure `NodeSpec` list shared by the OPC UA server and the tag map), `opcua_server`, `mqtt_pub`, `live` (`LiveRunner`), `control_api` (FastAPI :8100), `backfill`, `ml_dataset`, `tagmap`, `settings`. `__main__` subcommands: `live` (default) | `backfill` | `ml-dataset` | `tagmap`.

## 2. Process model (SimPy)
- **Calendar process:** toggles `in_shift` at working-shift boundaries (lazy, so live is unbounded) and notifies lines and units.
- **Equipment unit** (one process, sole owner of its state). Operating time = in a shift and not down; wear and hazards accrue only then, as in the sanity model.
  - Wear: Gamma process, updated every operating hour, d += Gamma(shape·Δh, mean/shape), clamped to [0, 1], initial d = `reset_after_repair`.
  - Failures: competing risks with an Exp(1) threshold consumed over operating time, λ_r(d) = share_r/mtbf × (1 + gain·d³ for wear_reasons). `chain_break` has its own wear hazard; microstops have a separate threshold.
  - Booth filters: dp = dp_start + rate·op_h, rate ~ N per filter, initial dp ~ U(start, limit).
  - Next event = min(failure, microstop, filter limit, wear update, shift end). PM, injects and the filter policy arrive by `interrupt()`.
  - Repair: MTTR lognormal in plant time, continuing off-shift. A wear-reason repair sets d = min(d, reset). Microstops are capped below `microstop_threshold_s`. A filter at the limit triggers `MT-FILTER` and a dp reset.
- **Planned maintenance:** at shift `A` start, due when (working-day index since run start + global position of the unit in `plant.yaml`) % every == 0 (sanity-check stagger). `DOWN_PLANNED` with the configured reason; d = max(pm_floor, d − pm_reduction).
- **Line** (work-based): each body needs W = ict × cycle_factor × LN(median_factor, σ) work-seconds at rate c(t) = 0 off-shift, otherwise min `degraded_capacity` of down A/B units. A rate change interrupts only while processing, so there is no per-cycle AnyOf.
  - WELD-1 follows a heijunka sequence (deterministic largest-deficit) and uses one kit per body; with no kit for the sequenced model it is `STARVED`/`MAT-SHORTAGE`.
  - Other lines `get` from the upstream `simpy.Store` (`STARVED`) and `put` downstream (`BLOCKED`). PAINT-1 serves the repaint queue before BIW; QC-1 outputs to FG. `body_id` = `B`+yyMMdd+NNNN.
- **Inspection at line exit:** p = (base + terms) × non-first-shift factor × active multipliers.
  - WELD term: robot_wear_gain × mean d of the area's `robot` units.
  - PAINT terms: dp_gain × clip((max dp − dp_from)/(limit − dp_from)), plus the humidity add when any booth is outside the signal's warn range (humidity = the telemetry expression, with noise from the inspect stream).
  - Disposition: scrap with `scrap_share_of_defects`; otherwise `repaint_share` decides repaint and the type is chosen by weight within the repaint or non-repaint group.
- **Rework:** `simpy.Resource(stations)` per line; duration = `rework_min` (fallback: the line's `minutes_median`), in-shift only; then downstream as `rework_pass`. A repaint body makes a second PAINT-1 pass that consumes capacity and is not re-inspected.
- **CKD:** initial `initial_kits`. Every N working days at shift A start, one lot per product with qty = lot_days × daily plan (month `line_model` plan / working days; fallback: mix × `plan_rate_per_shift`). Arrival delay is triangular in plant days.

## 3. States and emission
- **Equipment** = own condition: `DOWN_*` (with reason) > `IDLE_NO_PLAN` (off-shift) > `RUNNING`.
- **Line** = pure `derive_line_state` per §5.3: off-shift → `IDLE_NO_PLAN`; any A down → `DOWN_*` (reason of the earliest); flow `STARVED`/`BLOCKED` (`MAT-SHORTAGE` for kits); any B down or in PM → `DEGRADED`; else `RUNNING`. Class C is ignored; there is no `CHANGEOVER` (no config for it).
- **Events, on change only:** `alarm` on/off for `DOWN_UNPLANNED` (code = reason, text = `name_ru`); `buffer_level` on every put/get.
- **Counters,** cumulative from run start: Produced = first exits (pass/defect/scrap), Reject = defect+scrap, Good = pass. `rework_pass` is never re-counted; this is the PQ/GQ contract for M3.
- **Outbox:** records are converted by `to_event()` at the output boundary, assigning ids in emission order with `source=sim`, `received_ts=ts`. No pydantic work in the hot path.

## 4. Determinism and RNG (FR-SIM-01)
- One `random.Random(f"{seed}:{stream}")` per stream (string seeds are sha512-based, independent of PYTHONHASHSEED). Streams: `eq:{code}:{fail|micro|wear|repair|filter|static}`, `line:{code}:{cycle|inspect}`, `ckd`, `tele:{code}:{signal}`.
- No stream is shared across entities, so baseline and scenario runs get common random numbers (the k-th unit at a line gets the same draws). Telemetry has separate streams, so its period, or turning it off, does not change the model. No set iteration, no `hash()`, no wall clock in `model/`.
- **Contract:** (config, seed, interventions [(plant_t, inject)], nonce) gives an identical stream. Nonce: `backfill` (idempotent re-runs), `live:{epoch}` (bumped on reset), fixed in tests.

## 5. Modes
- **backfill:** `qost_sim backfill --sink jsonl:PATH [--from --to --seed]`, `backfill_from` → `demo_start`, telemetry every 300 s, batches ≤500. No OPC UA, no Redis.
- **live:** warm-up replays the same model silently from `backfill_from` to `demo_start` in a thread, so its state is bit-identical to the end of backfill (buffers, wear, counters, body numbers). Then it schedules the `at_min` scenarios (S2/S3), starts OPC UA, and writes all nodes with SourceTimestamp = `demo_start`.
  - Pacing: target = anchor_plant + (monotonic() − anchor_wall) × speed, then `run_until`, with a 100 ms tick and capped catch-up. Events → OPC UA (SourceTimestamp = event ts) and MQTT.
  - `plant:clock` in the M0 v1 format (`publish_clock_state`, wall_ts from `twin_core.clock.system_now`) at start/pause/speed/reset and at 1 Hz. `SIM_AUTOSTART` (default true).
  - Restart: interventions and the epoch are kept in Redis (`sim:interventions`, `sim:epoch`) and replayed to the last `plant:clock`.
- **ml-dataset:** `qost_sim ml-dataset --out ml/data/raw`, seed 7, from `ml_dataset.from` for the configured months, telemetry every 300 s. Polars parquet files: `telemetry`, `states` (intervals with reason, planned, microstop, wear flags), `units`, `maintenance`, `oracle_degradation` (documented as never a feature), plus `meta.json`. Windows and labels belong to M7.

## 6. OPC UA, MQTT, tag map
- **asyncua server:** `opc.tcp://0.0.0.0:4840/qost/`, security None; the namespace `urn:qost:twin:plant` must be ns=2 (asserted at startup); all nodes are read-only to clients.
  - Objects: `KST/{AREA}/{LINE}/{EQ}`, `KST/BUFFERS/{CODE}`, `KST/CKD/{PRODUCT}`, `KST/Plant`, NodeIds `ns=2;s=KST.…` per §6.7.
  - Equipment variables: State (Int32 per the `state_enum`), StateSince, AlarmCode, Alarm, telemetry (AnalogItem with EURange and EngineeringUnits).
  - `Degradation` exists only for wearing types; its Description says "hidden oracle (FR-SIM-02)"; never on MQTT or in the tag map. `Plant.Shift` = "YYYY-MM-DD/A" ("" off-shift); `Plant.Speed` = 0 while paused.
- **MQTT (aiomqtt):** QoS 1, signal topics retained. Topics: `qost/v1/KST/{AREA}/{LINE}/{EQ}/{signal}`, line, `BUFFERS/{CODE}`, `CKD/{P}/kits`, `Plant/*`. Payload `{ts,value,quality}`; state uses the same Int32 as OPC UA. `…/{LINE}/units` carries unit and defect Event JSON. A bounded queue and reconnect mean the model never stalls on the broker.
- **`make tagmap`** → `qost_sim tagmap --out config/tag_map.demo.yaml`: the `NodeSpec` list minus Degradation, plus `OPCUA_ENDPOINT` and the MQTT section; validated with `load_tag_map`, committed, with a drift test.

## 7. HTTP pult and reset
- **FastAPI on :8100.** `GET /status`: plant time (UTC and local), shift, speed, paused, runner state, epoch, seed, active and scheduled scenarios, interventions, line states, buffers.
  - `GET /scenarios`, `POST /start`, `/pause`, `/speed {value}` (0 < value ≤ max(presets)).
  - `/inject {scenario_id}` or ad-hoc `{type,…}`: parsed by the config `Inject` union plus crossref checks; errors 404, 422, 409 not ready.
  - `/reset {to:"demo_start"}`; `/healthz`, `/readyz` (ready after warm-up). The compose healthcheck switches to `/readyz`.
- **Reset protocol (proposed):**
  1. Pause, then wait ≥1 s wall for the collector to flush.
  2. Publish Redis `sim:control` `{action:"reset", epoch, demo_start}` and wait for an ack on `sim:control:ack`, with a timeout. The M3 engine deletes ts ≥ `demo_start` from event and derived tables, runs `XTRIM` on `events`, clears `live:*`, and reloads.
  3. Rebuild, warm up, re-schedule `at_min`, republish nodes and the clock, resume.

  M2 builds the sim side; with no listener, the reset times out and continues.

## 8. Scenarios (applied at the current plant time, logged)
- **S1:** CONV-03 forced `DOWN_UNPLANNED`/`ME-CHAIN` for 55 min, preempting any current down, alarm on, wear-reason repair semantics.
- **S2:** `filter_dp_pa` sets the hidden dp (hours_since_change = (v − start)/rate). **S3:** sets d.
- **S4:** product of area multipliers until t + duration. **S5:** sets J7 kits (emits `ckd set`) and delays J7's next lot. Other set_state keys return 422.

## 9. Performance
- Plain `simpy.Environment`, interrupts instead of conditions, stdlib RNG, slotted records, hourly wear updates. Telemetry off in calibration and scenario tests; one session-scoped 25-working-day run shared by the invariant and calibration tests; profile with cProfile.
- Targets: 20 working days ≤ 4 s; warm-up (33 working days) ≤ 6 s; backfill with telemetry ≤ 60 s; ml-dataset ≤ 3 min; sim tests in `make check` ≤ 25 s.

## 10. Tests
**Unit tests (`make check`):**
- `twin_core/test_events.py`: round-trip of every kind, discriminator and naive-ts errors, ULID ordering, deterministic ids, sinks; coverage stays ≥ 90%.
- `sim/test_expr.py`: every YAML expression compiles; the whitelist rejects attributes, unknown names and calls; N, Poisson and precursor semantics.
- `sim/test_states.py`: §5.3 table (each priority, min `degraded_capacity`, C ignored, off-shift).
- `sim/test_model.py` (invariants on the session run): conservation (kits = bodies; WIP = created − FG − scrap); 0 ≤ level ≤ capacity; ts non-decreasing; no repeated identical states; `body_id` format and uniqueness; counters = unit events; microstops below the threshold; PM on schedule with a planned reason; wear reset and PM reduction; no PM for ABB-04, BOOTH-02 or CONV-03 at shift A start on 16.10.
- `sim/test_calibration.py` (FR-SIM-03): seed `clock.random_seed`, 5 working days warm-up + 20 measured. FG/shift within 112–118; defect rates for WELD, PAINT and ASSY within their ranges; unplanned downtime ≥ threshold per area per working day within 5–70 for WELD, PAINT and ASSY (QC only ≤ max); PAINT-1 has the lowest starved+blocked share (bottleneck proxy).
- `sim/test_determinism.py`: two runs with interventions give identical JSONL bytes including event_id, and another seed differs; chunked `run_until` equals a single run; telemetry off/60 s/300 s gives the same non-telemetry stream; the live warm-up state equals the backfill end state; a subprocess with another PYTHONHASHSEED gives the same digest.
- `sim/test_scenarios.py` (common random numbers vs baseline from `demo_start`):
  - S1: 55 min down with alarm, ASSY-1 output 0 in the window, PBS above baseline.
  - S2: dp ≈ 370, then `MT-FILTER` at the predicted operating hour.
  - S3: Degradation 0.75, current and temperature telemetry up, earlier wear failure (over seeds).
  - S4: defects ×1.3–2.0 in the window. S5: J7 → 0, WELD-1 `STARVED`/`MAT-SHORTAGE`, later lot.
  - Also: the `at_min` schedule and ad-hoc inject validation.
- `sim/test_address_space.py`: §6.7 node set, types and metadata; Degradation only on wearing units; the generated tag map validates, has no degradation, and equals the committed file.
- `sim/test_control_api.py`: TestClient with a fake runner. `sim/test_live_pacing.py`: fake monotonic clock and in-memory KV; plant = speed × wall; pause freezes; speed re-anchors; M0 clock JSON.

**Integration (`make test`):**
- `test_sim_opcua.py`: in-process server on a free port. An asyncua client browses `Objects/KST/…`; every tag-map node is readable with the right type; Degradation present; values change; SourceTimestamp = plant time; writes rejected.
- `test_sim_mqtt.py`: unique topic root on localhost:1883; units payloads parse as events.
- `test_sim_live.py`: Redis test key, OPC UA, MQTT and HTTP. `SimClock` ≈ speed; `POST /inject` S1 gives `CONV-03.State`=4 and AlarmCode ME-CHAIN within ≤1 s wall; pause, speed and reset work.

## 11. Config, dependency and contract changes
- **simulation.yaml and schema (optional fields + crossref):** `paint_filters.equipment_type: booth` and `signal: filter_dp_pa`; `defects.PAINT.humidity_signal: humidity_pct`; `degradation.*.pm_floor: 0.05` (today a YAML comment only).
- **Oven expression** → `140 + 8*precursor(2) + N(0, 1.2)`, where `precursor(h)` ramps 0→1 over the last h operating hours before the unit's next pre-drawn failure. Other variables: `d`, `t` (local hour), `pi`, `phase` (per unit, U(0, 2π)), filter variables, earlier signals of the same unit.
- **Dependencies** (all in §4.3): `simpy`, `asyncua`, `aiomqtt`, `fastapi`, `uvicorn`, `polars` (parquet); a mypy override for asyncua may be needed.
- **Repo files:** a one-line fix to M0 `test_tag_map_auto_detection` (drop the committed demo map in the copy); compose sim healthcheck → `/readyz`; Makefile `tagmap`; `.gitignore` `var/`.

## 12. Open questions and risks (proposed resolution first)
- **Q1.** `repaint_share` 0.4 vs repaint-flagged type weights 0.30. Proposal: the share decides repaint and weights choose the type within the group, consistent with the M6 fast model.
- **Q2.** §6.2 says WELD-1 waits for the sequenced model's kit, so S5 can stall the plant for about 2–4 days. Alternative: skip/resequence via `process.ckd_shortage_policy`. Needs a decision.
- **Q3.** AlarmCode carries the current stop reason for any `DOWN_*`, PM included; `Alarm` is true only for `DOWN_UNPLANNED`; the engine maps codes via `aliases`. Line reasons (`MAT-SHORTAGE`) are only in events and MQTT.
- **Q4.** Owner of the reset DB tail cleanup. Proposal: the engine, via `sim:control`, since it must reload anyway.
- **Q5.** Equipment does not mirror line `STARVED`/`BLOCKED`: fewer events and clean equipment A/MTBF. Easy to add if the UI wants it.
- **Q6.** S2 and S3 are steps at `demo_start`; if M7 needs a trend, add an optional `ramp_min`.
- **Q7.** The in-process OPC UA browse test is `integration` per the instruction, so the AC is checked only in `make test`. A 2 s smoke version could stay in `make check`.
- **Q8.** Calibration may land near the 112 lower bound because buffers interact. Tune `simulation.yaml` only; report numbers in PROGRESS. **Q9.** Repairs continue off-shift; rework runs in-shift only; the shift-B factor applies to all non-first shifts; no `CHANGEOVER`.
- **Q10.** At 300× a microstop is about 0.4 s wall, so the M3 collector needs monitored-item queue_size > 1. The sim writes every value with SourceTimestamp.
- **Q11.** `uv.lock` and `docs/PROGRESS.md` will conflict with M1; re-lock and merge by hand.

**Order:** events and sinks → config additions → expr and states → model with calibration and determinism → scenarios → backfill and ml-dataset → address space and tagmap → OPC UA, MQTT, live and pult → integration tests → PROGRESS.
