"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { AlertsPage, DowntimeLive } from "@/lib/api/types";

/** Rule of an unplanned equipment stop (twin_core.rules.AL_EQUIPMENT_STOP). */
const STOP_ALERT_RULE = "AL-S1";

/** Output loss of an open stop as estimated by the engine (AL-S1 `value.impact`). */
export interface StopImpact {
  entity: string;
  /** Start of the stop the estimate belongs to (ISO). */
  started: string | null;
  lostUnits: number;
  lostMin: number | null;
}

interface S1Value {
  started?: string | null;
  ended?: string | null;
  impact?: { lost_units?: number | null; lost_min?: number | null } | null;
}

/**
 * Loss estimates of the open stops, keyed by unit (S14, VIS-1d). The number is the engine's
 * AL-S1 value — the UI does not recompute it (rule 3). The query key sits under ["alerts"], so
 * every `alert` WS message (the engine re-raises AL-S1 as the stop grows) refreshes it through
 * the alerts feed; a slow poll covers screens without the feed (TV mode).
 */
export function useStopImpacts(): Record<string, StopImpact> {
  const q = useQuery({
    queryKey: ["alerts", "stop-impact"],
    queryFn: () => api<AlertsPage>("/api/v1/alerts", { query: { rule_id: STOP_ALERT_RULE, status: "open,ack", limit: 20 } }),
    staleTime: 2_000,
    refetchInterval: 15_000,
  });
  const out: Record<string, StopImpact> = {};
  for (const a of q.data?.items ?? []) {
    const v = (a.value ?? {}) as S1Value;
    const lost = v.impact?.lost_units;
    if (v.ended || typeof lost !== "number" || !Number.isFinite(lost)) continue;
    const prev = out[a.entity];
    // newest alert of the unit wins (items come newest first)
    if (prev) continue;
    out[a.entity] = { entity: a.entity, started: v.started ?? null, lostUnits: lost, lostMin: v.impact?.lost_min ?? null };
  }
  return out;
}

/** The estimate of this open stop (same unit, same start ±1 s), if the engine has published one. */
export function impactOf(impacts: Record<string, StopImpact>, stop: Pick<DowntimeLive, "entity" | "start_ts">): StopImpact | undefined {
  const hit = impacts[stop.entity];
  if (!hit) return undefined;
  if (!hit.started) return hit;
  return Math.abs(Date.parse(hit.started) - Date.parse(stop.start_ts)) <= 1_000 ? hit : undefined;
}
