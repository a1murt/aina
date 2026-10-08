"use client";

import type { EChartsOption } from "echarts";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, FileText, Loader2, Plus } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useMemo, useState, type ReactNode } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import { usePlant } from "@/components/plant-context";
import { Badge } from "@/components/ui/badge";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api/client";
import type { KpiRow, LeverResult, LossesResponse, ReportView, ShiftsNeeded } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/** KPI tile: label, big number, secondary line; `tone` colours only deviations (ISA-101). */
export function Tile({
  label,
  value,
  sub,
  tone,
  icon,
  badge,
  testid,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "critical" | "warning" | null;
  icon?: ReactNode;
  badge?: ReactNode;
  testid?: string;
}) {
  return (
    <div
      data-testid={testid}
      className={cn(
        "flex min-h-[124px] flex-col justify-between rounded-lg border bg-card p-3.5",
        tone === "critical" && "border-l-4 border-l-severity-critical",
        tone === "warning" && "border-l-4 border-l-severity-warning",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
        {badge}
      </div>
      <div className="flex items-center gap-2">
        {icon}
        <p className="text-[28px] leading-tight font-semibold tracking-tight tabular-nums">{value}</p>
      </div>
      <div className="text-xs text-muted-foreground tabular-nums">{sub}</div>
    </div>
  );
}

/** OEE by area (month to date) with the bottleneck mark, and defects against the 2 % norm. */
export function AreaChart({
  rows,
  bottleneckArea,
  defectLimit,
}: {
  rows: KpiRow[];
  bottleneckArea: string | null;
  defectLimit: number;
}) {
  const t = useTranslations("director.areas");
  const plant = usePlant();
  const c = useChartColors();
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    const names = rows.map((r) => `${plant.name(plant.areas[r.code], r.code)}${r.code === bottleneckArea ? " ⧗" : ""}`);
    return {
      ...baseOption(c),
      grid: [
        { left: 110, right: 56, top: 22, height: "34%" },
        { left: 110, right: 56, bottom: 26, height: "34%" },
      ],
      xAxis: [
        { type: "value", gridIndex: 0, max: 1, show: false },
        { type: "value", gridIndex: 1, show: false, max: (v: { max: number }) => Math.max(v.max * 1.15, defectLimit * 1.6) },
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
            itemStyle: { color: r.code === bottleneckArea ? c.info : (r.oee ?? 0) < 0.85 ? c.warning : c.normal, borderRadius: 3 },
          })),
          label: { show: true, position: "right", color: c.fg, fontSize: 11, formatter: (p: { value?: unknown }) => plant.fmt.pct(Number(p.value), 1) },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: c.muted, type: "dashed" },
            label: { color: c.muted, fontSize: 10, formatter: t("oeeTarget") },
            data: [{ xAxis: 0.85 }],
          },
          tooltip: { formatter: (p: unknown) => `${(p as { name: string }).name}: OEE ${plant.fmt.pct((p as { value: number }).value)}` },
        },
        {
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          barWidth: 14,
          data: rows.map((r) => ({
            value: r.defect_rate ?? 0,
            itemStyle: { color: (r.defect_rate ?? 0) > defectLimit ? c.alarm : c.normal, borderRadius: 3 },
          })),
          label: { show: true, position: "right", color: c.fg, fontSize: 11, formatter: (p: { value?: unknown }) => plant.fmt.pct(Number(p.value), 2) },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: c.alarm, type: "dashed" },
            label: { color: c.muted, fontSize: 10, formatter: t("defectLimit", { v: plant.fmt.pct(defectLimit, 0) }) },
            data: [{ xAxis: defectLimit }],
          },
          tooltip: { formatter: (p: unknown) => `${(p as { name: string }).name}: ${t("defects")} ${plant.fmt.pct((p as { value: number }).value, 2)}` },
        },
      ],
      graphic: [
        { type: "text", left: 4, top: 2, style: { text: t("oee"), fill: c.muted, fontSize: 11 } },
        { type: "text", left: 4, top: "52%", style: { text: t("defects"), fill: c.muted, fontSize: 11 } },
      ],
    } as EChartsOption;
  }, [c, rows, bottleneckArea, defectLimit, plant, t]);
  return option ? <EChart option={option} height={320} ariaLabel={t("label")} /> : <div style={{ height: 320 }} />;
}

/** Top-5 losses (Pareto in cars) with ₸ in the tooltip. */
export function LossesPareto({ losses }: { losses: LossesResponse }) {
  const t = useTranslations("director.losses");
  const tc = useTranslations("lossCategory");
  const plant = usePlant();
  const c = useChartColors();
  const top = useMemo(() => [...losses.items].filter((i) => i.loss).sort((a, b) => b.units - a.units).slice(0, 5), [losses]);
  const total = losses.totals.units || 1;
  const option = useMemo<EChartsOption | null>(() => {
    if (!c) return null;
    let cum = 0;
    const cumShare = top.map((i) => (cum += i.units) / total);
    const label = (i: (typeof top)[number]) =>
      `${tc(i.category as "speed")} · ${i.equipment ?? i.line ?? ""}${i.reason_code ? ` · ${plant.name(plant.reasons[i.reason_code], i.reason_code)}` : ""}`;
    return {
      ...baseOption(c),
      grid: { left: 8, right: 44, top: 24, bottom: 8, containLabel: true },
      xAxis: { type: "category", data: top.map(label), axisLabel: { color: c.fg, fontSize: 10, interval: 0, width: 92, overflow: "break" }, axisTick: { show: false }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: [
        { type: "value", name: t("cars"), nameTextStyle: { color: c.muted, fontSize: 10 }, axisLabel: { color: c.muted }, splitLine: { lineStyle: { color: c.border, opacity: 0.6 } } },
        { type: "value", max: 1, axisLabel: { color: c.muted, formatter: (v: number) => `${Math.round(v * 100)}%` }, splitLine: { show: false } },
      ],
      tooltip: {
        ...(baseOption(c).tooltip as object),
        trigger: "axis",
        formatter: (raw: unknown) => {
          const i = top[(raw as Array<{ dataIndex: number }>)[0]?.dataIndex ?? 0];
          if (!i) return "";
          return `<b>${label(i)}</b><br/>${t("tooltip", { cars: plant.fmt.num(i.units, 0), min: plant.fmt.int(i.minutes), kzt: plant.fmt.money(i.kzt) })}`;
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
  }, [c, top, total, plant, t, tc]);
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
        return t("eliminate_failures", { equipment: String(p.equipment ?? "") });
      case "buffer_capacity":
        return t("buffer_capacity", { buffer: String(p.buffer ?? ""), from: String(p.from ?? ""), to: String(p.to ?? "") });
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
            <td className="py-2 text-right">{l.effect_kzt.p50 > 0 ? plant.fmt.money(l.effect_kzt.p50) : "—"}</td>
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
