# M3 plan: collector + engine. SPEC §7.1–7.3, §8, §9, §12.3; AC: T-INT, T-SF, T-RO, T-BN green, p95 ≤ 2 s

Status: approved and implemented (07.10.2026). Decisions and deviations are recorded in `docs/PROGRESS.md` (Решения, M3): stream MAXLEN 300 k (measured 365 B/entry), DQ-04 tolerance 2, bottleneck ties by unclipped period length, collector `received_ts` strictly increasing with replay order `(ts, received_ts, event_id)`, OPC UA report-by-exception vs MQTT republishing, counters mode for units not implemented.

**Principles.** The engine has one pure, synchronous core (`apply(event)`, `advance_to(now)`, `drain() → effects`, `snapshot()/restore()`) with no I/O and no clock. Live and replay are two drivers over it, so history, live, restart recovery and recompute share one code path. Plant numbers come only from `TwinConfig`, "now" only from `Clock`; Degradation is never used.

## 1. Data ownership
| Writer | DB tables | Redis |
|---|---|---|
| `DbSink` (`db:` sink: collector live and `qost_sim backfill --sink db:`) | `event_raw` + 1:1 facts `telemetry`, `unit_event`, `buffer_level`, `ckd_stock`, `defect`; `dq_issue` DQ-07 for ingest problems (collector only) | collector: `XADD events`, `collector:status` |
| engine | `equipment_state`, `downtime` (single writer, Q9), `kpi_shift` (source=events, versions), `bottleneck_shift`, `alert` (live rules), `dq_issue` (DQ-02/04/06, DQ-07 alarm codes), `engine_checkpoint` (new), `audit_log` (KPI re-versions) | `live:*`, `live` channel, `alerts` stream (M8 input), group `engine`, `sim:control:ack` |
| api (M1/M4) | import tables (unchanged); alert ack/resolve; classification = operator event into `events` + audit | reads `live:*`, `plant:clock` |
| sim | — | `plant:clock`, `sim:*` |

## 2. Redis contract (`twin_core.live`: key names, pydantic models of each WS `data`, `read_snapshot()` for M4)
- **`events` stream:** fields `k` (kind), `e` (entity), `j` (event JSON), so the engine skips telemetry by `k` without parsing; `MAXLEN ~EVENTS_STREAM_MAXLEN` (default 1 000 000 per FR-ING-02, memory risk Q6); group `engine` XACKs an entry only after the DB commit holding its effects.
- **Snapshot keys** (together = the §12.3 snapshot). Hashes code → JSON: `live:equipment` (state, since, reason_code, alarm, health_index=null, open downtime), `live:lines` (state, since, pq, gq, plan_to_now, A/E/QR/OEE, shift), `live:areas`, `live:buffers` (level, capacity, minutes_to_full/empty), `live:downtime_open`. Strings: `live:plant` (RTY, FG good MTD), `live:bottleneck`, `live:alerts_open`, `live:meta` (engine plant ts, shift, epoch).
- **`live` channel:** the WS envelope `{"type","ts","data"}`, type ∈ state|unit|buffer|kpi|alert|clock|bottleneck|snapshot; the API forwards and throttles to 2/s; `snapshot` = "re-read the snapshot" (after reset/restart). **`alerts` stream** (MAXLEN ~10 k): new/changed/escalated alerts with recipients and channels.
- **Tests:** Redis DB 15; pub/sub ignores the DB index, so channels also get a per-test prefix (`LIVE_CHANNEL`, `SIM_CONTROL_CHANNEL`).

