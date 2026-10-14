"use client";

import { useQuery } from "@tanstack/react-query";

import { usePlant } from "@/components/plant-context";
import { plantNow, useLive } from "@/lib/live-store";

import { api } from "./client";

/** Plant-local calendar day of a moment as YYYY-MM-DD. */
export function isoDay(ms: number, tz: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(ms);
}

/** One shift row of GET /api/v1/kpi?granularity=shift (only the fields the screens read). */
export interface KpiShiftRow {
  code: string;
  date: string;
  shift?: string | null;
  fpy: number | null;
  rty?: number | null;
  final?: boolean;
}

/** A week back finds the last closed shift after a weekend or a holiday. */
const LOOKBACK_DAYS = 7;

/**
 * KPI rows of the closed shifts of the past week (no live tail): the last closed shift for the
 * FPY / RTY fallback on /quality and the shift picker on /reports. `enabled: false` skips it.
 */
export function useClosedShifts(level: "area" | "plant", enabled = true) {
  const { assets, fmt } = usePlant();
  // Changes once a plant day, not on every clock tick.
  const from = useLive((s) => {
    const now = plantNow(s.clock);
    return now == null || !assets ? null : isoDay(now - LOOKBACK_DAYS * 86_400_000, fmt.tz);
  });
  return useQuery({
    queryKey: ["kpi", level, "shift", "closed", from],
    queryFn: () => api<{ items: KpiShiftRow[] }>("/api/v1/kpi", { query: { level, granularity: "shift", include_live: false, from: from ?? undefined } }),
    enabled: enabled && Boolean(from),
    staleTime: 60_000,
  });
}
