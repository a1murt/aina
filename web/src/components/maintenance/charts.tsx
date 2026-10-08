"use client";

import type { EChartsOption } from "echarts";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import type { LimitForecast, SignalSpec } from "@/lib/api/types";

/**
 * Telemetry of one signal with warning (amber, dashed) and limit (red) lines and, when the
 * engine has a limit forecast, the projected trend to the limit (dashed, ISA-101 warning).
 */
export function SignalChart({
  points,
  spec,
  forecast,
  height = 220,
  label,
  fmtTime,
  labels,
}: {
  points: Array<[number, number | null]>;
  spec: SignalSpec;
  forecast?: LimitForecast | null;
  height?: number;
  label: string;
  fmtTime: (ms: number) => string;
  labels: { value: string; warn: string; limit: string; projection: string };
}) {
  const c = useChartColors();
  if (!c) return <div style={{ height }} />;
  const marks: Array<{ yAxis: number; name: string; lineStyle: { color: string; type: "dashed" | "solid"; width: number } }> = [];
  for (const v of [spec.warn_lo, spec.warn_hi])
    if (v != null) marks.push({ yAxis: v, name: labels.warn, lineStyle: { color: c.warning, type: "dashed", width: 1.25 } });
  for (const v of [spec.limit_lo, spec.limit_hi])
    if (v != null) marks.push({ yAxis: v, name: labels.limit, lineStyle: { color: c.alarm, type: "solid", width: 1.5 } });
  const last = [...points].reverse().find((p) => p[1] != null);
  const projection: Array<[number, number]> = [];
  if (forecast && last && forecast.limit_at && forecast.slope_per_h > 0 && (forecast.alert || (forecast.hours_to_limit ?? 99) <= 24)) {
    projection.push([last[0], forecast.level_now]);
    projection.push([Date.parse(forecast.limit_at), forecast.limit]);
  }
  const values = [...points.map((p) => p[1]), ...projection.map((p) => p[1])].filter((v): v is number => v != null);
  const bounds = [...values, ...marks.map((m) => m.yAxis)];
  const lo = bounds.length ? Math.min(...bounds) : spec.lo;
  const hi = bounds.length ? Math.max(...bounds) : spec.hi;
  const pad = (hi - lo) * 0.08 || 1;
  const window = forecast?.window ? Date.parse(forecast.window) : null;
  const option: EChartsOption = {
    ...baseOption(c),
    animation: false,
    grid: { left: 48, right: 16, top: 16, bottom: 28 },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: c.border } },
      axisLabel: { color: c.muted, formatter: (v: number) => fmtTime(v), hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      min: Math.floor(lo - pad),
      max: Math.ceil(hi + pad),
      axisLabel: { color: c.muted },
      splitLine: { lineStyle: { color: c.border, opacity: 0.5 } },
    },
    tooltip: {
      ...(baseOption(c).tooltip as object),
      trigger: "axis",
      valueFormatter: (v) => `${typeof v === "number" ? v.toFixed(2) : v} ${spec.unit}`,
    },
    series: [
      {
        name: labels.value,
        type: "line",
        data: points,
        showSymbol: false,
        lineStyle: { color: c.fg, width: 1.5 },
        markLine: {
          silent: true,
          symbol: "none",
          label: { show: true, position: "insideEndTop", color: c.muted, fontSize: 10, formatter: (p) => `${(p as { name?: string }).name ?? ""}` },
          data: marks,
        },
        markArea: window
          ? { silent: true, itemStyle: { color: c.info, opacity: 0.1 }, data: [[{ xAxis: window - 10 * 60_000 }, { xAxis: window + 10 * 60_000 }]] }
          : undefined,
      },
      ...(projection.length
        ? [
            {
              name: labels.projection,
              type: "line" as const,
              data: projection,
              showSymbol: true,
              symbolSize: 6,
              lineStyle: { color: c.warning, width: 2, type: "dashed" as const },
              itemStyle: { color: c.warning },
            },
          ]
        : []),
    ],
  };
  return <EChart option={option} height={height} ariaLabel={label} />;
}

/** p_failure history (0..1) as a small area line. */
export function ProbabilitySpark({ points, height = 48, label }: { points: Array<[number, number]>; height?: number; label: string }) {
  const c = useChartColors();
  if (!c) return <div style={{ height }} />;
  const option: EChartsOption = {
    ...baseOption(c),
    animation: false,
    grid: { left: 2, right: 2, top: 4, bottom: 4 },
    xAxis: { type: "time", show: false },
    yAxis: { type: "value", show: false, min: 0, max: 1 },
    tooltip: { ...(baseOption(c).tooltip as object), trigger: "axis", valueFormatter: (v) => `${typeof v === "number" ? Math.round(v * 100) : v} %` },
    series: [
      {
        type: "line",
        data: points,
        showSymbol: false,
        lineStyle: { color: c.alarm, width: 1.5 },
        areaStyle: { color: c.alarm, opacity: 0.12 },
      },
    ],
  };
  return <EChart option={option} height={height} ariaLabel={label} />;
}
