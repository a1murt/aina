"use client";

import type { EChartsOption } from "echarts";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, FileText, Loader2, Plus } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useCallback, useMemo, useState, type ReactNode } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import { usePlant } from "@/components/plant-context";
import type { BottleneckLossItem } from "@/components/director/losses";
import { Skeleton } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api/client";
import type { KpiRow, LeverResult, ReportView, ShiftsNeeded } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/**
 * KPI tile: label, big number, secondary lines; `tone` colours only deviations (ISA-101).
 * `loading` keeps the label and shimmers only the number and the sub line (VIS-10).
 */
export function Tile({
  label,
  value,
  sub,
  tone,
  icon,
  badge,
  testid,
  loading = false,
  className,
}: {
  label: string;
  value?: ReactNode;
  sub?: ReactNode;
  tone?: "critical" | "warning" | null;
  icon?: ReactNode;
  badge?: ReactNode;
  testid?: string;
  loading?: boolean;
  className?: string;
}) {
  return (
    <div
      data-testid={testid}
      aria-busy={loading || undefined}
      className={cn(
        "flex min-h-[124px] flex-col justify-between gap-1 rounded-lg border bg-card p-3.5",
        tone === "critical" && "border-l-4 border-l-severity-critical",
        tone === "warning" && "border-l-4 border-l-severity-warning",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
        {badge}
      </div>
      {loading ? (
        <>
          <Skeleton className="h-[34px] w-28" />
          <Skeleton className="h-3.5 w-36" />
        </>
      ) : (
        <>
          <div className="flex items-center gap-2">
            {icon}
            <p className="text-[28px] leading-tight font-semibold tracking-tight tabular-nums">{value}</p>
          </div>
          <div className="space-y-0.5 text-xs text-muted-foreground tabular-nums">{sub}</div>
        </>
      )}
    </div>
  );
}

/**
 * OEE by area (month to date) with the bottleneck mark, and defects against the norm. Targets
 * come from the rules config; they are drawn as a light zone behind the bars so the value
 * labels are never struck through (VIS-16).
 */
export function AreaChart({
  rows,
  bottleneckArea,
  oeeTarget,
  defectLimit,
}: {
  rows: KpiRow[];
  bottleneckArea: string | null;
  oeeTarget: number | null;
  defectLimit: number | null;
}) {
  const t = useTranslations("director.areas");
  const plant = usePlant();
  const c = useChartColors();
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    const names = rows.map((r) => `${plant.name(plant.areas[r.code], r.code)}${r.code === bottleneckArea ? " ⧗" : ""}`);
    const defectMax = Math.max(...rows.map((r) => r.defect_rate ?? 0), defectLimit ?? 0) * 1.3 || 0.01;
    const zoneLabel = { color: c.muted, fontSize: 10, position: "insideTop" as const, distance: -12 };
    return {
      ...baseOption(c),
      grid: [
        { left: 110, right: 56, top: 22, height: "34%" },
        { left: 110, right: 56, bottom: 26, height: "34%" },
      ],
      xAxis: [
        { type: "value", gridIndex: 0, max: 1, show: false },
        { type: "value", gridIndex: 1, show: false, max: defectMax },
      ],
      yAxis: [
        { type: "category", gridIndex: 0, data: names, inverse: true, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: c.fg, fontSize: 12 } },
        { type: "category", gridIndex: 1, data: names, inverse: true, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: c.fg, fontSize: 12 } },
      ],
      tooltip: { ...(baseOption(c).tooltip as object), trigger: "item" },
      series: [
        {
          type: "bar",
          xAxisIndex: 0,
          yAxisIndex: 0,
          barWidth: 14,
          data: rows.map((r) => ({
            value: r.oee ?? 0,
            itemStyle: { color: r.code === bottleneckArea ? c.info : oeeTarget != null && (r.oee ?? 0) < oeeTarget ? c.warning : c.normal, borderRadius: 3 },
          })),
          label: { show: true, position: "right", color: c.fg, fontSize: 11, formatter: (p: { value?: unknown }) => plant.fmt.pct(Number(p.value), 1) },
          markArea:
            oeeTarget != null
              ? {
                  silent: true,
                  itemStyle: { color: c.normal, opacity: 0.1 },
                  label: { ...zoneLabel, formatter: t("oeeTarget", { v: plant.fmt.pct(oeeTarget, 0) }) },
                  data: [[{ xAxis: oeeTarget }, { xAxis: 1 }]],
                }
              : undefined,
          tooltip: { formatter: (p: unknown) => `${(p as { name: string }).name}: OEE ${plant.fmt.pct((p as { value: number }).value)}` },
        },
        {
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          barWidth: 14,
          data: rows.map((r) => ({
            value: r.defect_rate ?? 0,
            itemStyle: { color: defectLimit != null && (r.defect_rate ?? 0) > defectLimit ? c.alarm : c.normal, borderRadius: 3 },
          })),
          label: { show: true, position: "right", color: c.fg, fontSize: 11, formatter: (p: { value?: unknown }) => plant.fmt.pct(Number(p.value), 2) },
          markArea:
            defectLimit != null
              ? {
                  silent: true,
                  itemStyle: { color: c.normal, opacity: 0.1 },
                  label: { ...zoneLabel, formatter: t("defectLimit", { v: plant.fmt.pct(defectLimit, 0) }) },
                  data: [[{ xAxis: 0 }, { xAxis: defectLimit }]],
                }
              : undefined,
          tooltip: { formatter: (p: unknown) => `${(p as { name: string }).name}: ${t("defects")} ${plant.fmt.pct((p as { value: number }).value, 2)}` },
        },
      ],
      graphic: [
        { type: "text", left: 4, top: 2, style: { text: t("oee"), fill: c.muted, fontSize: 11 } },
        { type: "text", left: 4, top: "52%", style: { text: t("defects"), fill: c.muted, fontSize: 11 } },
      ],
    } as EChartsOption;
  }, [c, rows, bottleneckArea, oeeTarget, defectLimit, plant, t]);
  return option ? <EChart option={option} height={320} ariaLabel={t("label")} /> : <div style={{ height: 320 }} />;
}

