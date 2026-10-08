"use client";

import { create } from "zustand";

import type {
  AlertCounts,
  AreaLive,
  BottleneckLive,
  BufferLive,
  ClockLive,
  DowntimeLive,
  EquipmentLive,
  LineLive,
  PlantLive,
  Snapshot,
  UnitEvent,
} from "@/lib/api/types";

/**
 * Live plant state fed by WS /ws/live (SPEC §12.3): the first message is a snapshot, then deltas
 * (state, kpi, buffer, bottleneck, alert, clock, unit). Units are not stored — they go to
 * subscribers (body animation) through {@link onUnit}.
 */
export type ConnectionStatus = "connecting" | "live" | "reconnecting" | "offline";

export interface PlantClock {
  /** Plant time (epoch ms) at `receivedAt` (performance.now()). */
  plantMs: number;
  receivedAt: number;
  speed: number;
  paused: boolean;
  mode: string;
  shift: { date: string; code: string; elapsed_min?: number } | null;
}

interface LiveState {
  status: ConnectionStatus;
  ready: boolean;
  clock: PlantClock | null;
  lines: Record<string, LineLive>;
  equipment: Record<string, EquipmentLive>;
  buffers: Record<string, BufferLive>;
  areas: Record<string, AreaLive>;
  plant: PlantLive | null;
  bottleneck: BottleneckLive | null;
  alertsOpen: AlertCounts;
  downtimeOpen: Record<string, DowntimeLive>;
  /** Incremented on every alert / state message — components refetch lists on change. */
  alertSeq: number;
  stateSeq: number;
  lastMessageAt: number;
  setStatus: (s: ConnectionStatus) => void;
  applySnapshot: (s: Snapshot) => void;
  applyMessage: (type: string, data: Record<string, unknown>) => void;
}

function byCode<T extends { code: string }>(rows: T[] | undefined): Record<string, T> {
  const out: Record<string, T> = {};
  for (const r of rows ?? []) out[r.code] = r;
  return out;
}

function clockOf(c: ClockLive | null | undefined): PlantClock | null {
  if (!c) return null;
  return {
    plantMs: Date.parse(c.plant_time),
    receivedAt: performance.now(),
    speed: c.speed,
    paused: Boolean(c.paused) || c.speed === 0,
    mode: c.mode,
    shift: c.shift,
  };
}

const unitListeners = new Set<(u: UnitEvent) => void>();

/** Subscribe to completed units (body animation); returns the unsubscribe function. */
export function onUnit(fn: (u: UnitEvent) => void): () => void {
  unitListeners.add(fn);
  return () => {
    unitListeners.delete(fn);
  };
}

export const useLive = create<LiveState>((set) => ({
  status: "connecting",
  ready: false,
  clock: null,
  lines: {},
  equipment: {},
  buffers: {},
  areas: {},
  plant: null,
  bottleneck: null,
  alertsOpen: { critical: 0, warning: 0, info: 0 },
  downtimeOpen: {},
  alertSeq: 0,
  stateSeq: 0,
  lastMessageAt: 0,
  setStatus: (status) => set({ status }),
  applySnapshot: (s) =>
    set((prev) => ({
      ready: true,
      clock: clockOf(s.clock) ?? prev.clock,
      lines: byCode(s.lines),
      equipment: byCode(s.equipment),
      buffers: byCode(s.buffers),
      areas: byCode(s.areas),
      plant: s.plant ?? null,
      bottleneck: s.bottleneck,
      alertsOpen: s.alerts_open,
      downtimeOpen: Object.fromEntries((s.downtime_open ?? []).map((d) => [d.entity, d])),
      alertSeq: prev.alertSeq + 1,
      stateSeq: prev.stateSeq + 1,
      lastMessageAt: performance.now(),
    })),
  applyMessage: (type, data) =>
    set((prev) => {
      const now = performance.now();
      switch (type) {
        case "clock": {
          if (data.event === "tick" && typeof data.plant_time === "string") {
            return { clock: clockOf(data as unknown as ClockLive), lastMessageAt: now };
          }
          return { lastMessageAt: now, stateSeq: prev.stateSeq + 1 };
        }
        case "state": {
          if (data.downtime && !data.entity_type) {
            const d = data.downtime as DowntimeLive;
            return {
              downtimeOpen: { ...prev.downtimeOpen, [d.entity]: d },
              stateSeq: prev.stateSeq + 1,
              lastMessageAt: now,
            };
          }
          if (data.downtime_closed) {
            const d = data.downtime_closed as DowntimeLive;
            const rest = { ...prev.downtimeOpen };
            delete rest[d.entity];
            return { downtimeOpen: rest, stateSeq: prev.stateSeq + 1, lastMessageAt: now };
          }
          const code = String(data.code ?? "");
          if (!code) return { lastMessageAt: now };
          if (data.entity_type === "line") {
            return {
              lines: { ...prev.lines, [code]: { ...prev.lines[code], ...(data as unknown as LineLive) } },
              stateSeq: prev.stateSeq + 1,
              lastMessageAt: now,
            };
          }
          const eq = { ...prev.equipment[code], ...(data as unknown as EquipmentLive) };
          const downtimeOpen = { ...prev.downtimeOpen };
          if (eq.downtime && !eq.downtime.end_ts) downtimeOpen[code] = eq.downtime;
          return {
            equipment: { ...prev.equipment, [code]: eq },
            downtimeOpen,
            stateSeq: prev.stateSeq + 1,
            lastMessageAt: now,
          };
        }
        case "kpi": {
          const code = String(data.code ?? "");
          if (data.level === "line") {
            return { lines: { ...prev.lines, [code]: { ...prev.lines[code], ...(data as unknown as LineLive) } }, lastMessageAt: now };
          }
          if (data.level === "area") {
            return { areas: { ...prev.areas, [code]: data as unknown as AreaLive }, lastMessageAt: now };
          }
          if (data.level === "plant") return { plant: data as unknown as PlantLive, lastMessageAt: now };
          return { lastMessageAt: now };
        }
        case "buffer": {
          const code = String(data.code ?? "");
          return { buffers: { ...prev.buffers, [code]: { ...prev.buffers[code], ...(data as unknown as BufferLive) } }, lastMessageAt: now };
        }
        case "bottleneck":
          return { bottleneck: data as unknown as BottleneckLive, lastMessageAt: now };
        case "alert":
          return { alertSeq: prev.alertSeq + 1, lastMessageAt: now };
        case "unit":
          for (const fn of unitListeners) fn(data as unknown as UnitEvent);
          return { lastMessageAt: now };
        default:
          return { lastMessageAt: now };
      }
    }),
}));

/** Plant time now, extrapolated from the last tick with the simulation speed. */
export function plantNow(clock: PlantClock | null, nowPerf: number = performance.now()): number | null {
  if (!clock) return null;
  const factor = clock.paused ? 0 : clock.speed;
  return clock.plantMs + (nowPerf - clock.receivedAt) * factor;
}
