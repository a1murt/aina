"use client";

import type { EChartsOption, LineSeriesOption } from "echarts";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import type { DirectorPlan } from "@/components/director/data";
import { usePlant } from "@/components/plant-context";
import type { ForecastResult } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const HEIGHT = 340;
const GRID = { left: 52, right: 118, top: 26, bottom: 56 };
/** The «end of month» zoom (VIS-6: the gap between P50 and the plan line): at most this many days… */
const ZOOM_DAYS = 10;
/** …and at least this many, starting where the lines enter the narrowed y range. */
const ZOOM_MIN_DAYS = 4;
/** Minimum vertical distance between two end-of-line labels, px. */
const LABEL_GAP = 14;

type Pt = [number, number | null];

/** Days since the month start (plant zone) of an instant: 16.10 07:00 → 15.29. */
function dayOffset(iso: string, monthStart: string, tz: string): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date(iso))
      .map((p) => [p.type, p.value]),
  );
  const day = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day));
  const start = Date.parse(`${monthStart}T00:00:00Z`);
  return (day - start) / 86_400_000 + (Number(parts.hour) * 60 + Number(parts.minute)) / 1440;
}

/** Day index (0 = first day of the month) of a calendar date. */
function dayIndex(date: string, monthStart: string): number {
  return Math.round((Date.parse(`${date}T00:00:00Z`) - Date.parse(`${monthStart}T00:00:00Z`)) / 86_400_000);
}

/** Spread end-of-line labels that would overlap: returns a vertical pixel offset per label. */
function spreadLabels(values: number[], yMin: number, yMax: number): number[] {
  const plot = HEIGHT - GRID.top - GRID.bottom;
  const px = values.map((v) => plot * (1 - (v - yMin) / Math.max(1e-9, yMax - yMin)));
  const order = px.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  const placed: number[] = [];
  for (const o of order) {
    const prev = placed.length ? (placed[placed.length - 1] as number) : -Infinity;
    placed.push(Math.max(o.y, prev + LABEL_GAP));
  }
  // centre the shift around the original positions so a pair moves apart symmetrically
  const shift = order.reduce((s, o, k) => s + ((placed[k] as number) - o.y), 0) / Math.max(1, order.length);
  const out = new Array<number>(values.length).fill(0);
  order.forEach((o, k) => {
    out[o.i] = (placed[k] as number) - o.y - shift;
  });
  return out;
}

/**
 * Cumulative plan and fact + the forecast fan P10–P90 to the month end (SPEC §13.2 /director).
 * The x axis is continuous plant time (days since the month start): cumulative values sit at the
 * end of their day, fact ends and the forecast starts at the same point «now, MTD» (DEMO-10).
 * P50 and its fan are drawn in the foreground colour, plan lines are muted and labelled at their
 * ends; the what-if scenario is a second (blue) fan. `zoom` shows the last days with a narrow y
 * axis so the gap between P50 and the plan line is visible (VIS-6).
 */
