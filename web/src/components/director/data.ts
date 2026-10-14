"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "@/lib/api/client";
import type {
  AlertsPage,
  BottleneckResponse,
  EffectResult,
  ForecastRunView,
  KpiResponse,
  LeversResult,
  LossesResponse,
  Overrides,
  PlanProgress,
  ReportView,
} from "@/lib/api/types";

import { useLive, usePaused } from "@/lib/live-store";

/**
 * Line capacity for the month (additive field of GET /plan/progress): bottleneck ICT × calendar
 * shifts. Absent on older APIs, null when the month has no plan.
 */
export interface PlanCeiling {
  qty: number;
  rate_per_shift: number;
  shifts: number;
}

/** GET /plan/progress with the optional fields the director screen reads defensively. */
export type DirectorPlan = Omit<PlanProgress, "unallocated"> & {
  /** null for a month without a plan (STAB-7). */
  unallocated: number | null;
  ceiling?: PlanCeiling | null;
  output_line?: string | null;
};

/** Forecast-type queries re-read every 30 s while the plant clock runs (DEMO-3). */
const LIVE_REFRESH_MS = 30_000;

function useLiveInterval(): number | false {
  return usePaused() ? false : LIVE_REFRESH_MS;
}

/** Director data (SPEC §13.2): one query per tile group, base forecast computed on open (AL-P1). */
export function usePlanProgress() {
  return useQuery({ queryKey: ["plan", "progress"], queryFn: () => api<DirectorPlan>("/api/v1/plan/progress"), refetchInterval: 60_000 });
}

export function useBaseForecast() {
  const interval = useLiveInterval();
  return useQuery({
    queryKey: ["forecast", "base"],
    queryFn: () => api<ForecastRunView>("/api/v1/forecast", { method: "POST", body: { mode: "fast", compare: false } }),
    staleTime: 20_000,
    refetchInterval: interval,
  });
}

export function useLevers() {
  const interval = useLiveInterval();
  return useQuery({
    queryKey: ["forecast", "levers"],
    queryFn: () => api<LeversResult>("/api/v1/forecast/levers"),
    staleTime: 20_000,
    refetchInterval: interval,
  });
}

/**
 * «С системой» — the economic effect of the default scenario (SPEC §10.5). It is simulated from
 * the month start with the calibration snapshot, not from the live state, so it is not polled:
 * the pause / alert / new-timeline invalidations re-read it (no Monte-Carlo run every 30 s
 * competing with a what-if for the forecast slots).
 */
export function useSystemEffect() {
  return useQuery({
    queryKey: ["effect", "system"],
    queryFn: () => api<EffectResult>("/api/v1/effect", { method: "POST", body: {} }),
    staleTime: 300_000,
  });
}

/**
 * Alert rules whose firing changes the month forecast: an unplanned stop (AL-S1) and the plan
 * risk alert (AL-P1, raised by the engine's own forecast). Rule ids are the SPEC §7 contract.
 */
const FORECAST_ALERT_RULES = ["AL-S1", "AL-P1"] as const;

/** Query groups that depend on the plant state: re-read together. */
const STATE_KEYS = [["forecast"], ["effect"], ["plan"], ["kpi"], ["bottleneck"]] as const;

/**
 * Keeps the director tiles and the what-if base on the same plant state (DEMO-3): refetch when
 * the clock pauses (the presenter stops to talk) and when an AL-S1 / AL-P1 alert appears or
 * changes status. A new timeline (demo reset) is handled globally by `Providers` (epochSeq).
 */
