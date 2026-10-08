"use client";

import { Calculator, Loader2, RotateCcw, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, type ReactNode } from "react";

import { usePlant } from "@/components/plant-context";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import type { AreaLive, EffectResult, ForecastRunView, KpiRow, Overrides } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const CLASSES = ["A", "B", "C"] as const;
const MULTIPLIERS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];

export function overridesEmpty(o: Overrides): boolean {
  return (
    !Object.keys(o.defect_rate ?? {}).length &&
    !Object.keys(o.mtbf_multiplier ?? {}).length &&
    !Object.keys(o.mttr_multiplier ?? {}).length &&
    !Object.keys(o.ict_seconds ?? {}).length &&
    !Object.keys(o.buffer_capacity ?? {}).length &&
    !(o.extra_shifts ?? []).length &&
    !o.filter_policy &&
    !Object.keys(o.ckd_delay_days ?? {}).length
  );
}

/** Merge lever overrides into the scenario (lists are united, maps overwritten key by key). */
export function mergeOverrides(a: Overrides, b: Record<string, unknown>): Overrides {
  const out: Overrides = structuredClone(a);
  for (const [k, v] of Object.entries(b)) {
    if (k === "extra_shifts" && Array.isArray(v)) {
      const list = [...(out.extra_shifts ?? [])];
      for (const s of v as Array<{ date: string; shifts?: string[] | null }>) {
        const ex = list.find((x) => x.date === s.date);
        if (!ex) list.push({ date: s.date, shifts: s.shifts ?? null });
        else ex.shifts = ex.shifts && s.shifts ? Array.from(new Set([...ex.shifts, ...s.shifts])) : null;
      }
      out.extra_shifts = list;
    } else if (k === "filter_policy") {
      out.filter_policy = v as Overrides["filter_policy"];
    } else if (v && typeof v === "object") {
      const key = k as "defect_rate" | "mtbf_multiplier" | "mttr_multiplier" | "ict_seconds" | "buffer_capacity" | "ckd_delay_days";
      out[key] = { ...(out[key] ?? {}), ...(v as Record<string, number>) };
    }
  }
  return out;
}

function setKey(map: Record<string, number> | undefined, key: string, value: number | null): Record<string, number> {
  const next = { ...(map ?? {}) };
  if (value === null || Number.isNaN(value)) delete next[key];
  else next[key] = value;
  return next;
}

function Group({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <fieldset className="border-b px-5 py-4">
      <legend className="sr-only">{title}</legend>
      <p className="text-sm font-semibold">{title}</p>
      {hint ? <p className="mb-2 text-xs text-muted-foreground">{hint}</p> : <div className="mb-2" />}
      {children}
    </fieldset>
  );
}

const input =
  "h-9 w-24 rounded-md border bg-card px-2 text-right text-sm tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring/60";

/** Non-working days of the rest of the month (candidates for extra shifts). */
function extraShiftDays(month: string, today: string, weekdays: number[], holidays: string[]): string[] {
  const [y, m] = month.split("-").map(Number) as [number, number];
  const out: string[] = [];
  const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
  for (let d = 1; d <= days; d++) {
    const date = new Date(Date.UTC(y, m - 1, d));
    const iso = date.toISOString().slice(0, 10);
    if (iso <= today) continue;
    const isoWeekday = ((date.getUTCDay() + 6) % 7) + 1;
    if (!weekdays.includes(isoWeekday) || holidays.includes(iso)) out.push(iso);
  }
  return out;
}

/**
 * What-if panel (SPEC §13.2, FR-FC-02): defects by area, MTBF/MTTR multipliers by class, filter
 * policy, buffer capacities, extra shifts, ICT by line → «Рассчитать» → base vs scenario.
 */