/** Break a label into lines of whole words, about `max` characters each. */
function wrapWords(text: string, max: number): string {
  const lines: string[] = [];
  for (const word of text.split(" ")) {
    const last = lines[lines.length - 1];
    if (last !== undefined && last.length + 1 + word.length <= max) lines[lines.length - 1] = `${last} ${word}`;
    else lines.push(word);
  }
  return lines.join("\n");
}

/**
 * Label of a loss item: category · unit or line name · reason. The function is stable between
 * renders (chart options list it in their memo deps: a new closure would redraw the chart).
 */
export function useLossLabel() {
  const tc = useTranslations("lossCategory");
  const plant = usePlant();
  return useCallback(
    (i: Pick<BottleneckLossItem, "category" | "equipment" | "line" | "reason_code">, sep = " · "): string =>
      [
        tc(i.category as "speed"),
        plant.entityName(i.equipment ?? i.line),
        i.reason_code ? plant.name(plant.reasons[i.reason_code], i.reason_code) : null,
      ]
        .filter(Boolean)
        .join(sep),
    [tc, plant],
  );
}

/**
 * Pareto x-axis labels: category and unit wrapped at word boundaries (never a word broken in two,
 * VIS-16); the reason is added as a further line only where two bars would read the same.
 */
function paretoAxisLabels(top: BottleneckLossItem[], label: ReturnType<typeof useLossLabel>): string[] {
  const wrap = (text: string) =>
    text
      .split("\n")
      .map((l) => wrapWords(l, 15))
      .join("\n");
  const short = top.map((i) => label({ ...i, reason_code: null }, "\n"));
  return top.map((i, k) => {
    const s = short[k] as string;
    const clash = short.some((o, j) => j !== k && o === s);
    return wrap(clash && i.reason_code ? label(i, "\n") : s);
  });
}

/**
 * Top-5 losses on the bottleneck (Pareto in cars, WIN-4 / DEMO-14) with ₸ in the tooltip. The
 * items are already weighted by the bottleneck share of their line (see `bottleneckLosses`).
 */