export function PlanChart({
  plan,
  forecast,
  scenario,
  lineKey,
  zoom,
}: {
  plan: DirectorPlan;
  forecast: ForecastResult | null;
  scenario: ForecastResult | null;
  lineKey: string | null;
  zoom: boolean;
}) {
  const t = useTranslations("director.chart");
  const plant = usePlant();
  const c = useChartColors();
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    const tz = plant.fmt.tz;
    const monthStart = `${plan.month}-01`;
    const xMax = plan.daily.length;
    const nowX = Math.min(xMax, Math.max(0, dayOffset(plan.as_of, monthStart, tz)));
    const dateAt = (x: number) => plan.daily[Math.min(plan.daily.length - 1, Math.max(0, x))]?.date ?? "";

    // fact: month start → end of each closed day → now
    const fact: Pt[] = [[0, 0]];
    for (const d of plan.daily) {
      const x = dayIndex(d.date, monthStart) + 1;
      if (x < nowX) fact.push([x, d.cum_output]);
    }
    fact.push([nowX, plan.mtd_output]);

    const targets = Object.entries(plan.targets).sort(([a], [b]) => (a === lineKey ? -1 : b === lineKey ? 1 : 0));
    const planPts = (key: string): Pt[] => [[0, 0], ...plan.daily.map((d) => [dayIndex(d.date, monthStart) + 1, d.cum_plan[key] ?? null] as Pt)];

    // forecast fans anchored at (forecast time, its MTD)
    const fanPts = (f: ForecastResult | null) => {
      if (!f?.fan.length) return null;
      const x0 = Math.min(xMax, Math.max(0, dayOffset(f.as_of, monthStart, tz)));
      const pts = [{ x: x0, p10: f.mtd, p50: f.mtd, p90: f.mtd }];
      for (const p of f.fan) {
        const x = dayIndex(p.date, monthStart) + 1;
        if (x > x0 + 1e-6) pts.push({ x, p10: p.p10, p50: p.p50, p90: p.p90 });
      }
      return pts;
    };
    const base = fanPts(forecast);
    const scen = fanPts(scenario);
    const end = (pts: ReturnType<typeof fanPts>) => (pts?.length ? pts[pts.length - 1] : null);
    const baseEnd = end(base);
    const scenEnd = end(scen);

    // y range: whole month from 0, or the end of the month around the line plan
    const lineQty = lineKey ? (plan.targets[lineKey]?.qty ?? null) : null;
    const allTops = [...targets.map(([, x]) => x.qty), baseEnd?.p90 ?? 0, scenEnd?.p90 ?? 0, plan.mtd_output];
    let yMin = 0;
    let yMax = Math.ceil((Math.max(...allTops) * 1.06) / 500) * 500 || 500;
    let xMin = 0;
    if (zoom) {
      const lows = [baseEnd?.p10, scenEnd?.p10, lineQty].filter((v): v is number => typeof v === "number");
      const highs = [baseEnd?.p90, scenEnd?.p90, lineQty].filter((v): v is number => typeof v === "number");
      if (lows.length) {
        yMin = Math.max(0, Math.floor((Math.min(...lows) - 250) / 100) * 100);
        yMax = Math.ceil((Math.max(...highs) + 50) / 50) * 50;
      }
      // start the window a day before the first line (plan or forecast) enters the y range
      const entries = [
        ...(lineKey ? planPts(lineKey) : []),
        ...(base ?? []).map((p) => [p.x, p.p90] as Pt),
        ...(scen ?? []).map((p) => [p.x, p.p90] as Pt),
      ]
        .filter(([, y]) => y != null && y >= yMin)
        .map(([x]) => x);
      const enter = entries.length ? Math.min(...entries) : xMax - ZOOM_DAYS;
      xMin = Math.max(0, xMax - ZOOM_DAYS, Math.min(xMax - ZOOM_MIN_DAYS, Math.floor(enter) - 1));
    }

    // end-of-line labels: plan lines, base P50, scenario P50 — spread when they would overlap
    const labelled: Array<{ key: string; value: number; text: string; color: string }> = [];
    for (const [key, tg] of targets) {
      if (tg.qty < yMin || tg.qty > yMax) continue;
      labelled.push({ key: `plan:${key}`, value: tg.qty, text: t(key === lineKey ? "endPlan" : "endTarget", { n: plant.fmt.int(tg.qty) }), color: c.muted });
    }
    if (baseEnd) labelled.push({ key: "base", value: baseEnd.p50, text: t("endP50", { n: plant.fmt.int(baseEnd.p50) }), color: c.fg });
    if (scenEnd) labelled.push({ key: "scen", value: scenEnd.p50, text: t("endScenario", { n: plant.fmt.int(scenEnd.p50) }), color: c.info });
    const offsets = spreadLabels(
      labelled.map((l) => l.value),
      yMin,
      yMax,
    );
    const endLabel = (key: string) => {
      const i = labelled.findIndex((l) => l.key === key);
      const l = labelled[i];
      if (!l) return undefined;
      return {
        show: true,
        formatter: l.text,
        color: l.color,
        fontSize: 11,
        fontWeight: key === "base" || key === "scen" ? (700 as const) : (500 as const),
        offset: [4, offsets[i] ?? 0] as [number, number],
      };
    };

    const planSeries: LineSeriesOption[] = targets.map(([key, tg]) => ({
      name: t(key === lineKey ? "planLine" : "planTarget", { qty: plant.fmt.int(tg.qty) }),
      type: "line",
      data: planPts(key),
      showSymbol: false,
      lineStyle: { color: c.muted, type: key === lineKey ? "dashed" : "dotted", width: 1.25, opacity: 0.7 },
      itemStyle: { color: c.muted },
      endLabel: endLabel(`plan:${key}`),
      z: 2,
    }));

    const fanSeries = (pts: ReturnType<typeof fanPts>, color: string, prefix: "base" | "scen", label: string, dashed: boolean): LineSeriesOption[] => {
      if (!pts) return [];
      return [
        {
          name: `${prefix}-p10`,
          type: "line",
          data: pts.map((p) => [p.x, p.p10]),
          stack: prefix,
          showSymbol: false,
          lineStyle: { opacity: 0 },
          silent: true,
          tooltip: { show: false },
        },
        {
          name: `${prefix}-band`,
          type: "line",
          data: pts.map((p) => [p.x, p.p90 - p.p10]),
          stack: prefix,
          showSymbol: false,
          lineStyle: { opacity: 0 },
          areaStyle: { color, opacity: prefix === "base" ? 0.16 : 0.22 },
          silent: true,
          tooltip: { show: false },
        },
        {
          name: label,
          type: "line",
          data: pts.map((p) => [p.x, p.p50]),
          showSymbol: false,
          lineStyle: { color, width: 2.5, type: dashed ? [6, 4] : "solid" },
          itemStyle: { color },
          endLabel: endLabel(prefix),
          z: 4,
        },
      ];
    };

    const fanAt = (pts: ReturnType<typeof fanPts>, x: number) => pts?.find((p) => Math.abs(p.x - x) < 1e-6) ?? null;

    return {
      ...baseOption(c),
      grid: GRID,
      legend: {
        bottom: 0,
        left: "center",
        textStyle: { color: c.muted, fontSize: 11 },
        itemWidth: 16,
        itemHeight: 8,
        data: [t("fact"), ...(base ? [t("forecast")] : []), ...(scen ? [t("scenario")] : []), ...planSeries.map((s) => String(s.name))],
      },
      xAxis: {
        type: "value",
        min: xMin,
        max: xMax,
        interval: zoom ? 1 : 5,
        axisLabel: { color: c.muted, formatter: (v: number) => (v < xMax ? plant.fmt.dayMonth(dateAt(Math.round(v))) : "") },
        axisLine: { lineStyle: { color: c.border } },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        min: yMin,
        max: yMax,
        axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.int(v) },
        splitLine: { lineStyle: { color: c.border, opacity: 0.6 } },
      },
      tooltip: {
        ...(baseOption(c).tooltip as object),
        trigger: "axis",
        formatter: (raw: unknown) => {
          const ps = raw as Array<{ seriesName: string; value: [number, number | null]; color: string }>;
          const x = ps[0]?.value?.[0];
          if (x == null) return "";
          const head = Math.abs(x - nowX) < 1e-6 ? `${t("today")}, ${plant.fmt.dateShort(plan.as_of)}` : plant.fmt.date(dateAt(Math.round(x) - 1));
          const rows = ps
            .filter((p) => p.value?.[1] != null && !p.seriesName.includes("-"))
            .map((p) => `<span style="color:${p.color}">●</span> ${p.seriesName}: <b>${plant.fmt.int(p.value[1])}</b>`);
          const f = fanAt(base, x);
          if (f && f.p90 > f.p10) rows.push(`${t("fanRange")}: ${plant.fmt.int(f.p10)}–${plant.fmt.int(f.p90)}`);
          const s = fanAt(scen, x);
          if (s && s.p90 > s.p10) rows.push(`${t("scenarioRange")}: ${plant.fmt.int(s.p10)}–${plant.fmt.int(s.p90)}`);
          return `<b>${head}</b><br/>${rows.join("<br/>")}`;
        },
      },
      series: [
        ...planSeries,
        ...fanSeries(base, c.fg, "base", t("forecast"), true),
        ...fanSeries(scen, c.info, "scen", t("scenario"), false),
        {
          name: t("fact"),
          type: "line",
          data: fact,
          showSymbol: false,
          lineStyle: { color: c.fg, width: 2.5 },
          itemStyle: { color: c.fg },
          areaStyle: zoom ? undefined : { color: c.fg, opacity: 0.05 },
          z: 5,
          markLine: {
            silent: true,
            symbol: "none",
            label: { color: c.fg, fontSize: 11, formatter: t("today"), position: "end", distance: 4 },
            lineStyle: { color: c.muted, type: "solid", opacity: 0.6 },
            data: nowX > xMin ? [{ xAxis: nowX }] : [],
          },
        },
      ],
    } as EChartsOption;
  }, [c, plan, forecast, scenario, lineKey, zoom, plant, t]);
  return option ? <EChart option={option} height={HEIGHT} ariaLabel={t("label")} /> : <div style={{ height: HEIGHT }} />;
}

/** Segmented switch «Месяц | Конец месяца» for {@link PlanChart}. */
export function ZoomSwitch({ zoom, onChange }: { zoom: boolean; onChange: (zoom: boolean) => void }) {
  const t = useTranslations("director.chart");
  return (
    <div role="radiogroup" aria-label={t("zoomLabel")} className="inline-flex rounded-md border p-0.5 text-xs">
      {[false, true].map((z) => (
        <button
          key={String(z)}
          type="button"
          role="radio"
          aria-checked={zoom === z}
          data-testid={z ? "chart-zoom-end" : "chart-zoom-month"}
          onClick={() => onChange(z)}
          className={cn(
            "rounded px-2 py-1 font-medium whitespace-nowrap",
            zoom === z ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
          )}
        >
          {t(z ? "zoomEnd" : "zoomMonth")}
        </button>
      ))}
    </div>
  );
}

export const PLAN_CHART_HEIGHT = HEIGHT;
