"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type {
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

/** Director data (SPEC §13.2): one query per tile group, base forecast computed on open (AL-P1). */
export function usePlanProgress() {
  return useQuery({ queryKey: ["plan", "progress"], queryFn: () => api<PlanProgress>("/api/v1/plan/progress"), refetchInterval: 60_000 });
}

export function useBaseForecast() {
  return useQuery({
    queryKey: ["forecast", "base"],
    queryFn: () => api<ForecastRunView>("/api/v1/forecast", { method: "POST", body: { mode: "fast", compare: false } }),
    staleTime: 120_000,
  });
}

export function useLevers() {
  return useQuery({ queryKey: ["forecast", "levers"], queryFn: () => api<LeversResult>("/api/v1/forecast/levers"), staleTime: 300_000 });
}

/** «С системой» — the economic effect of the default scenario (SPEC §10.5). */
export function useSystemEffect() {
  return useQuery({
    queryKey: ["effect", "system"],
    queryFn: () => api<EffectResult>("/api/v1/effect", { method: "POST", body: {} }),
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
