"use client";

import type { EChartsOption } from "echarts";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import type { SignalSpec } from "@/lib/api/types";

/** Telemetry sparkline with warning and limit lines (ISA-101: amber warning, red limit). */
export function Sparkline({
  points,
  spec,
  height = 64,
  label,
}: {
  points: Array<[number, number | null]>;
  spec: SignalSpec;
  height?: number;
  label: string;
}) {
  const c = useChartColors();
  if (!c) return <div style={{ height }} />;
  const marks: Array<{ yAxis: number; lineStyle: { color: string; type: "dashed" | "solid"; width: number } }> = [];
  for (const v of [spec.warn_lo, spec.warn_hi]) if (v != null) marks.push({ yAxis: v, lineStyle: { color: c.warning, type: "dashed", width: 1 } });
  for (const v of [spec.limit_lo, spec.limit_hi]) if (v != null) marks.push({ yAxis: v, lineStyle: { color: c.alarm, type: "solid", width: 1.25 } });
  const values = points.map((p) => p[1]).filter((v): v is number => v != null);
  const bounds = [...values, ...marks.map((m) => m.yAxis)];
  const lo = bounds.length ? Math.min(...bounds) : spec.lo;
  const hi = bounds.length ? Math.max(...bounds) : spec.hi;
  const pad = (hi - lo) * 0.12 || 1;
  const option: EChartsOption = {
    ...baseOption(c),
    animation: false,
    grid: { left: 2, right: 2, top: 4, bottom: 4 },
    xAxis: { type: "time", show: false },
    yAxis: { type: "value", show: false, min: lo - pad, max: hi + pad },
    tooltip: { ...(baseOption(c).tooltip as object), trigger: "axis", valueFormatter: (v) => `${typeof v === "number" ? v.toFixed(2) : v} ${spec.unit}` },
    series: [
      {
        type: "line",
        data: points,
        showSymbol: false,
        lineStyle: { color: c.fg, width: 1.5 },
        areaStyle: { color: c.normal, opacity: 0.12 },
        markLine: { silent: true, symbol: "none", label: { show: false }, data: marks },
      },
    ],
  };
  return <EChart option={option} height={height} ariaLabel={label} />;
}