export function useDirectorFreshness(): void {
  const qc = useQueryClient();
  const paused = usePaused();
  const wasPaused = useRef(paused);
  useEffect(() => {
    if (paused && !wasPaused.current) {
      for (const queryKey of STATE_KEYS) void qc.invalidateQueries({ queryKey: [...queryKey] });
    }
    wasPaused.current = paused;
  }, [paused, qc]);

  const watch = useQuery({
    queryKey: ["alerts", "forecast-watch"],
    queryFn: () => api<AlertsPage>("/api/v1/alerts", { query: { rule_id: FORECAST_ALERT_RULES, limit: 5 } }),
    staleTime: 2_000,
  });
  // the engine announces an alert before its writer commits the row: re-read a moment later
  const seq = useLive((s) => s.alertSeq);
  useEffect(() => {
    if (seq === 0) return;
    const a = setTimeout(() => void qc.invalidateQueries({ queryKey: ["alerts", "forecast-watch"] }), 1_200);
    return () => clearTimeout(a);
  }, [seq, qc]);
  // order-independent: the base forecast re-upserts the day's AL-P1 with a fresh ts, which moves
  // it in the ts-sorted list — a reorder alone must not start another refetch round
  const signature = watch.data
    ? watch.data.items
        .map((a) => `${a.id}:${a.status}`)
        .sort()
        .join("|")
    : null;
  const seen = useRef<string | null>(null);
  useEffect(() => {
    if (signature === null) return;
    if (seen.current !== null && seen.current !== signature) {
      for (const queryKey of STATE_KEYS) void qc.invalidateQueries({ queryKey: [...queryKey] });
    }
    seen.current = signature;
  }, [signature, qc]);
}

/** Effective thresholds the director screen draws (GET /config/rules → thresholds, SPEC §7). */
export interface DirectorThresholds {
  oee_target?: number;
  defect_rate_limit?: number;
  plan_risk_warn_p?: number;
  plan_risk_crit_p?: number;
  /** Target key watched by AL-P1: `line_plan` or `plant_target`. */
  plan_risk_target?: string;
}

export function useThresholds() {
  return useQuery({
    queryKey: ["config", "rules", "thresholds"],
    queryFn: async () => (await api<{ thresholds: DirectorThresholds }>("/api/v1/config/rules")).thresholds,
    staleTime: 300_000,
  });
}

export function useAreaKpi() {
  return useQuery({
    queryKey: ["kpi", "area", "month"],
    queryFn: () => api<KpiResponse>("/api/v1/kpi", { query: { level: "area", granularity: "month" } }),
    refetchInterval: 60_000,
  });
}

export function useLosses() {
  return useQuery({ queryKey: ["kpi", "losses"], queryFn: () => api<LossesResponse>("/api/v1/kpi/losses"), refetchInterval: 120_000 });
}

export function useBottleneckMonth() {
  return useQuery({ queryKey: ["bottleneck", "month"], queryFn: () => api<BottleneckResponse>("/api/v1/bottleneck"), refetchInterval: 120_000 });
}

export async function runScenario(overrides: Overrides): Promise<{ run: ForecastRunView; effect: EffectResult | null }> {
  const [run, effect] = await Promise.all([
    api<ForecastRunView>("/api/v1/forecast", { method: "POST", body: { mode: "fast", overrides, compare: true } }),
    api<EffectResult>("/api/v1/effect", { method: "POST", body: { scenario: overrides } }).catch(() => null),
  ]);
  return { run, effect };
}

/**
 * Last stored shift report: walks back over (date, shift) of the previous working shifts.
 * `shifts` is the calendar order of shift codes; `today` the plant date.
 */
export function useLastReport(today: string | null, current: string | null, shifts: string[]) {
  return useQuery({
    queryKey: ["reports", "last", today, current],
    enabled: Boolean(today) && shifts.length > 0,
    staleTime: 120_000,
    queryFn: async () => {
      const candidates: Array<{ date: string; shift: string }> = [];
      const day = new Date(`${today}T12:00:00Z`);
      for (let back = 0; back < 4 && candidates.length < 6; back++) {
        const d = new Date(day.getTime() - back * 86_400_000).toISOString().slice(0, 10);
        const codes = [...shifts].reverse();
        for (const code of codes) {
          if (back === 0 && current && shifts.indexOf(code) >= shifts.indexOf(current)) continue;
          candidates.push({ date: d, shift: code });
        }
      }
      for (const c of candidates.slice(0, 6)) {
        const list = await api<ReportView[]>("/api/v1/reports/shift", { query: { date: c.date, shift: c.shift } });
        if (list.length > 0) return { ...c, report: list[0] as ReportView };
      }
      return { ...(candidates[0] ?? { date: today ?? "", shift: "" }), report: null };
    },
  });
}