export function LossesPareto({ items, total }: { items: BottleneckLossItem[]; total: number }) {
  const t = useTranslations("director.losses");
  const plant = usePlant();
  const label = useLossLabel();
  const c = useChartColors();
  const top = useMemo(() => items.slice(0, 5), [items]);
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    let cum = 0;
    const cumShare = top.map((i) => (cum += i.units) / (total || 1));
    return {
      ...baseOption(c),
      grid: { left: 8, right: 44, top: 24, bottom: 8, containLabel: true },
      xAxis: {
        type: "category",
        data: paretoAxisLabels(top, label),
        axisLabel: { color: c.fg, fontSize: 10, lineHeight: 13, interval: 0, width: 100, overflow: "truncate", ellipsis: "…" },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: c.border } },
      },
      yAxis: [
        { type: "value", name: t("cars"), nameTextStyle: { color: c.muted, fontSize: 10 }, axisLabel: { color: c.muted }, splitLine: { lineStyle: { color: c.border, opacity: 0.6 } } },
        { type: "value", max: 1, axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.axisPct(v) }, splitLine: { show: false } },
      ],
      tooltip: {
        ...(baseOption(c).tooltip as object),
        trigger: "axis",
        formatter: (raw: unknown) => {
          const i = top[(raw as Array<{ dataIndex: number }>)[0]?.dataIndex ?? 0];
          if (!i) return "";
          const share = i.weight < 1 ? `<br/>${t("weight", { share: plant.fmt.pct(i.weight, 0) })}` : "";
          return `<b>${label(i)}</b><br/>${t("tooltip", { cars: plant.fmt.num(i.units, 0), min: plant.fmt.int(i.minutes), kzt: plant.fmt.money(i.kzt) })}${share}`;
        },
      },
      series: [
        {
          type: "bar",
          data: top.map((i) => i.units),
          barWidth: "52%",
          itemStyle: { color: c.normal, borderRadius: [3, 3, 0, 0] },
          label: { show: true, position: "top", color: c.fg, fontSize: 11, formatter: (p: { value?: unknown }) => plant.fmt.int(Number(p.value)) },
        },
        { type: "line", yAxisIndex: 1, data: cumShare, symbolSize: 5, lineStyle: { color: c.muted, width: 1.5 }, itemStyle: { color: c.muted } },
      ],
    };
  }, [c, top, total, plant, label, t]);
  return option ? <EChart option={option} height={260} ariaLabel={t("label")} /> : <div style={{ height: 260 }} />;
}

/** Lever text from its kind and params (SPEC §10.4). */
export function useLeverLabel() {
  const t = useTranslations("director.levers.kind");
  const plant = usePlant();
  return (l: LeverResult): string => {
    const p = l.params as Record<string, unknown>;
    switch (l.kind) {
      case "extra_shift":
        return t("extra_shift", { date: plant.fmt.dayMonth(String(p.date ?? "")), shifts: Array.isArray(p.shifts) ? p.shifts.join(", ") : "" });
      case "filter_policy":
        return t("filter_policy");
      case "defect_norm":
        return t("defect_norm", { area: plant.entityName(String(p.area ?? "")), from: plant.fmt.pct(Number(p.from), 1), to: plant.fmt.pct(Number(p.to), 1) });
      case "eliminate_failures":
        return t("eliminate_failures", { equipment: plant.entityName(String(p.equipment ?? "")) });
      case "buffer_capacity":
        return t("buffer_capacity", { buffer: plant.entityName(String(p.buffer ?? "")), from: String(p.from ?? ""), to: String(p.to ?? "") });
      default:
        return l.id;
    }
  };
}

