"use client";

import type { EChartsOption, LineSeriesOption } from "echarts";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import { usePlant } from "@/components/plant-context";
import type { FanPoint, PlanProgress } from "@/lib/api/types";

/**
 * Cumulative plan and fact by day + forecast fan P10–P90 to the month end + the 5 500 and 4 800
 * lines (SPEC §13.2 /director); the what-if scenario is drawn as a second (blue) fan.
 */
export function PlanChart({ plan, fan, scenarioFan }: { plan: PlanProgress; fan: FanPoint[] | null; scenarioFan: FanPoint[] | null }) {
  const t = useTranslations("director.chart");
  const plant = usePlant();
  const c = useChartColors();
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    const dates = plan.daily.map((d) => d.date);
    const asOfDate = plan.as_of.slice(0, 10);
    const factEnd = dates.findIndex((d) => d > asOfDate);
    const fact = plan.daily.map((d, i) => (factEnd === -1 || i < factEnd ? d.cum_output : null));
    const targets = Object.entries(plan.targets);
    const planSeries: LineSeriesOption[] = targets.map(([key, tg], i) => ({
      name: t(key === "plant_target" ? "planTarget" : "planLine", { qty: plant.fmt.int(tg.qty) }),
      type: "line",
      data: plan.daily.map((d) => d.cum_plan[key] ?? null),
      showSymbol: false,
      lineStyle: { color: c.muted, type: i === 0 ? "dashed" : "dotted", width: 1.25, opacity: 0.8 },
      itemStyle: { color: c.muted },
      z: 2,
      markLine:
        i === 0
          ? {
              silent: true,
              symbol: "none",
              lineStyle: { color: c.muted, type: [2, 4], width: 1, opacity: 0.8 },
              label: { color: c.fg, fontSize: 11, fontWeight: 600, position: "end", formatter: (p: { value?: unknown }) => plant.fmt.int(Number(p.value)) },
              data: targets.map(([, x]) => ({ yAxis: x.qty })),
            }
          : undefined,
    }));
    const fanSeries = (points: FanPoint[] | null, color: string, prefix: string, label: string): LineSeriesOption[] => {
      if (!points?.length) return [];
      const byDate = new Map(points.map((p) => [p.date, p]));
      const p10 = dates.map((d) => byDate.get(d)?.p10 ?? null);
      const band = dates.map((d) => {
        const p = byDate.get(d);
        return p ? p.p90 - p.p10 : null;
      });
      const p50 = dates.map((d) => byDate.get(d)?.p50 ?? null);
      return [
        { name: `${prefix}-p10`, type: "line", data: p10, stack: prefix, showSymbol: false, lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
        {
          name: `${prefix}-band`,
          type: "line",
          data: band,
          stack: prefix,
          showSymbol: false,
          lineStyle: { opacity: 0 },
          areaStyle: { color, opacity: 0.35 },
          silent: true,
          tooltip: { show: false },
        },
        { name: label, type: "line", data: p50, showSymbol: false, lineStyle: { color, width: 2.5 }, itemStyle: { color }, z: 4 },
      ];
    };
    const fanByDate = new Map((fan ?? []).map((p) => [p.date, p]));
    const scenByDate = new Map((scenarioFan ?? []).map((p) => [p.date, p]));
    return {
      ...baseOption(c),
      grid: { left: 52, right: 56, top: 16, bottom: 56 },
      legend: {
        bottom: 0,
        left: "center",
        textStyle: { color: c.muted, fontSize: 11 },
        itemWidth: 16,
        itemHeight: 8,
        data: [t("fact"), t("forecast"), ...(scenarioFan ? [t("scenario")] : []), ...planSeries.map((s) => String(s.name))],
      },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLabel: { color: c.muted, formatter: (v: string) => plant.fmt.dayMonth(v), interval: 4 },
        axisLine: { lineStyle: { color: c.border } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.int(v) },
        splitLine: { lineStyle: { color: c.border, opacity: 0.6 } },
      },
      tooltip: {
        ...(baseOption(c).tooltip as object),
        trigger: "axis",
        formatter: (raw: unknown) => {
          const ps = raw as Array<{ axisValue: string; seriesName: string; value: number | null; color: string }>;
          const date = ps[0]?.axisValue ?? "";
          const rows = ps
            .filter((p) => p.value != null && !p.seriesName.includes("-"))
            .map((p) => `<span style="color:${p.color}">●</span> ${p.seriesName}: <b>${plant.fmt.int(p.value)}</b>`);
          const f = fanByDate.get(date);
          if (f) rows.push(`${t("fanRange")}: ${plant.fmt.int(f.p10)}–${plant.fmt.int(f.p90)}`);
          const s = scenByDate.get(date);
          if (s) rows.push(`${t("scenarioRange")}: ${plant.fmt.int(s.p10)}–${plant.fmt.int(s.p90)}`);
          return `<b>${plant.fmt.date(date)}</b><br/>${rows.join("<br/>")}`;
        },
      },
      series: [
        ...planSeries,
        ...fanSeries(fan, c.normal, "base", t("forecast")),
        ...fanSeries(scenarioFan, c.info, "scen", t("scenario")),
        {
          name: t("fact"),
          type: "line",
          data: fact,
          showSymbol: false,
          lineStyle: { color: c.fg, width: 2.5 },
          itemStyle: { color: c.fg },
          areaStyle: { color: c.fg, opacity: 0.05 },
          z: 5,
          markLine: {
            silent: true,
            symbol: "none",
            label: { color: c.muted, fontSize: 10, formatter: t("today"), position: "insideEndTop" },
            lineStyle: { color: c.border, type: "solid" },
            data: factEnd > 0 ? [{ xAxis: dates[factEnd - 1] ?? "" }] : [],
          },
        },
      ],
    };
  }, [c, plan, fan, scenarioFan, plant, t]);
  return option ? <EChart option={option} height={340} ariaLabel={t("label")} /> : <div style={{ height: 340 }} />;
}
