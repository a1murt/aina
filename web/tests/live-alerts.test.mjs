// Unit tests of the pending alert rows (web/src/lib/live-alerts.ts): `node --test` with Node's
// built-in TypeScript type stripping (Node >= 22.18), run by `pnpm test:unit` (make check).
import assert from "node:assert/strict";
import { test } from "node:test";

import { ALERT_COMMIT_GRACE_MS, mergeAlerts, noteAlert } from "../src/lib/live-alerts.ts";

const PLANT_NOW = Date.parse("2026-10-16T02:53:30Z"); // 07:53:30 plant time
const S1 = {
  dedup_key: "AL-S1|CONV-03|2026-10-16T02:53:00",
  ts: "2026-10-16T02:53:00+00:00",
  rule_id: "AL-S1",
  severity: "critical",
  entity_type: "equipment",
  entity: "CONV-03",
  title_ru: "Внеплановая остановка",
  message_ru: "Конвейер CONV-03: остановка с 07:53",
  value: { started: "2026-10-16T02:53:00" },
  status: "open",
  resolved_ts: null,
  recipients: ["master"],
  channels: ["ui"],
};

function listed(over = {}) {
  return {
    id: 41,
    ts: S1.ts,
    rule_id: "AL-S1",
    severity: "critical",
    entity_type: "equipment",
    entity: "CONV-03",
    title_ru: S1.title_ru,
    title_kk: null,
    message_ru: S1.message_ru,
    message_kk: null,
    value: null,
    status: "open",
    ack_by: null,
    ack_ts: null,
    escalation_level: 0,
    recipients: ["master"],
    can_act: true,
    dedup_key: S1.dedup_key,
    ...over,
  };
}

test("a new AL-S1 shows at once as a pending row, before the list is re-read", () => {
  const t0 = 1_000_000;
  const live = noteAlert({}, S1, t0, PLANT_NOW, 60);
  assert.ok(live);
  // the list was read 2 s before the message and does not have the alert
  const rows = mergeAlerts([], t0 - 2_000, live);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].pending, true);
  assert.equal(rows[0].entity, "CONV-03");
  assert.equal(rows[0].rule_id, "AL-S1");
  assert.equal(rows[0].can_act, false); // no database id yet: no «Принять»
  // the list re-read a moment later has the committed row: it replaces the pending one
  const after = mergeAlerts([listed()], t0 + 1_500, live);
  assert.deepEqual(after.map((r) => [r.id, r.pending ?? false]), [[41, false]]);
});

test("the pending row survives an early re-read without the commit, not a late one", () => {
  const t0 = 2_000_000;
  const live = noteAlert({}, S1, t0, PLANT_NOW, 60);
  assert.equal(mergeAlerts([], t0 + 1_000, live).length, 1);
  assert.equal(mergeAlerts([], t0 + ALERT_COMMIT_GRACE_MS + 1, live).length, 0);
});

test("minute updates keep the first-seen time; an old alert never becomes pending", () => {
  const t0 = 3_000_000;
  let live = noteAlert({}, S1, t0, PLANT_NOW, 60);
  live = noteAlert(live, { ...S1, message_ru: "…14 мин; потеря ≈ 3,5 авто" }, t0 + 1_000, PLANT_NOW + 60_000, 60);
  assert.equal(live[S1.dedup_key].firstSeenAt, t0);
  assert.equal(live[S1.dedup_key].receivedAt, t0 + 1_000);
  assert.match(live[S1.dedup_key].message_ru, /3,5 авто/);
  // a tab opened while a stop runs: the first message it sees is a minute update of an alert
  // raised 20 plant minutes ago — acknowledged maybe (the engine still says «open»): no row
  const old = noteAlert({}, S1, t0, PLANT_NOW + 20 * 60_000, 60);
  assert.equal(old[S1.dedup_key].raisedNow, false);
  assert.equal(mergeAlerts([], t0 - 100, old).length, 0);
});

test("an alert announced as acknowledged or resolved leaves the feed before the re-read", () => {
  const t0 = 4_000_000;
  let live = noteAlert({}, S1, t0, PLANT_NOW, 60);
  assert.equal(mergeAlerts([listed()], t0 + 500, live).length, 1);
  live = noteAlert(live, { ...S1, status: "ack" }, t0 + 3_000, PLANT_NOW + 180_000, 60);
  assert.equal(mergeAlerts([listed()], t0 + 500, live).length, 0);
  // an older message does not hide a row of a newer list
  assert.equal(mergeAlerts([listed()], t0 + 3_000 + ALERT_COMMIT_GRACE_MS + 1, live).length, 1);
  const resolved = noteAlert({}, { ...S1, status: "resolved" }, t0, PLANT_NOW, 60);
  assert.equal(mergeAlerts([], t0 - 100, resolved).length, 0);
});

test("partial messages are ignored; the memory is bounded", () => {
  assert.equal(noteAlert({}, { dedup_key: S1.dedup_key, escalation_level: 1, notify: ["director"] }, 1, PLANT_NOW, 60), null);
  assert.equal(noteAlert({}, { ...S1, severity: "fatal" }, 1, PLANT_NOW, 60), null);
  let live = {};
  for (let i = 0; i < 5; i++) live = noteAlert(live, { ...S1, dedup_key: `k${i}` }, 100 + i, PLANT_NOW, 1, 3);
  assert.deepEqual(Object.keys(live).sort(), ["k2", "k3", "k4"]);
});

test("before the first clock tick a message counts as new; at ×1 the window is 30 plant s", () => {
  assert.equal(noteAlert({}, S1, 1, null, 0)[S1.dedup_key].raisedNow, true);
  const ts = Date.parse(S1.ts);
  assert.equal(noteAlert({}, S1, 1, ts + 20_000, 1)[S1.dedup_key].raisedNow, true);
  assert.equal(noteAlert({}, S1, 1, ts + 61_000, 1)[S1.dedup_key].raisedNow, false);
});
