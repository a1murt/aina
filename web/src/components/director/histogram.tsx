"use client";

import type { CustomSeriesOption, EChartsOption } from "echarts";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import { usePlant } from "@/components/plant-context";
import type { ForecastResult } from "@/lib/api/types";

const HEIGHT = 260;

interface Bin {
  lo: number;
  hi: number;
  share: number;
}

function binsOf(f: ForecastResult | null): Bin[] {
  const h = f?.histogram;
  if (!h?.counts.length || h.edges.length !== h.counts.length + 1) return [];
  const total = h.counts.reduce((a, b) => a + b, 0) || 1;
  return h.counts.map((n, i) => ({ lo: h.edges[i] as number, hi: h.edges[i + 1] as number, share: n / total }));
}

type RenderApi = {
  value: (dim: number) => number;
  coord: (v: [number, number]) => [number, number];
};

/**
 * «Распределение выпуска на 31.10» (VIS-6, SPEC §13.3): the Monte-Carlo histogram of the month
 * output from POST /forecast (`histogram.edges/counts`), as a share of runs. Bins at or above
 * the line plan are drawn in the foreground colour (a bin crossing the plan is split at it), the
 * plan and the plant target are marked; a what-if scenario is overlaid as a blue outline.
 */
export function ForecastHistogram({
  base,
  scenario,
  lineQty,
  targetQty,
}: {
  base: ForecastResult;
  scenario: ForecastResult | null;
  lineQty: number | null;
  targetQty: number | null;
}) {
  const t = useTranslations("director.histogram");
  const plant = usePlant();
  const c = useChartColors();
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    const baseBins = binsOf(base);
    const scenBins = binsOf(scenario);
    const all = [...baseBins, ...scenBins];
    if (!all.length) return null;
    let lo = Math.min(...all.map((b) => b.lo));
    let hi = Math.max(...all.map((b) => b.hi));
    const span = Math.max(1, hi - lo);
    // a plan just outside the simulated range is still worth showing
    const near = (q: number | null) => q != null && q >= lo - span * 0.6 && q <= hi + span * 0.6;
    if (near(lineQty)) {
      lo = Math.min(lo, lineQty as number);
      hi = Math.max(hi, lineQty as number);
    }
    const pad = (hi - lo) * 0.04;
    lo -= pad;
    hi += pad;
    const cut = lineQty ?? Infinity;

    const rects = (bins: Bin[], kind: "base" | "scen"): CustomSeriesOption => ({
      type: "custom",
      name: kind === "base" ? t("base") : t("scenario"),
      data: bins.map((b) => [b.lo, b.hi, b.share]),
      encode: { x: [0, 1], y: 2 },
      z: kind === "base" ? 2 : 3,
      renderItem: (_params: unknown, api: unknown) => {
        const a = api as RenderApi;
        const x0 = a.value(0);
        const x1 = a.value(1);
        const v = a.value(2);
        const [px0, py] = a.coord([x0, v]);
        const [px1, pyBase] = a.coord([x1, 0]);
        const h = Math.max(0, pyBase - py);
        const gap = 1;
        if (kind === "scen") {
          return {
            type: "rect",
            shape: { x: px0 + gap / 2, y: py, width: Math.max(1, px1 - px0 - gap), height: h },
            style: { fill: c.info, opacity: 0.22, stroke: c.info, lineWidth: 1.25 },
          };
        }
        const rect = (pa: number, pb: number, reach: boolean) => ({
          type: "rect" as const,
          shape: { x: pa, y: py, width: Math.max(0.5, pb - pa), height: h },
          style: { fill: reach ? c.fg : c.muted, opacity: reach ? 0.85 : 0.35 },
        });
        if (x1 <= cut) return rect(px0 + gap / 2, px1 - gap / 2, false);
        if (x0 >= cut) return rect(px0 + gap / 2, px1 - gap / 2, true);
        const [pc] = a.coord([cut, 0]);
        return { type: "group", children: [rect(px0 + gap / 2, pc, false), rect(pc, px1 - gap / 2, true)] };
      },
    });

    const marks: Array<{ xAxis: number; label: string; color: string }> = [];
    if (near(lineQty)) marks.push({ xAxis: lineQty as number, label: t("planMark", { qty: plant.fmt.int(lineQty) }), color: c.fg });
    if (near(targetQty)) marks.push({ xAxis: targetQty as number, label: t("targetMark", { qty: plant.fmt.int(targetQty) }), color: c.muted });

    const baseSeries = rects(baseBins, "base");
    baseSeries.markLine = {
      silent: true,
      symbol: "none",
      data: marks.map((m) => ({
        xAxis: m.xAxis,
        lineStyle: { color: m.color, type: "dashed", width: 1.25 },
        label: { formatter: m.label, color: m.color, fontSize: 11, fontWeight: 600, position: "end", distance: 4 },
      })),
    };

    const off = targetQty != null && !near(targetQty) && targetQty > hi;
    return {
      ...baseOption(c),
      animation: false,
      grid: { left: 44, right: 16, top: 26, bottom: 30 },
      xAxis: {
        type: "value",
        min: lo,
        max: hi,
        splitNumber: 4,
        axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.int(v), hideOverlap: true, showMinLabel: false, showMaxLabel: false },
        axisLine: { lineStyle: { color: c.border } },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        splitNumber: 3,
        axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.axisPct(v) },
        splitLine: { lineStyle: { color: c.border, opacity: 0.6 } },
      },
      tooltip: {
        ...(baseOption(c).tooltip as object),
        trigger: "item",
        formatter: (raw: unknown) => {
          const p = raw as { seriesName: string; value: [number, number, number] };
          const [a, b, share] = p.value;
          return `${p.seriesName}<br/>${t("tooltip", { lo: plant.fmt.int(a), hi: plant.fmt.int(b), share: plant.fmt.pct(share, 1) })}`;
        },
      },
      graphic: off
        ? [{ type: "text", right: 18, top: 34, style: { text: t("offAxis", { qty: plant.fmt.int(targetQty) }), fill: c.muted, fontSize: 11 } }]
        : [],
      series: [baseSeries, ...(scenBins.length ? [rects(scenBins, "scen")] : [])],
    } as EChartsOption;
  }, [c, base, scenario, lineQty, targetQty, plant, t]);
  return option ? <EChart option={option} height={HEIGHT} ariaLabel={t("label")} /> : <div style={{ height: HEIGHT }} />;
}

export const HISTOGRAM_HEIGHT = HEIGHT;