## 3. twin_core additions (no formula or golden change)
- **`events`:** `DeterministicIds`/`RandomIds` become monotonic within a millisecond (previous + 1), so `(ts, event_id)` order = emission order (e.g. buffer put+get at one instant). New `content_ulid(ts, key)` = ULID(ts_ms, blake2b(key)); the collector's key `entity_type|entity|kind|signal|ts_us|value` gives the same id on reconnect/restart re-delivery and on the OPC UA and MQTT paths.
- **`event_sink`:** `db:` (uses `DATABASE_URL`) or `db:URL`, registered lazily from `twin_core.db.sink`; asyncpg moves into twin_core's deps (§4.3 stack).
- **`DbSink`** (raw asyncpg, one transaction per batch): `INSERT event_raw … SELECT FROM unnest(arrays) ON CONFLICT DO NOTHING RETURNING event_id`, and facts only for the returned ids (so even `defect`, with no natural key, is idempotent). `telemetry`/`unit_event` DO NOTHING; `buffer_level`/`ckd_stock` keep the last row per key in the batch, then DO UPDATE. Each event is dumped to JSON once for stream, spool and DB.
- **`states`:** `derive_line_state`, `UnitCondition`, `LineStatus` move here from `qost_sim.model.states` (the sim re-exports them).
- **`bottleneck` active-period method** (pure; minutes or datetimes): `active_periods(timeline, window)` and `shifting_bottleneck(periods, window, order, now=None)` → b(t) segments, sole/shifting per line, shares, overall (argmax, ties by flow order). Live = window [start, now] with open periods ending at now. Overlapping shifting regions of consecutive changes go to the nearest transition, so the §9.5 invariant (one sole / exactly two shifting / none) always holds (Q5).
- **`kpi`:** `shift_time_model(line_intervals, line_downtimes, window, threshold)` → `TimeModel` is the one function for live tick, close, replay and DB recompute. `DOWN_*` time splits by the downtime row's current `planned` flag (reclassification moves minutes PDOT↔ADOT); unplanned < threshold = microstop (stays in APT); `STARVED`+`BLOCKED` → ADET; `CHANGEOVER` → AUST; in-shift `IDLE_NO_PLAN` → PDOT. Also `aggregate_kpi()` (areas) and `stop_impact()` (FR-ENG-06).
- **`dq`/`rules`/text:** new `check_flow_balance_live()`; DQ-02 reuses `check_downtime_reconciliation` with live inputs (Q3). New AL-S1 (severity by criticality), AL-B1 and AL-L1 evaluators; `Alert.period_key` for per-excursion keys `rule|entity|startISO` (S1/B1/L1); `AlertValue` widened to JSON. `alert_text.py` takes `message_ru` from api, plus the new rules and FR-ENG-06 impact texts.
- **`uns`:** topic of a tag-map node from the plant hierarchy (parity test vs the sim's `AddressSpace.topic`).
- **Config:** optional `rules.yaml: engine:` with SPEC defaults: `kpi_tick_s` 5, `bottleneck_window_h` 4, `buffer_balance_window_min` 30, `unclassified_after_min` 10, `alert_debounce_s` 300, `bottleneck_rate_shifts` 5, `live_oee_min_elapsed_min` 120.

## 4. services/collector (read-only)
- **Settings (env):** `DATABASE_URL`, `REDIS_URL`, `MQTT_URL`, `PLANT_TAG_MAP`, `OPCUA_ENDPOINT` (overrides the map). Modes: `COLLECTOR_SIGNALS`=opcua|mqtt and `COLLECTOR_UNITS`=mqtt|off (defaults first; per-body unit/defect events exist only on `…/{LINE}/units` §6.8, OPC UA has only counters, Q1). Tuning: `COLLECTOR_BATCH_MAX` 500, `COLLECTOR_BATCH_MS` 200, `COLLECTOR_OPCUA_QUEUE_SIZE` 32 (PROGRESS: microstops at 300×), spool dir/segment/cap, health port 8110.
- **`tagmap.py`:** compiles node_id/topic → (entity_type, code, signal). Used: equipment state/alarm_code/alarm/telemetry, line state, buffer level/capacity, CKD kits; counters, `last_*`, `StateSince`, `Plant.*` are mapped but unused (logged). A node or topic whose last segment is a `FORBIDDEN_SIGNAL` (Degradation) is rejected even if mapped.
- **`opcua_source.py`** (asyncua 2.1): security from the tag map (prod: cert/key, user/password env); one subscription at `publishing_interval_ms`, sampling 0, `queuesize`, read via `async for` ("queue empty" = batch boundary); unknown node → warn + DQ-07; StatusCode → `quality` good/uncertain/bad (FR-ING-04); overflow bit → counter; reconnect with backoff, dropping replayed or unchanged (ts, value) per node. Only Browse/Read/CreateSubscription/CreateMonitoredItems/Publish are used.
- **`mqtt_source.py`** (aiomqtt `{root}/#`, QoS 1): signal topics → the same normalizer; `units` → `parse_event` keeping the sim's `event_id`, `source=mqtt`, `received_ts`=Clock; unknown topic → rate-limited warning + DQ-07.
- **`normalize.py`** (pure):
  - Int32 State via `state_enum` (unknown value → DQ-07, dropped).
  - State + AlarmCode joined by SourceTimestamp into one `state` event (with `alarm_code`; the engine resolves the reason), flushed at batch end or after `join_grace_ms` (MQTT); a late AlarmCode re-emits the same-ts state event.
  - Alarm → `alarm`; Level (+Capacity) → `buffer_level`; Kits → `ckd` (consume/delivery by sign, `set` first); telemetry gets its unit from config.
  - Same (node, ts) within a batch → last wins. `ts` = SourceTimestamp (or payload ts).
- **Pipeline:** sources → queue → Batcher (≤ 500 / ≤ 200 ms) → two independent `SpooledOutput`s, `DbSink` and `StreamOutput` (pipelined XADD). On error or a full queue the batch goes to that output's spool; while the spool is non-empty new batches are spooled too (FIFO); a drainer replays in order with a cursor per committed batch. Duplicates are harmless (`ON CONFLICT`, engine event_id LRU). With the DB down and Redis up, the engine stays live.
- **`spool.py`:** `{dir}/{output}/{seq}.jsonl`, 10 MB segments, one batch per line, cursor (segment, offset) fsynced per batch; torn tail ignored, drained segments deleted; cap 2 GB (NFR-03: 24 h at 60× ≈ 1.5 GB), oldest dropped beyond it with an error and a counter.
- **Reset and health:** on `sim:control` the collector clears its dedupe caches (the engine acks); `/healthz`, `/readyz`, `/stats` (emitted, written per output, spool bytes), mirrored in `collector:status`.
- **Stretch (cut first):** `COLLECTOR_UNITS=counters` for a pilot without MQTT (unit events from count deltas + `LastBodyId`; defects without codes).

## 5. services/engine
**Core (`qost_engine/core`, pure).**
- **Order and quality:** per (entity, kind), ts < last applied ts is late (kept in raw, ignored for state, counted); units/defects deduped by an event_id LRU (200 k); `bad` ignored, `uncertain` used.
- **States (FR-ENG-01):** a change closes/opens `equipment_state` for equipment and lines; same state → no-op; same ts+state with a new `alarm_code` → reason update. Line state from the State tag (`line_state_source=auto`), else derived via `twin_core.states` (input empty → STARVED; no kits at the first line → MAT-SHORTAGE; output full → BLOCKED; source=engine). A line `DOWN_*` reason = the earliest class-A unit's reason.
- **Downtime (FR-ENG-02/03/04):**
  - `DOWN_*`/`CHANGEOVER` opens rows for the equipment and its line, keyed (entity, start_ts). Reason: event → `alarm_code` via `aliases.reasons` → `UNK` (+ DQ-07 once/day per unknown code); `planned` = reason.planned (UNK: from state); on close, duration and `microstop` (< `microstop_threshold_s`).
  - Operator `classify_downtime` `{entity, start_ts, reason_code, comment, user}` → `reason_source=operator`, `planned` recomputed, propagated to the line row with the same start_ts; closed shift → new `kpi_shift` version + `audit_log`.
  - UNK ≥ `unclassified_after_min` → `needs_classification` (live delta, `live:downtime_open`).
- **KPI (§5.4, FR-KPI-03/04):**
  - Per line and shift: PQ/GQ/ΣPRI (`FIRST_EXIT_RESULTS` × `cycle_factor`), defects per area, equipment run/failures/repair. Live every `kpi_tick_s` plant s and on each state/unit change; at most one publish per `ENGINE_PUBLISH_MIN_MS` (100 ms wall).
  - Close when max event ts ≥ end + grace (`ENGINE_CLOSE_GRACE_WALL_S`×speed; 0 in replay), or by clock if no events → final `kpi_shift` v1, `bottleneck_shift`, DQ, close rules.
  - The last 2 closed shifts stay in memory: a late event or reclassification → version + 1 with audit; older shifts are recomputed from DB rows with the same functions.
- **Buffers and bottleneck:** buffers (§9.4) — level; rate = Δlevel over the last 30 plant min → minutes to full/empty. Bottleneck (FR-ENG-05) — current shift and rolling 4 h → b(t) + since + shares; at close → `bottleneck_shift` (line_group = site code).
- **DQ live** at close, upsert by `dedup_key`: DQ-02 per area (Q3); DQ-04 per adjacent pair, residual = upstream (pass + rework_pass) − downstream first exits − Δlevel (|r| ≤ 1 WIP OK, else info, ≥ `flow_balance_warn_units` warning); DQ-06 E > 1.
- **Rules (§9.7):**
  - AL-S1: class A critical on entry, B/C after `microstop_threshold_s`; value = elapsed, reason, impact; class-A stops that end as microstops are auto-resolved (Q2).
  - AL-D1: class-A unplanned min per local day incl. the open stop. AL-O1/O2 at close + live projection after `live_oee_min_elapsed_min` (resolved at close if unconfirmed). AL-Q1/Q3 at close.
  - AL-B1/L1: debounced, per excursion, working shifts only, resolved on clear; L1 coverage = kits / (month plan / working days).
  - Hook registry (event/tick/close evaluators) for P1 (M6) and M1/M2/Q2 (M7).
  - Escalation (`rules.yaml`, plant time): at ts + k×timeout `UPDATE … SET escalation_level=k WHERE status='open'`; a changed row → `alert` delta + `alerts` entry for chain[k].
- **FR-ENG-06 impact:** `stop_impact(elapsed, degraded_capacity, ICT, is_bottleneck, POT×60/ICT, bottleneck rate = mean PQ of the last 5 shifts, upstream buffer free)` → irrecoverable units, or "recoverable in N shifts".

**Live driver.**
- **Startup:** (1) load `engine_checkpoint` (snapshot, stream id, event_ts) and restore, else start empty; (2) create the group (from 0 if new); (3) if the checkpoint is older than the stream's first entry, replay `event_raw` over the gap first; (4) read pending then new entries, skipping ids ≤ the checkpoint; (5) rebuild `live:*`, publish `snapshot`.
- **Loop:** XREADGROUP (BLOCK 100 ms, COUNT 1000) → apply → publish deltas at once (HSET+PUBLISH pipeline). DB effects coalesced by PK; every `ENGINE_COMMIT_MS` (1000) or 2000 events one transaction {effects + checkpoint with snapshot JSON}, then XACK. A timer task runs `advance_to(clock.now())`.
- **DB down:** in-memory write-behind (cap 200 k ops, then stop reading = backpressure) while live publishing continues; a crash restarts from the checkpoint and re-reads unacked entries. `/healthz`, `/readyz`, `/stats` (lag, late events, pending ops).

**Replay driver and history (NFR-08).**
- **`qost_engine replay [--from --to] [--source db|jsonl:PATH]`:** (1) delete engine-derived rows in range; (2) stream non-telemetry `event_raw` (ORDER BY ts, event_id) day by day through the same core — no publish, grace 0, cleared conditions written as resolved (Q4); (3) bulk-write effects per day with the checkpoint; (4) end with the live checkpoint + a `baseline` snapshot at `to`.
- **Decision:** backfill goes sink → DB (unnest batches of 5000) and the engine replays from DB, not in-process; the same path serves restart gap recovery and DB recompute, and the sim stays independent of the engine.
- **Estimate** (507 k events, 103 k non-telemetry): DB load ≈ 25–40 s (event_raw ≈ 50 k rows/s); replay ≈ 10–15 s (parse 2, core 3–5, writes of ≈18 k intervals / ≈6 k downtimes / 264 `kpi_shift` 2 s). Asserted ≤ 90 s / ≤ 60 s, measured into PROGRESS. `make history` = `qost_sim backfill --sink db: --batch-size 5000 && qost_engine replay` (M4 puts it in `make demo`).

**Reset handshake** (on `sim:control {reset, epoch, demo_start T}`; idempotent per epoch).
1. Stop reading, flush the writer, wait until `collector:status` is idle (≤ 2 s).
2. One transaction: delete ts ≥ T from `event_raw`, `telemetry`, `unit_event`, `buffer_level`, `ckd_stock`, `defect`; in `equipment_state`/`downtime` (`import_id` NULL) delete start_ts ≥ T and reopen rows with end_ts > T; delete `kpi_shift`(events)/`bottleneck_shift` for shifts starting ≥ T, plus engine alerts and DQ rows (`import_id` NULL) with ts ≥ T; checkpoint := baseline(T) or empty.
3. `XTRIM events MAXLEN 0`, `XGROUP SETID $`, DEL `live:*`, rebuild from the restored core, publish `snapshot`, ack `{epoch}` (`audit_log` kept).

## 6. Schema and config
- **Migration `0003`** (services/api/migrations): `engine_checkpoint(name PK, stream_id, event_ts, state jsonb, updated_ts)`; `dq_issue.dedup_key` (text, unique, nullable); unique partial index `downtime(entity, start_ts) WHERE import_id IS NULL AND start_ts IS NOT NULL`.
- **Other:** `rules.yaml: engine:` + schema; `.env.example` (`EVENTS_STREAM_MAXLEN`, `COLLECTOR_*`, `ENGINE_*`); compose env, healthchecks → `/readyz`; Makefile `history`; api imports `alert_text` from twin_core.

## 7. Tests
**Unit (`make check`, ≤ +15 s).**
- **T-BN:** T-BN-1/2 exact; hypothesis on random 2–5-line timelines checks the §9.5 invariants, Σsole + Σshifting/2 = covered share, b(t) active, live == closed on the truncated window.
- **twin_core:** monotonic/content ULIDs; `shift_time_model` (clipping, microstop in APT, planned→PDOT, OEE identity); new rules, DQ-02/04 live, `alert_text`; `uns` parity; DbSink row building.
- **collector:** bindings (no Degradation, mode filters); normalizer (quality, state_enum, join same/split/late, dedupe, ckd sign, OPC UA ids == MQTT ids); batcher; spool (roll, order, torn tail, cursor, cap); outputs FIFO. Fidelity: sim records → synthetic notifications → normalizer gives the sim's events.
- **T-RO (static):** AST scan of `services/collector` for `write_value(s)`, `write_attribute`, `set_value`, `set_attribute`, `call_method`, `set_writable`, `add_nodes`/`delete_nodes`, `ua.Write*`/`ua.Call*`; demo-map subscriptions exclude Degradation; no "degradation" in `services/engine`.
- **engine core:** states/late events; downtime (reason chain, microstop, reclassification + propagation, FR-ENG-04); KPI tick/close/versions; rules incl. plant-time escalation and impact; snapshot/restore = an uninterrupted run. Sim-driven: 10 working days → contiguous non-overlapping intervals, time model sums to POT, PQ = sim counters, zero DQ-04, bottleneck mostly PAINT-1, ≤ 3 s.

**Integration (`make test`).**
- **Setup:** shared containers only (never `make up/down` here); own DB `<db>_it_m3` (CREATE + alembic head), Redis DB 15 + prefixed channels, unique MQTT root, temp tag map; sim, collector, engine as subprocesses on free ports.
- **Component:** `test_db_sink` (idempotency, fact gating, ≥ 5 k events/s); `test_collector_opcua` (subscribed set == map without Degradation = runtime T-RO; 300× microstops delivered); `test_collector_equivalence` (OPC UA == MQTT event sets); `test_replay_db` (3 days backfill → DB → replay == in-memory replay; re-run idempotent; time extrapolated).
- **T-INT:** at 60× inject S1; within ≤ 5 s wall: `live:equipment` CONV-03 `DOWN_UNPLANNED`/ME-CHAIN, `read_snapshot()` in §12.3 shape, an open `downtime` CONV-03/ME-CHAIN, AL-S1 critical open, a `live` delta received. Then reset: ack, tail gone, `live:*` rebuilt.
- **T-SF:** default (CI, non-disruptive) cuts an in-test TCP proxy between collector and DB for 60 s; opt-in `QOST_T_SF_DOCKER=1` runs `docker stop/start qost-timescaledb-1` in `finally` (affects other worktrees; run deliberately). Assert: event_id set in stream == set in `event_raw` == collector emitted count; spool empty; engine checkpoint == stream tail; no overlapping intervals.
- **Restart:** kill -9 the engine mid-run, restart; derived rows == replay of `event_raw` over the window.
- **Latency (AC p95, `test_latency`):** 60× for 120 s wall + 300× for 60 s, subscribed to `live`. For each OPC UA-sourced state/buffer delta, latency = recv_wall − wall(event ts), with wall(ts) = wall_ts + (ts − plant_time)/speed from `plant:clock` (same host; includes sim tick, OPC UA publish, batching). Units (MQTT) reported separately. Assert p95 ≤ 2 s; p50/p95/max → `var/m3_latency.json` and PROGRESS. Expected ≈ 0.4 s p50, ≈ 0.9 s p95 (tick 100 + publish ≤ 500 + batch ≤ 200 ms).

## 8. Performance (NFR-01 ≥ 1000 changes/s)
Collector hot path: asyncua decode (≈ 50–100 µs per notification, the main cost), normalize + event build (pydantic validated once, data dict built directly), blake2b id, one JSON dump reused three times, asyncpg unnest (no per-row SQL), one XADD pipeline per batch. Engine: telemetry (80%) skipped unparsed; the rest ≈ 50 events/s live; ticks O(lines × intervals). Measured by a perf smoke of 60 k synthetic changes through normalize → outputs (PROGRESS); full T-LOAD stays in M9.

## 9. Open questions (proposed default first; all switchable)
- **Q1** `COLLECTOR_UNITS=mqtt` by default: OPC UA has no body/defect detail, while §6.8 says "counters from OPC UA"; counters mode is the stretch.
- **Q2** S1 class A "сразу": literal immediate alerting is needed for T-INT (5 min plant = 5 s wall at 60×), but conveyor microstops would give ≈ 3 critical/shift → auto-resolve them when they end below threshold (`s1_resolve_microstops: true`; M8 may delay Telegram).
- **Q3** DQ-02 live: literal PBT−APT contains ADET and excludes planned time while the journal includes planned → fires every shift. Proposal: lost = PDOT + ADOT, journal = equipment rows ≥ threshold clipped to the shift (`dq_live_lost: downtime|literal`).
- **Q4** History alerts from replay are written resolved when their condition cleared within history (otherwise hundreds of open S1/B1 at demo start); `ENGINE_REPLAY_ALERTS=resolved|open`.
- **Q5** §9.5 leaves overlapping shifting regions undefined; the nearest-transition rule keeps the stated invariant.
- **Q6** Stream size: 1 M entries ≈ 250–300 MB vs Redis `maxmemory 512mb` (noeviction). Measure; if > 200 MB, the default becomes 300 k (gap recovery from `event_raw` covers it).
- **Q7** A reset during a DB outage (non-empty spool) is unsupported; `demo-reset` requires a healthy stack (documented).
- **Q8** Migration 0003 is owned by M3 (parallel stages must not add one); `uv.lock`/PROGRESS will conflict with m7a → merge by hand.
- **Q9** Downtime has one writer, the engine; API PATCH (M4) = audit + operator event. **Q10** Downtime rows exist for equipment and lines; the UI filters by entity type.

## 10. Order
twin_core (events, states, bottleneck + T-BN, kpi, rules/dq/text, live, uns) → migration 0003, DbSink, `--batch-size` → engine core + unit tests, then the sim-driven test → replay and `make history` (measure) → collector (normalizer, spool/outputs, then sources) → live driver and reset → integration (T-INT, T-SF, latency, restart) → PROGRESS, commit `M3: …`.
