import {
  Ban,
  CircleAlert,
  CircleDashed,
  Info,
  Moon,
  OctagonX,
  Play,
  RefreshCw,
  TriangleAlert,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type { Severity, StateCode } from "@/lib/api/types";

/**
 * ISA-101 presentation of equipment / line states (SPEC §5.3, §13.1): CSS token, icon and
 * label key — a state is never shown by colour alone. Normal operation is neutral grey.
 */
export interface StateMeta {
  Icon: LucideIcon;
  /** CSS custom property with the state colour. */
  token: string;
  text: string;
  bg: string;
  border: string;
  /** Translucent background of a badge (static class names: Tailwind scans the source). */
  tint: string;
  /** SVG classes of the schema: translucent fill, stroke and text fill. */
  fill: string;
  stroke: string;
  textFill: string;
  /** Abnormal states get colour; normal / idle stay grey. */
  abnormal: boolean;
}

export const STATE_META: Record<StateCode, StateMeta> = {
  RUNNING: { Icon: Play, token: "--state-running", text: "text-state-running", bg: "bg-state-running", border: "border-state-running", tint: "bg-state-running/15", fill: "fill-state-running/15", stroke: "stroke-state-running", textFill: "fill-state-running", abnormal: false },
  DEGRADED: { Icon: TriangleAlert, token: "--state-degraded", text: "text-state-degraded", bg: "bg-state-degraded", border: "border-state-degraded", tint: "bg-state-degraded/15", fill: "fill-state-degraded/15", stroke: "stroke-state-degraded", textFill: "fill-state-degraded", abnormal: true },
  STARVED: { Icon: CircleDashed, token: "--state-starved", text: "text-state-starved", bg: "bg-state-starved", border: "border-state-starved", tint: "bg-state-starved/15", fill: "fill-state-starved/15", stroke: "stroke-state-starved", textFill: "fill-state-starved", abnormal: true },
  BLOCKED: { Icon: Ban, token: "--state-blocked", text: "text-state-blocked", bg: "bg-state-blocked", border: "border-state-blocked", tint: "bg-state-blocked/15", fill: "fill-state-blocked/15", stroke: "stroke-state-blocked", textFill: "fill-state-blocked", abnormal: true },
  DOWN_UNPLANNED: { Icon: OctagonX, token: "--state-down-unplanned", text: "text-state-down-unplanned", bg: "bg-state-down-unplanned", border: "border-state-down-unplanned", tint: "bg-state-down-unplanned/15", fill: "fill-state-down-unplanned/15", stroke: "stroke-state-down-unplanned", textFill: "fill-state-down-unplanned", abnormal: true },
  DOWN_PLANNED: { Icon: Wrench, token: "--state-down-planned", text: "text-state-down-planned", bg: "bg-state-down-planned", border: "border-state-down-planned", tint: "bg-state-down-planned/15", fill: "fill-state-down-planned/15", stroke: "stroke-state-down-planned", textFill: "fill-state-down-planned", abnormal: true },
  CHANGEOVER: { Icon: RefreshCw, token: "--state-changeover", text: "text-state-changeover", bg: "bg-state-changeover", border: "border-state-changeover", tint: "bg-state-changeover/15", fill: "fill-state-changeover/15", stroke: "stroke-state-changeover", textFill: "fill-state-changeover", abnormal: true },
  IDLE_NO_PLAN: { Icon: Moon, token: "--state-idle-no-plan", text: "text-state-idle-no-plan", bg: "bg-state-idle-no-plan", border: "border-state-idle-no-plan", tint: "bg-state-idle-no-plan/15", fill: "fill-state-idle-no-plan/15", stroke: "stroke-state-idle-no-plan", textFill: "fill-state-idle-no-plan", abnormal: false },
};

export const STATE_CODES = Object.keys(STATE_META) as StateCode[];

export function stateMeta(state: string | null | undefined): StateMeta {
  return STATE_META[(state ?? "IDLE_NO_PLAN") as StateCode] ?? STATE_META.IDLE_NO_PLAN;
}

export function isStateCode(s: unknown): s is StateCode {
  return typeof s === "string" && s in STATE_META;
}

export const SEVERITY_META: Record<Severity, { Icon: LucideIcon; token: string; text: string; bg: string; border: string; tint: string }> = {
  critical: { Icon: OctagonX, token: "--severity-critical", text: "text-severity-critical", bg: "bg-severity-critical", border: "border-severity-critical", tint: "bg-severity-critical/15" },
  warning: { Icon: TriangleAlert, token: "--severity-warning", text: "text-severity-warning", bg: "bg-severity-warning", border: "border-severity-warning", tint: "bg-severity-warning/15" },
  info: { Icon: Info, token: "--severity-info", text: "text-severity-info", bg: "bg-severity-info", border: "border-severity-info", tint: "bg-severity-info/15" },
};

export function severityMeta(s: string | null | undefined) {
  return SEVERITY_META[(s ?? "info") as Severity] ?? { ...SEVERITY_META.info, Icon: CircleAlert };
}

const colorCache = new Map<string, string>();
let probe: CanvasRenderingContext2D | null = null;

/** Any CSS colour (oklch tokens included) → "rgb(r, g, b)" — the chart library parses only sRGB. */
export function toRgb(color: string): string {
  const hit = colorCache.get(color);
  if (hit) return hit;
  if (typeof document === "undefined") return "#888888";
  probe ??= document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  if (!probe) return color;
  probe.clearRect(0, 0, 1, 1);
  probe.fillStyle = "#888888";
  probe.fillStyle = color;
  probe.fillRect(0, 0, 1, 1);
  const d = probe.getImageData(0, 0, 1, 1).data;
  const out = `rgb(${d[0] ?? 0}, ${d[1] ?? 0}, ${d[2] ?? 0})`;
  colorCache.set(color, out);
  return out;
}

/** Resolve a CSS custom property of the themed root as an sRGB colour (for charts). */
export function cssVar(name: string, el?: Element | null): string {
  if (typeof window === "undefined") return "#888888";
  const v = getComputedStyle(el ?? document.documentElement).getPropertyValue(name).trim();
  return v ? toRgb(v) : "#888888";
}