export function WhatIfPanel({
  open,
  onClose,
  value,
  onChange,
  onRun,
  running,
  month,
  today,
  areaKpi,
  result,
  effect,
  error,
}: {
  open: boolean;
  onClose: () => void;
  value: Overrides;
  onChange: (o: Overrides) => void;
  onRun: () => void;
  running: boolean;
  month: string;
  today: string;
  areaKpi: Record<string, KpiRow | AreaLive>;
  result: ForecastRunView | null;
  effect: EffectResult | null;
  error: string | null;
}) {
  const t = useTranslations("director.whatIf");
  const plant = usePlant();
  const top = useRef<HTMLDivElement>(null);
  // a new result: bring the comparison (top of the panel) into view
  useEffect(() => {
    if (result || error) top.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [result, error]);
  const assets = plant.assets;
  const areas = (assets?.areas ?? []).filter((a) => a.kind === "production");
  const days = assets
    ? extraShiftDays(
        month,
        today,
        assets.calendar.working_weekdays,
        assets.calendar.holidays.map((h) => h.date),
      )
    : [];
  const shiftCodes = assets?.calendar.shifts.map((s) => s.code) ?? [];
  const eqKeys = (map: Record<string, number> | undefined) => Object.keys(map ?? {}).filter((k) => !(CLASSES as readonly string[]).includes(k));

  const toggleShift = (date: string, code: string) => {
    const list = [...(value.extra_shifts ?? [])];
    const i = list.findIndex((s) => s.date === date);
    const cur = i >= 0 ? (list[i]?.shifts ?? shiftCodes) : [];
    const next = cur.includes(code) ? cur.filter((c) => c !== code) : [...cur, code];
    if (i >= 0) list.splice(i, 1);
    if (next.length) list.push({ date, shifts: shiftCodes.filter((c) => next.includes(c)) });
    onChange({ ...value, extra_shifts: list.sort((a, b) => a.date.localeCompare(b.date)) });
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={t("title")}
      labelledBy="what-if-title"
      footer={
        <div className="flex items-center gap-2">
          <Button onClick={onRun} disabled={running} data-testid="what-if-run" className="h-10 flex-1">
            {running ? <Loader2 aria-hidden className="animate-spin" /> : <Calculator aria-hidden />}
            {t("run")}
          </Button>
          <Button variant="outline" className="h-10" onClick={() => onChange({})} disabled={running}>
            <RotateCcw aria-hidden />
            {t("reset")}
          </Button>
        </div>
      }
    >
      <div data-testid="what-if-panel" ref={top} className="scroll-mt-2">
        {result?.result || error ? (
          <div className="border-b bg-muted/40 px-5 py-4">
            {error ? <p className="text-sm text-severity-critical">{error}</p> : null}
            {result?.result ? <Comparison run={result} effect={effect} /> : null}
          </div>
        ) : null}

        <Group title={t("defects")} hint={t("defectsHint")}>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2">
            {areas.map((a) => {
              const cur = areaKpi[a.code]?.defect_rate;
              const v = value.defect_rate?.[a.code];
              return (
                <label key={a.code} className="flex items-center justify-between gap-2 text-sm">
                  <span>{plant.name(a)}</span>
                  <span className="flex items-center gap-1">
                    <input
                      type="number"
                      min={0}
                      max={50}
                      step={0.1}
                      data-testid={`defect-${a.code}`}
                      className={input}
                      value={v != null ? +(v * 100).toFixed(2) : ""}
                      placeholder={cur != null ? (cur * 100).toFixed(1) : ""}
                      onChange={(e) =>
                        onChange({ ...value, defect_rate: setKey(value.defect_rate, a.code, e.target.value === "" ? null : Number(e.target.value) / 100) })
                      }
                    />
                    <span className="text-muted-foreground">%</span>
                  </span>
                </label>
              );
            })}
          </div>
        </Group>

        {(["mtbf_multiplier", "mttr_multiplier"] as const).map((key) => (
          <Group key={key} title={t(key === "mtbf_multiplier" ? "mtbf" : "mttr")} hint={t(key === "mtbf_multiplier" ? "mtbfHint" : "mttrHint")}>
            <div className="grid grid-cols-3 gap-2">
              {CLASSES.map((cls) => (
                <label key={cls} className="flex items-center gap-2 text-sm">
                  <span className="w-14 text-muted-foreground">{t("class", { cls })}</span>
                  <select
                    className="h-9 flex-1 rounded-md border bg-background px-2 text-sm tabular-nums"
                    value={value[key]?.[cls] ?? 1}
                    onChange={(e) => onChange({ ...value, [key]: setKey(value[key], cls, Number(e.target.value) === 1 ? null : Number(e.target.value)) })}
                  >
                    {MULTIPLIERS.map((m) => (
                      <option key={m} value={m}>
                        ×{plant.fmt.num(m, 2)}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
            {eqKeys(value[key]).length ? (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {eqKeys(value[key]).map((k) => (
                  <span key={k} className="inline-flex items-center gap-1 rounded-md border bg-muted px-2 py-0.5 text-xs">
                    {k} ×{plant.fmt.num(value[key]?.[k] ?? 1, 2)}
                    <button type="button" aria-label={t("remove")} onClick={() => onChange({ ...value, [key]: setKey(value[key], k, null) })}>
                      <X aria-hidden className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            ) : null}
          </Group>
        ))}

        <Group title={t("filters")} hint={t("filtersHint")}>
          <div className="grid grid-cols-3 gap-1 rounded-md border p-1" role="radiogroup">
            {([null, "on_limit", "predictive_shift_change"] as const).map((p) => (
              <button
                key={p ?? "none"}
                type="button"
                role="radio"
                aria-checked={(value.filter_policy ?? null) === p}
                onClick={() => onChange({ ...value, filter_policy: p })}
                className={cn(
                  "rounded px-2 py-1.5 text-xs font-medium",
                  (value.filter_policy ?? null) === p ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
                )}
              >
                {t(p === null ? "filterAsIs" : p === "on_limit" ? "filterOnLimit" : "filterShiftChange")}
              </button>
            ))}
          </div>
        </Group>

        <Group title={t("extraShifts")} hint={t("extraShiftsHint")}>
          <div className="grid grid-cols-2 gap-2" data-testid="extra-shifts">
            {days.map((d) => {
              const sel = value.extra_shifts?.find((s) => s.date === d);
              const chosen = sel ? (sel.shifts ?? shiftCodes) : [];
              const holiday = assets?.calendar.holidays.find((h) => h.date === d);
              return (
                <div key={d} className={cn("flex items-center justify-between gap-2 rounded-md border px-2 py-1.5", chosen.length && "border-primary bg-accent")}>
                  <span className="text-sm tabular-nums">
                    {plant.fmt.dayMonth(d)} <span className="text-xs text-muted-foreground">{holiday ? t("holiday") : t(`weekday.${new Date(`${d}T12:00:00Z`).getUTCDay()}` as "weekday.0")}</span>
                  </span>
                  <span className="flex gap-1">
                    {shiftCodes.map((code) => (
                      <button
                        key={code}
                        type="button"
                        data-testid={`shift-${d}-${code}`}
                        aria-pressed={chosen.includes(code)}
                        onClick={() => toggleShift(d, code)}
                        className={cn(
                          "size-7 rounded text-xs font-semibold",
                          chosen.includes(code) ? "bg-primary text-primary-foreground" : "border text-muted-foreground hover:bg-accent",
                        )}
                      >
                        {code}
                      </button>
                    ))}
                  </span>
                </div>
              );
            })}
          </div>
        </Group>

        <Group title={t("buffers")} hint={t("buffersHint")}>
          <div className="grid gap-2">
            {(assets?.buffers ?? []).map((b) => (
              <label key={b.code} className="flex items-center justify-between gap-2 text-sm">
                <span>
                  <b>{b.code}</b> <span className="text-muted-foreground">{plant.name(b)}</span>
                </span>
                <input
                  type="number"
                  min={0}
                  max={500}
                  className={input}
                  value={value.buffer_capacity?.[b.code] ?? ""}
                  placeholder={String(b.capacity)}
                  onChange={(e) => {
                    const n = e.target.value === "" ? null : Math.round(Number(e.target.value));
                    onChange({ ...value, buffer_capacity: setKey(value.buffer_capacity, b.code, n === b.capacity ? null : n) });
                  }}
                />
              </label>
            ))}
          </div>
        </Group>

        <Group title={t("ict")} hint={t("ictHint")}>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2">
            {(assets?.flow ?? []).map((code) => {
              const line = plant.lines[code];
              return (
                <label key={code} className="flex items-center justify-between gap-2 text-sm">
                  <span>{code}</span>
                  <span className="flex items-center gap-1">
                    <input
                      type="number"
                      min={30}
                      max={3600}
                      className={input}
                      value={value.ict_seconds?.[code] ?? ""}
                      placeholder={line ? String(line.ict_seconds) : ""}
                      onChange={(e) => {
                        const n = e.target.value === "" ? null : Number(e.target.value);
                        onChange({ ...value, ict_seconds: setKey(value.ict_seconds, code, n === line?.ict_seconds ? null : n) });
                      }}
                    />
                    <span className="text-xs text-muted-foreground">{t("seconds")}</span>
                  </span>
                </label>
              );
            })}
          </div>
        </Group>
      </div>
    </Sheet>
  );
}

/** Base vs scenario: P50 (P10–P90), P(targets), Δ cars (paired), effect in ₸. */
export function Comparison({ run, effect, compact = false }: { run: ForecastRunView; effect: EffectResult | null; compact?: boolean }) {
  const t = useTranslations("director.whatIf");
  const plant = usePlant();
  const r = run.result;
  if (!r) return null;
  const base = r.base;
  const keys = Object.keys(r.targets);
  const rows: Array<{ label: string; base: string; scen: string; testid?: string }> = [
    {
      label: t("p50"),
      base: base ? plant.fmt.int(base.summary.p50) : "—",
      scen: plant.fmt.int(r.summary.p50),
      testid: "scenario-p50",
    },
    {
      label: t("range"),
      base: base ? `${plant.fmt.int(base.summary.p10)}–${plant.fmt.int(base.summary.p90)}` : "—",
      scen: `${plant.fmt.int(r.summary.p10)}–${plant.fmt.int(r.summary.p90)}`,
    },
    ...keys.map((k) => ({
      label: t("pReach", { qty: plant.fmt.int(r.targets[k] ?? 0) }),
      base: base ? plant.fmt.pct(base.p_reach[k] ?? 0, 0) : "—",
      scen: plant.fmt.pct(r.p_reach[k] ?? 0, 0),
    })),
  ];
  return (
    <div data-testid="scenario-comparison">
      <table className={cn("w-full text-sm tabular-nums", compact && "text-xs")}>
        <thead>
          <tr className="text-xs text-muted-foreground">
            <th className="py-1 text-left font-medium" />
            <th className="py-1 text-right font-medium">{t("base")}</th>
            <th className="py-1 text-right font-medium text-severity-info">{t("scenario")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className="border-t">
              <td className="py-1.5 text-muted-foreground">{row.label}</td>
              <td className="py-1.5 text-right" data-testid={row.testid ? "base-p50" : undefined}>
                {row.base}
              </td>
              <td className="py-1.5 text-right font-semibold" data-testid={row.testid}>
                {row.scen}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {r.delta ? (
        <p className="mt-2 text-sm">
          {t("delta", {
            d: plant.fmt.signed(r.delta.paired.p50, 0),
            lo: plant.fmt.signed(r.delta.paired.p10, 0),
            hi: plant.fmt.signed(r.delta.paired.p90, 0),
          })}
          {effect ? <span className="text-muted-foreground"> · {t("effect", { m: plant.fmt.money(effect.month_kzt.p50) })}</span> : null}
        </p>
      ) : null}
      <p className="mt-1 text-xs text-muted-foreground">{t("runInfo", { n: plant.fmt.int(r.n_runs), ms: r.duration_ms ?? 0 })}</p>
    </div>
  );
}
