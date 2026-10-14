"use client";

import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { BarChart3, CheckCircle2, Lightbulb, Route, Search, ShieldCheck, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { api } from "@/lib/api/client";
import { useClosedShifts, type KpiShiftRow } from "@/lib/api/shifts";
import { useRole } from "@/lib/api/use-role";
import type { CorrelationsView, ParetoView, SpcArea, SpcView } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

/** Roles of GET /api/v1/bodies/{id} (routes/bodies.py). */
const BODY_ROLES = ["quality", "master"];

/** SPC / KPI shift key «2026-10-14/B» → «14.10 · смена B». */
function useShiftKey() {
  const t = useTranslations("quality");
  const { fmt } = usePlant();
  return (key: string) => {
    const [date, code] = key.split("/");
    if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return key;
    return code ? t("shiftKey", { date: fmt.dayMonth(date), shift: code }) : fmt.dayMonth(date);
  };
}

interface BodyTrace {
  body_id: string;
  product: string | null;
  status: string;
  first_ts: string;
  last_ts: string;
  events: Array<{ ts: string; line: string; line_name_ru: string | null; result: string; first_exit: boolean; defect_code: string | null; defect_name_ru: string | null }>;
  defects: Array<{ id: number; ts: string; line: string; area: string; equipment: string | null; defect_code: string; defect_name_ru: string | null; qty: number; disposition: string }>;
}

/** /quality — p-charts, Pareto, FPY / RTY, correlations, body trace (SPEC §11.3, §13.2). */
export function QualityView() {
  const t = useTranslations("quality");
  const plant = usePlant();
  const role = useRole() ?? "";
  const spc = useQuery({ queryKey: ["quality", "spc"], queryFn: () => api<SpcView>("/api/v1/quality/spc"), refetchInterval: 60_000 });
  const pareto = useQuery({ queryKey: ["quality", "pareto"], queryFn: () => api<ParetoView>("/api/v1/quality/pareto"), refetchInterval: 60_000 });
  const corr = useQuery({ queryKey: ["quality", "corr"], queryFn: () => api<CorrelationsView>("/api/v1/quality/correlations"), refetchInterval: 120_000 });
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4" data-testid="screen-quality">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </div>
      <FpyStrip />
      <Card>
        <CardHeader title={t("spcTitle")} icon={<ShieldCheck aria-hidden className="size-4" />} subtitle={t("spcHint")} />
        <CardBody>
          <QueryState loading={spc.isLoading} error={spc.error} compact />
          <div className="grid gap-4 lg:grid-cols-2">
            {(spc.data?.areas ?? []).map((a) => (
              <PChart key={a.area} area={a} />
            ))}
          </div>
        </CardBody>
      </Card>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader title={t("paretoTitle")} icon={<BarChart3 aria-hidden className="size-4" />} subtitle={t("paretoHint")} />
          <CardBody>
            <QueryState loading={pareto.isLoading} error={pareto.error} compact />
            {pareto.data ? <Pareto data={pareto.data} /> : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("corrTitle")} icon={<Lightbulb aria-hidden className="size-4" />} subtitle={t("corrHint")} />
          <CardBody className="flex flex-col gap-2">
            <QueryState loading={corr.isLoading} error={corr.error} compact />
            {(corr.data?.insights ?? []).map((c) => (
              <div
                key={c.factor}
                className={cn("rounded-lg border p-3", c.insight ? "border-l-4 border-l-severity-warning bg-severity-warning/8" : "opacity-80")}
                data-testid={`corr-${c.factor}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5 font-medium">
                    {c.insight ? <TriangleAlert aria-hidden className="size-4 text-severity-warning" /> : <CheckCircle2 aria-hidden className="size-4 text-muted-foreground" />}
                    {c.name_ru}
                  </span>
                  <Badge tone={c.insight ? "warning" : "neutral"}>ρ = {plant.fmt.num(c.rho, 2).replace("-", "\u2212")}</Badge>
                </div>
                <p className="mt-1 text-sm text-muted-foreground">{c.text_ru}</p>
              </div>
            ))}
          </CardBody>
        </Card>
      </div>
      {BODY_ROLES.includes(role) ? <BodySearch /> : null}
    </div>
  );
}

function lastRow(rows: KpiShiftRow[] | undefined, code?: string): KpiShiftRow | null {
  const list = (rows ?? []).filter((r) => (code ? r.code === code : true) && r.final !== false && r.fpy != null);
  return list.length ? list[list.length - 1]! : null;
}

function FpyStrip() {
  const t = useTranslations("quality");
  const plant = usePlant();
  const shiftKey = useShiftKey();
  const areas = useLive((s) => s.areas);
  const rty = useLive((s) => s.plant?.rty ?? null);
  const list = (plant.assets?.areas ?? []).filter((a) => a.kind === "production" && a.lines.length);
  const needArea = list.some((a) => areas[a.code]?.fpy == null);
  const areaKpi = useClosedShifts("area", needArea);
  const plantKpi = useClosedShifts("plant", rty == null);
  const tile = (key: string, label: string, live: number | null, last: KpiShiftRow | null, value: (r: KpiShiftRow) => number | null | undefined, strong = false) => {
    const fallback = live == null && last ? last : null;
    return (
      <div key={key} className={cn("rounded-lg border px-3 py-2", strong ? "border-primary/40 bg-primary/5" : "bg-card")} data-testid={`fpy-${key}`}>
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-2xl font-semibold tabular-nums">{plant.fmt.pct(live ?? (fallback ? (value(fallback) ?? null) : null), 1)}</div>
        <div className="text-[11px] text-muted-foreground">
          {fallback ? t("lastShift", { shift: shiftKey(`${fallback.date}/${fallback.shift ?? ""}`) }) : live != null ? t("currentShift") : "\u00a0"}
        </div>
      </div>
    );
  };
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6" data-testid="fpy-strip">
      {list.map((a) => tile(a.code, t("fpy", { area: plant.name(a) }), areas[a.code]?.fpy ?? null, lastRow(areaKpi.data?.items, a.code), (r) => r.fpy))}
      {tile("rty", t("rty"), rty, lastRow(plantKpi.data?.items), (r) => r.rty ?? null, true)}
    </div>
  );
}

function PChart({ area }: { area: SpcArea }) {
  const t = useTranslations("quality");
  const plant = usePlant();
  const shiftKey = useShiftKey();
  const c = useChartColors();
  const bad = new Set(area.violations.flatMap((v) => v.keys));
  const over = area.points.some((p) => p.p > p.ucl);
  const header = (
    <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold">{plant.name(plant.areas[area.area], area.name_ru)}</h3>
      {area.in_control ? (
        <Badge tone="neutral">
          <CheckCircle2 aria-hidden />
          {t("inControl")}
        </Badge>
      ) : (
        <Badge tone={over ? "critical" : "warning"}>
          <TriangleAlert aria-hidden />
          {t("outOfControl", { n: area.violations.length })}
        </Badge>
      )}
    </div>
  );
  if (!c) return <div style={{ height: 240 }}>{header}</div>;
  const x = area.points.map((p) => `${plant.fmt.dayMonth(p.date)} ${p.shift}`);
  const pct = (v: number) => +(v * 100).toFixed(2);
  const pctText = (v: unknown) => (typeof v === "number" ? plant.fmt.pct(v / 100, 2) : "");
  const option: EChartsOption = {
    ...baseOption(c),
    animation: false,
    grid: { left: 40, right: 12, top: 18, bottom: 40 },
    legend: { bottom: 0, textStyle: { color: c.muted, fontSize: 11 }, itemHeight: 8 },
    xAxis: { type: "category", data: x, axisLabel: { color: c.muted, fontSize: 10, hideOverlap: true }, axisLine: { lineStyle: { color: c.border } } },
    yAxis: {
      type: "value",
      axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.axisPct(v / 100) },
      splitLine: { lineStyle: { color: c.border, opacity: 0.5 } },
    },
    tooltip: { ...(baseOption(c).tooltip as object), trigger: "axis", valueFormatter: pctText },
    series: [
      {
        name: t("p"),
        type: "line",
        data: area.points.map((p) => ({
          value: pct(p.p),
          itemStyle: { color: p.p > p.ucl ? c.alarm : bad.has(p.key) ? c.warning : c.fg },
          symbolSize: bad.has(p.key) || p.p > p.ucl ? 9 : 5,
        })),
        lineStyle: { color: c.fg, width: 1.5 },
        itemStyle: { color: c.fg },
        symbol: "circle",
        markLine: {
          silent: true,
          symbol: "none",
          data: [
            ...(area.p_bar != null
              ? [
                  {
                    yAxis: pct(area.p_bar),
                    name: t("center"),
                    lineStyle: { color: c.info, type: "solid" as const, width: 1.25 },
                    // centre and norm are often close: their labels sit at opposite ends
                    label: { position: "insideStartTop" as const },
                  },
                ]
              : []),
            { yAxis: pct(area.norm), name: t("norm"), lineStyle: { color: c.normal, type: "dotted" as const, width: 1.5 } },
          ],
          label: {
            color: c.muted,
            fontSize: 10,
            position: "insideEndTop",
            formatter: (p) => {
              const m = p as { name?: string; value?: number };
              return `${m.name ?? ""} ${typeof m.value === "number" ? plant.fmt.pct(m.value / 100, 1) : ""}`;
            },
          },
        },
      },
      {
        name: t("ucl"),
        type: "line",
        step: "middle",
        data: area.points.map((p) => pct(p.ucl)),
        showSymbol: false,
        lineStyle: { color: c.alarm, width: 1, type: "dashed" },
        itemStyle: { color: c.alarm },
      },
      {
        name: t("lcl"),
        type: "line",
        step: "middle",
        data: area.points.map((p) => pct(p.lcl)),
        showSymbol: false,
        lineStyle: { color: c.alarm, width: 1, type: "dashed", opacity: 0.6 },
        itemStyle: { color: c.alarm, opacity: 0.6 },
      },
    ],
  };
  return (
    <div className="rounded-lg border p-3" data-testid={`pchart-${area.area}`}>
      {header}
      <EChart option={option} height={220} ariaLabel={t("spcTitle")} />
      {area.violations.length ? (
        <ul className="mt-1 space-y-0.5 text-xs">
          {area.violations.map((v) => (
            <li key={`${v.rule}-${v.end_key}`} className="flex items-start gap-1.5">
              <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0 text-severity-warning" />
              <span>
                {t("rule", { rule: String(v.rule) })}: {v.text_ru} ({shiftKey(v.end_key)})
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Pareto({ data }: { data: ParetoView }) {
  const t = useTranslations("quality");
  const { fmt } = usePlant();
  const c = useChartColors();
  if (!c) return <div style={{ height: 300 }} />;
  const items = data.items.slice(0, 12);
  const vital = new Set(data.vital_few);
  const option: EChartsOption = {
    ...baseOption(c),
    animation: false,
    grid: { left: 40, right: 44, top: 16, bottom: 70 },
    tooltip: { ...(baseOption(c).tooltip as object), trigger: "axis" },
    xAxis: {
      type: "category",
      data: items.map((i) => i.defect_code),
      axisLabel: { color: c.muted, rotate: 40, fontSize: 10 },
      axisLine: { lineStyle: { color: c.border } },
    },
    yAxis: [
      { type: "value", axisLabel: { color: c.muted, formatter: (v: number) => fmt.int(v) }, splitLine: { lineStyle: { color: c.border, opacity: 0.5 } } },
      { type: "value", min: 0, max: 100, axisLabel: { color: c.muted, formatter: (v: number) => fmt.axisPct(v / 100) }, splitLine: { show: false } },
    ],
    series: [
      {
        name: t("qty"),
        type: "bar",
        data: items.map((i) => ({ value: i.qty, itemStyle: { color: vital.has(i.defect_code) ? c.warning : c.idle } })),
        itemStyle: { color: c.warning },
        tooltip: { valueFormatter: (v) => (typeof v === "number" ? fmt.int(v) : "") },
        barMaxWidth: 28,
      },
      {
        name: t("cumulative"),
        type: "line",
        yAxisIndex: 1,
        data: items.map((i) => +(i.cumulative * 100).toFixed(1)),
        tooltip: { valueFormatter: (v) => (typeof v === "number" ? fmt.pct(v / 100, 1) : "") },
        lineStyle: { color: c.fg, width: 1.5 },
        itemStyle: { color: c.fg },
        markLine: { silent: true, symbol: "none", data: [{ yAxis: 80 }], lineStyle: { color: c.muted, type: "dashed" }, label: { show: false } },
      },
    ],
  };
  return (
    <div>
      <EChart option={option} height={300} ariaLabel={t("paretoTitle")} />
      <p className="text-xs text-muted-foreground">
        {t("vitalFew", { codes: items.filter((i) => vital.has(i.defect_code)).map((i) => `${i.defect_code} (${i.name_ru ?? ""})`).join(", "), total: fmt.int(data.total) })}
      </p>
    </div>
  );
}

function BodySearch() {
  const t = useTranslations("quality");
  const tb = useTranslations("quality.body");
  const plant = usePlant();
  // enums of routes/bodies.py through the message book; an unknown value is shown as is
  const en = (group: "status" | "result" | "disposition", v: string) => (tb.has(`${group}.${v}` as "status.finished") ? tb(`${group}.${v}` as "status.finished") : v);
  const defectName = (code: string | null, fallback: string | null) => {
    const d = plant.defects.find((x) => x.code === code);
    return d ? plant.name(d) : (fallback ?? "");
  };
  const [input, setInput] = useState("");
  const [id, setId] = useState<string | null>(null);
  const body = useQuery({
    queryKey: ["body", id],
    queryFn: () => api<BodyTrace>(`/api/v1/bodies/${encodeURIComponent(id ?? "")}`),
    enabled: Boolean(id),
    retry: false,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim()) setId(input.trim());
  };
  const b = body.data;
  return (
    <Card>
      <CardHeader title={t("bodyTitle")} icon={<Route aria-hidden className="size-4" />} subtitle={t("bodyHint")} />
      <CardBody className="flex flex-col gap-3">
        <form onSubmit={submit} className="flex max-w-md gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t("bodyPlaceholder")}
            aria-label={t("bodyTitle")}
            className="h-9 flex-1 rounded-md border bg-background px-3 text-sm"
            data-testid="body-input"
          />
          <Button type="submit" size="sm">
            <Search aria-hidden />
            {t("find")}
          </Button>
        </form>
        <QueryState loading={body.isLoading} error={body.error} compact />
        {b ? (
          <div className="grid gap-4 md:grid-cols-2" data-testid="body-trace">
            <div>
              <p className="mb-2 text-sm">
                <b>{b.body_id}</b> · {b.product ?? "—"} · <Badge tone={b.defects.length ? "warning" : "neutral"}>{en("status", b.status)}</Badge>
              </p>
              <ol className="relative space-y-1.5 border-l pl-4 text-sm">
                {b.events.map((e) => (
                  <li key={`${e.ts}-${e.line}`}>
                    <span className="tabular-nums text-muted-foreground">{plant.fmt.dateTime(e.ts)}</span> · <b>{e.line}</b> {plant.name(plant.lines[e.line], e.line_name_ru ?? "")} —{" "}
                    <span className={cn(e.defect_code && "font-medium text-severity-warning")}>{en("result", e.result)}</span>
                    {e.defect_code ? ` (${e.defect_code})` : ""}
                  </li>
                ))}
              </ol>
            </div>
            <div>
              <h3 className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{t("defects")}</h3>
              {b.defects.length === 0 ? <p className="text-sm text-muted-foreground">{t("noDefects")}</p> : null}
              <ul className="space-y-1 text-sm">
                {b.defects.map((d) => (
                  <li key={d.id} className="flex items-center gap-2">
                    <TriangleAlert aria-hidden className="size-4 text-severity-warning" />
                    <b>{d.defect_code}</b> {defectName(d.defect_code, d.defect_name_ru)} · {plant.entityName(d.area)} · {en("disposition", d.disposition)}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