export function LeversTable({ levers, targetKey, onApply }: { levers: LeverResult[]; targetKey: string; onApply: (l: LeverResult) => void }) {
  const t = useTranslations("director.levers");
  const plant = usePlant();
  const label = useLeverLabel();
  const top = [...levers].sort((a, b) => a.rank - b.rank).slice(0, 5);
  return (
    <table className="w-full text-sm tabular-nums" data-testid="levers">
      <thead>
        <tr className="text-xs text-muted-foreground">
          <th className="pb-1.5 text-left font-medium">{t("lever")}</th>
          <th className="pb-1.5 text-right font-medium">{t("dCars")}</th>
          <th className="pb-1.5 text-right font-medium">{t("dP")}</th>
          <th className="pb-1.5 text-right font-medium">{t("kzt")}</th>
          <th className="pb-1.5" />
        </tr>
      </thead>
      <tbody>
        {top.map((l) => (
          <tr key={l.id} className="border-t">
            <td className="py-2 pr-2">
              <span className="mr-1.5 text-xs text-muted-foreground">{l.rank}.</span>
              {label(l)}
            </td>
            <td className="py-2 text-right font-semibold">{plant.fmt.signed(l.delta_p50, 0)}</td>
            <td className="py-2 text-right">{plant.fmt.signed((l.delta_p_reach[targetKey] ?? 0) * 100, 0)} {t("pp")}</td>
            <td className="py-2 text-right">
              <LeverMoney effect={l.effect_kzt} />
            </td>
            <td className="py-2 pl-2 text-right">
              <button
                type="button"
                onClick={() => onApply(l)}
                data-testid={`apply-${l.id}`}
                className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium whitespace-nowrap hover:bg-accent"
              >
                <Plus aria-hidden className="size-3.5" />
                {t("apply")}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Money effect of a lever, ₸ per month: the median, or — when the median of a skewed
 * distribution is 0 (rare failures) — the mean, labelled «в среднем» (CONS-21).
 */
export function leverMoney(e: LeverResult["effect_kzt"]): { value: number; mean: boolean } | null {
  if (e.p50 > 0) return { value: e.p50, mean: false };
  if (e.mean > 0) return { value: e.mean, mean: true };
  return null;
}

/**
 * Money effect of a scenario, ₸ per month, of either sign: {@link leverMoney} for a gain, else
 * the median (a worse scenario shows its loss, a zero effect shows 0).
 */
export function scenarioMoney(e: LeverResult["effect_kzt"]): { value: number; mean: boolean } {
  return leverMoney(e) ?? { value: e.p50 !== 0 ? e.p50 : e.mean < 0 ? e.mean : 0, mean: e.p50 === 0 && e.mean < 0 };
}

function LeverMoney({ effect }: { effect: LeverResult["effect_kzt"] }) {
  const t = useTranslations("director.levers");
  const plant = usePlant();
  const m = leverMoney(effect);
  if (!m) return <>—</>;
  return (
    <span title={m.mean ? t("meanHint") : undefined}>
      {plant.fmt.money(m.value)}
      {m.mean ? <span className="block text-[11px] leading-tight text-muted-foreground">{t("mean")}</span> : null}
    </span>
  );
}

/** «Сколько смен нужно для 5 500»: Saturdays only vs weekends and holidays. */
export function ShiftsNeededView({ data, qty }: { data: Record<string, ShiftsNeeded> | undefined; qty: number }) {
  const t = useTranslations("director.shifts");
  const plant = usePlant();
  if (!data) return null;
  return (
    <ul className="space-y-2.5 text-sm" data-testid="shifts-needed">
      {Object.entries(data).map(([set, s]) => (
        <li key={set} className="rounded-md border p-2.5">
          <p className="text-xs text-muted-foreground">{t(set === "saturdays" ? "saturdays" : "weekends")}</p>
          {s.shifts != null ? (
            <>
              <p className="font-semibold">{t("needed", { n: s.shifts, qty: plant.fmt.int(qty), p: plant.fmt.pct(s.p_reach ?? 0, 0) })}</p>
              <p className="mt-0.5 text-xs text-muted-foreground tabular-nums">
                {s.dates.map((d) => `${plant.fmt.dayMonth(String(d.date))} ${String(d.shift ?? "")}`).join(" · ")}
              </p>
            </>
          ) : (
            <p className="font-semibold">{t("notEnough", { n: s.candidates, p: plant.fmt.pct(s.p_max, 0) })}</p>
          )}
        </li>
      ))}
    </ul>
  );
}

export function LastReport({
  data,
  canGenerate,
}: {
  data: { date: string; shift: string; report: ReportView | null } | undefined;
  canGenerate: boolean;
}) {
  const t = useTranslations("director.report");
  const locale = useLocale();
  const plant = usePlant();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const generate = useMutation({
    mutationFn: () =>
      api<ReportView>("/api/v1/reports/shift", {
        method: "POST",
        body: { date: data?.date, shift: data?.shift, lang: locale === "kk" ? "kk" : "ru", mode: "auto" },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["reports"] }),
  });
  if (!data) return null;
  const r = data.report;
  if (!r) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-muted-foreground">{t("none")}</p>
        {canGenerate && data.shift ? (
          <button
            type="button"
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm font-medium hover:bg-accent disabled:opacity-50"
          >
            {generate.isPending ? <Loader2 aria-hidden className="size-4 animate-spin" /> : <FileText aria-hidden className="size-4" />}
            {t("generate", { date: plant.fmt.date(data.date), code: data.shift })}
          </button>
        ) : null}
        {generate.error ? <p className="text-xs text-severity-critical">{generate.error instanceof ApiError ? generate.error.detail : String(generate.error)}</p> : null}
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="font-medium">{t("shift", { date: plant.fmt.date(r.shift_date), code: r.shift_code })}</p>
          <p className="text-xs text-muted-foreground">{t("generated", { by: r.generated_by, at: plant.fmt.dateTime(r.created_ts) })}</p>
        </div>
        {r.numbers_verified ? (
          <Badge tone="outline">
            <BadgeCheck aria-hidden />
            {t("verified")}
          </Badge>
        ) : null}
      </div>
      <p className="mt-2 line-clamp-4 text-sm whitespace-pre-line text-muted-foreground">{r.text}</p>
      <button type="button" onClick={() => setOpen(true)} className="mt-2 inline-flex items-center gap-1 text-sm font-medium underline-offset-4 hover:underline">
        <FileText aria-hidden className="size-4" />
        {t("open")}
      </button>
      <Sheet open={open} onClose={() => setOpen(false)} title={t("shift", { date: plant.fmt.date(r.shift_date), code: r.shift_code })}>
        <article className="px-5 py-4 text-sm leading-relaxed whitespace-pre-line">{r.text}</article>
      </Sheet>
    </div>
  );
}
