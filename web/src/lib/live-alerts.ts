import type { AlertView, Severity } from "@/lib/api/types";

/**
 * Alerts announced over WS /ws/live (SPEC §12.3), shown before the alerts list is re-read.
 *
 * The engine publishes an `alert` message as soon as it decides (an AL-S1 the moment a unit
 * stops) and commits the row on its next database commit; the lists re-read `/alerts` after the
 * message burst settles (alerts.tsx). A running stop re-publishes its AL-S1 every plant minute,
 * so at 60× the burst never settles and the list waits for its maximum wait (≈ 5 s). To show a
 * new alert at once, the feed adds it from the message as a pending row (no database id, no
 * actions) and reconciles with the next list: a listed alert replaces its pending row; a list
 * read well after the first message (ALERT_COMMIT_GRACE_MS) is the truth.
 *
 * Pure functions (no React, no store): unit-tested by `node --test` (web/tests).
 */
export interface LiveAlert {
  dedup_key: string;
  /** Plant time of the first raise (the engine keeps it on every update of the same alert). */
  ts: string;
  rule_id: string;
  severity: Severity;
  entity_type: string;
  entity: string;
  title_ru: string;
  message_ru: string;
  value: unknown;
  /** As the database keeps it: `ack` stays until `resolved`, whatever the engine's updates say. */
  status: AlertView["status"];
  /** Wall ms (Date.now) of the first message for this key in this tab. */
  firstSeenAt: number;
  /** Wall ms of the latest message for this key. */
  receivedAt: number;
  /**
   * First seen within moments of its raise (plant time): a new alert, not an update of an older
   * one. A running stop's minute update says `open` even when the alert is acknowledged (the
   * database keeps `ack`), so only new alerts may become pending rows.
   */
  raisedNow: boolean;
}

/** An alert row of a feed: from the list, or pending (from a WS message, not committed yet). */
export type FeedAlert = AlertView & { pending?: boolean };

/** A list read this long after an alert was first announced has its commit (or knows better). */
export const ALERT_COMMIT_GRACE_MS = 8_000;
/** Delivery lag of a new alert (engine processing + WS), wall ms, and its floor in plant ms. */
const RAISE_LAG_WALL_MS = 5_000;
const RAISE_LAG_MIN_PLANT_MS = 30_000;
/** Distinct alerts remembered per tab (oldest first seen are dropped). */
export const LIVE_ALERTS_MAX = 200;

const SEVERITIES: readonly string[] = ["critical", "warning", "info"];
const STATUSES: readonly string[] = ["open", "ack", "resolved"];

function str(data: Record<string, unknown>, key: string): string | null {
  const v = data[key];
  return typeof v === "string" && v ? v : null;
}

/**
 * Remember an `alert` message: the latest payload per dedup key, keeping when it was first seen.
 * Partial messages (escalation level only) are ignored — returns null when nothing changes.
 * `plantNowMs`/`speed`: the plant clock at arrival (null before the first clock tick).
 */
export function noteAlert(
  prev: Readonly<Record<string, LiveAlert>>,
  data: Record<string, unknown>,
  nowMs: number,
  plantNowMs: number | null,
  speed: number,
  max: number = LIVE_ALERTS_MAX,
): Record<string, LiveAlert> | null {
  const key = str(data, "dedup_key");
  const rule = str(data, "rule_id");
  const entity = str(data, "entity");
  const ts = str(data, "ts");
  const severity = str(data, "severity");
  const status = str(data, "status");
  if (!key || !rule || !entity || !ts || !severity || !SEVERITIES.includes(severity) || !status || !STATUSES.includes(status)) {
    return null;
  }
  const old = prev[key];
  // as the database keeps it: an engine update saying `open` does not undo an acknowledgement,
  // only `resolved` does (engine writer, alert upsert) — else a stop acknowledged within the
  // grace of its raise would come back as a pending row with its next minute update
  const kept = old?.status === "ack" && status === "open" ? "ack" : (status as LiveAlert["status"]);
  const tsMs = Date.parse(ts);
  const lag = Math.max(RAISE_LAG_MIN_PLANT_MS, Math.max(0, speed) * RAISE_LAG_WALL_MS);
  const raisedNow = old ? old.raisedNow : plantNowMs === null || Number.isNaN(tsMs) || tsMs >= plantNowMs - lag;
  const next: Record<string, LiveAlert> = {
    ...prev,
    [key]: {
      dedup_key: key,
      ts,
      rule_id: rule,
      severity: severity as Severity,
      entity_type: str(data, "entity_type") ?? "equipment",
      entity,
      title_ru: str(data, "title_ru") ?? rule,
      message_ru: str(data, "message_ru") ?? "",
      value: data.value ?? null,
      status: kept,
      firstSeenAt: old?.firstSeenAt ?? nowMs,
      receivedAt: nowMs,
      raisedNow,
    },
  };
  const keys = Object.keys(next);
  if (keys.length > max) {
    keys.sort((a, b) => (next[a]?.firstSeenAt ?? 0) - (next[b]?.firstSeenAt ?? 0));
    for (const k of keys.slice(0, keys.length - max)) delete next[k];
  }
  return next;
}

function pendingRow(m: LiveAlert): FeedAlert {
  return {
    id: 0,
    ts: m.ts,
    rule_id: m.rule_id,
    severity: m.severity,
    entity_type: m.entity_type,
    entity: m.entity,
    title_ru: m.title_ru,
    title_kk: null,
    message_ru: m.message_ru,
    message_kk: null,
    value: m.value,
    status: "open",
    ack_by: null,
    ack_ts: null,
    escalation_level: 0,
    recipients: [],
    can_act: false,
    dedup_key: m.dedup_key,
    pending: true,
  };
}

/**
 * The open alerts as listed (`fetched`, read at wall ms `fetchedAt`; 0 = not read yet),
 * corrected by the WS messages:
 * - a new open alert that the list does not have yet is added as a pending row, until a list
 *   read ALERT_COMMIT_GRACE_MS after its first message (the list then has it, or it is not open);
 * - a listed alert whose message since the list (minus the grace) says acknowledged or resolved
 *   is dropped.
 */
export function mergeAlerts(
  fetched: readonly FeedAlert[],
  fetchedAt: number,
  live: Readonly<Record<string, LiveAlert>>,
  graceMs: number = ALERT_COMMIT_GRACE_MS,
): FeedAlert[] {
  const listed = new Set<string>();
  const out: FeedAlert[] = [];
  for (const row of fetched) {
    const msg = row.dedup_key ? live[row.dedup_key] : undefined;
    if (row.dedup_key) listed.add(row.dedup_key);
    if (msg && msg.status !== "open" && msg.receivedAt > fetchedAt - graceMs) continue;
    out.push(row);
  }
  for (const msg of Object.values(live)) {
    if (msg.status !== "open" || !msg.raisedNow || listed.has(msg.dedup_key)) continue;
    if (msg.firstSeenAt <= fetchedAt - graceMs) continue;
    out.push(pendingRow(msg));
  }
  return out;
}
