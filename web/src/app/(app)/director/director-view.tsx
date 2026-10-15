"use client";

import { useMutation } from "@tanstack/react-query";
import { Bell, CircleAlert, FileText, History, Info, Lightbulb, Loader2, SlidersHorizontal, Sparkles, TrendingDown, TrendingUp } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState, type ReactNode } from "react";

import { AlertsFeed } from "@/components/alerts";
import {
  useAreaKpi,
  useBaseForecast,
  useBottleneckMonth,
  useDirectorFreshness,
  useLastReport,
  useLevers,
  useLosses,
  usePlanProgress,
  useSystemEffect,
  useThresholds,
  runScenario,
  type DirectorPlan,
} from "@/components/director/data";
import { ForecastHistogram, HISTOGRAM_HEIGHT } from "@/components/director/histogram";
import { bottleneckLosses } from "@/components/director/losses";
import { AreaChart, LastReport, LeversTable, LossesPareto, ShiftsNeededView, Tile, leverMoney, useLeverLabel } from "@/components/director/panels";
import { PLAN_CHART_HEIGHT, PlanChart, ZoomSwitch, planChartYRange } from "@/components/director/plan-chart";
import { mergeOverrides, overridesEmpty, WhatIfPanel } from "@/components/director/what-if";
import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ApiError } from "@/lib/api/client";
import type { EffectResult, ForecastResult, ForecastRunView, LeversResult, Overrides } from "@/lib/api/types";
import { useEpochSeq, useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

type Tone = "critical" | "warning" | null;

/** Fixed-height chart box: the spinner is an overlay, so loading never shifts the layout (VIS-10). */
function ChartBox({ height, loading, children }: { height: number; loading: boolean; children?: ReactNode }) {
  const t = useTranslations("common");
  return (
    <div className="relative" style={{ minHeight: height }}>
      {children}
      {loading ? (
        <div role="status" className="absolute inset-0 flex items-center justify-center gap-2 bg-card/60 text-sm text-muted-foreground">
          <Loader2 aria-hidden className="size-4 animate-spin" />
          {t("loading")}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The 3-second answer (VIS-17): forecast against the line plan, the plant target against the line
 * ceiling, the best lever with its effect and a button that puts it into the what-if scenario.
 */
function Verdict({
  forecast,
  plan,
  lineKey,
  targetKey,
  levers,
  tone,
  onApply,
}: {
  forecast: ForecastResult;
  plan: DirectorPlan;
  lineKey: string;
  targetKey: string | null;
  levers: LeversResult | undefined;
  tone: Tone;
  onApply: (overrides: Record<string, unknown>) => void;
}) {
  const t = useTranslations("director.verdict");
  const plant = usePlant();
  const leverLabel = useLeverLabel();
  const lineQty = forecast.targets[lineKey] ?? plan.targets[lineKey]?.qty ?? 0;
  const parts: string[] = [
    t("forecast", { p50: plant.fmt.int(forecast.summary.p50), qty: plant.fmt.int(lineQty), p: plant.fmt.pct(forecast.p_reach[lineKey] ?? 0, 0) }),
  ];
  if (targetKey) {
    const qty = forecast.targets[targetKey] ?? plan.targets[targetKey]?.qty ?? 0;
    const ceiling = plan.ceiling;
    parts.push(
      ceiling && ceiling.qty < qty
        ? t("aboveCeiling", { qty: plant.fmt.int(qty), ceiling: plant.fmt.int(ceiling.qty) })
        : t("target", { qty: plant.fmt.int(qty), p: plant.fmt.pct(forecast.p_reach[targetKey] ?? 0, 0) }),
    );
  }
  const best = levers ? [...levers.levers].sort((a, b) => a.rank - b.rank)[0] : undefined;
  let lever: string | null = null;
  if (best && levers) {
    const pAfter = Math.min(1, Math.max(0, (levers.base.p_reach[lineKey] ?? 0) + (best.delta_p_reach[lineKey] ?? 0)));
    const money = leverMoney(best.effect_kzt);
    lever = money
      ? t(money.mean ? "leverMean" : "lever", { lever: leverLabel(best), p: plant.fmt.pct(pAfter, 0), kzt: plant.fmt.money(money.value) })
      : t("leverNoMoney", { lever: leverLabel(best), p: plant.fmt.pct(pAfter, 0) });
  }
  const Icon = tone ? TrendingDown : TrendingUp;
  return (
    <section
      aria-label={t("label")}
      data-testid="director-verdict"
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border bg-card px-4 py-3",
        tone === "critical" && "border-l-4 border-l-severity-critical",
        tone === "warning" && "border-l-4 border-l-severity-warning",
      )}
    >
      <Icon
        aria-hidden
        className={cn("size-6 shrink-0", tone === "critical" ? "text-severity-critical" : tone === "warning" ? "text-severity-warning" : "text-muted-foreground")}
      />
      <p className="min-w-0 flex-1 text-[15px] leading-snug">
        <b className="font-semibold">{parts.join(" ")}</b>
        {lever ? <span className="text-muted-foreground"> {lever}</span> : null}
      </p>
      {best ? (
        <Button variant="outline" className="h-9" data-testid="verdict-apply" onClick={() => onApply(best.overrides as Record<string, unknown>)}>
          <SlidersHorizontal aria-hidden />
          {t("apply")}
        </Button>
      ) : null}
    </section>
  );
}

/** /director — month plan, forecast, losses, levers, what-if (SPEC §13.2, US-1, US-4). */
export function DirectorView() {
  const t = useTranslations("director");
  const plant = usePlant();
  const plan = usePlanProgress();
  const base = useBaseForecast();
  const levers = useLevers();
  const effect = useSystemEffect();
  const areaKpi = useAreaKpi();
  const losses = useLosses();
  const bn = useBottleneckMonth();
  const thr = useThresholds().data;
  useDirectorFreshness();
  const clock = useLive((s) => s.clock);
  const shiftCodes = plant.assets?.calendar.shifts.map((s) => s.code) ?? [];
  const report = useLastReport(clock?.shift?.date ?? plan.data?.as_of.slice(0, 10) ?? null, clock?.shift?.code ?? null, shiftCodes);

  const [panel, setPanel] = useState(false);
  const [zoom, setZoom] = useState(false);
  const [scenario, setScenario] = useState<Overrides>({});
  // a what-if result belongs to the plant timeline it was computed on: a demo reset drops it
  const epochSeq = useEpochSeq();
  const [stored, setStored] = useState<{ run: ForecastRunView; effect: EffectResult | null; epoch: number; overrides: Overrides } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: async ({ epoch, overrides }: { epoch: number; overrides: Overrides }) => ({ ...(await runScenario(overrides)), epoch, overrides }),
    onSuccess: (o) => {
      setStored(o);
      setError(null);
    },
    onError: (err) => setError(err instanceof ApiError ? `${err.title}: ${err.detail}` : String(err)),
  });
  const outcome = stored && stored.epoch === epochSeq ? stored : null;
  const runScenarioNow = () => run.mutate({ epoch: epochSeq, overrides: scenario });
  // «пересчитать» an outdated result: the scenario that produced it, on the current plant state
  const rerunShown = () => run.mutate({ epoch: epochSeq, overrides: outcome?.overrides ?? scenario });

  const r = base.data?.result ?? null;
  const p = plan.data ?? null;
  // which targets exist: from the plan (authoritative for «no plan»), else from the forecast
  const keys = Object.keys(p?.targets ?? r?.targets ?? {});
  const hasPlan = p ? keys.length > 0 : null;
  const watched = thr?.plan_risk_target;
  const lineKey = (watched && keys.includes(watched) ? watched : keys.includes("line_plan") ? "line_plan" : keys[0]) ?? null;
  const targetKey = keys.find((k) => k !== lineKey) ?? null;
  const qtyOf = (k: string | null) => (k ? (p?.targets[k]?.qty ?? r?.targets[k] ?? null) : null);
  const lineQty = qtyOf(lineKey);
  const targetQty = qtyOf(targetKey);
  const warnP = thr?.plan_risk_warn_p ?? null;
  const critP = thr?.plan_risk_crit_p ?? null;
  const toneOfP = (pr: number | null | undefined): Tone =>
    pr == null ? null : critP != null && pr < critP ? "critical" : warnP != null && pr < warnP ? "warning" : null;

  const areaRows = useMemo(
    () => (areaKpi.data?.items ?? []).filter((x) => plant.areas[x.code]?.kind === "production"),
    [areaKpi.data, plant.areas],
  );
  const areaMap = useMemo(() => Object.fromEntries(areaRows.map((x) => [x.code, x])), [areaRows]);
  const bnArea = bn.data?.overall ? (plant.lines[bn.data.overall]?.area ?? null) : null;
  const defectLimit = thr?.defect_rate_limit ?? null;
  const worstDefect = [...areaRows].sort((a, b) => (b.defect_rate ?? 0) - (a.defect_rate ?? 0))[0];
  // the base forecast was re-read on a newer plant state than the scenario's own base: the
  // scenario is kept in the panel, marked outdated, and left off the base charts
  const scenAll = outcome?.run.result ?? null;
  const staleSince = scenAll && r && Date.parse(r.as_of) > Date.parse(scenAll.as_of) ? r.as_of : null;
  const scenRun = staleSince ? null : scenAll;
  const outputLine = p?.output_line ?? plant.assets?.flow[plant.assets.flow.length - 1] ?? null;
  const bnLosses = useMemo(() => (losses.data ? bottleneckLosses(losses.data, bn.data, outputLine) : null), [losses.data, bn.data, outputLine]);
  const monthLabel = (m: string) => plant.fmt.date(`${m}-01`).slice(3);
  const month = p?.month ?? r?.month ?? "";
  const lastDay = p?.daily[p.daily.length - 1]?.date ?? null;

  const applyLever = (overrides: Record<string, unknown>) => {
    setScenario((s) => mergeOverrides(s, overrides));
    setPanel(true);
  };

  // «цель выше оси» only when the zoomed y range really ends below the plant target
  const targetAboveAxis = useMemo(
    () => zoom && p != null && targetQty != null && targetQty > planChartYRange(p, r, scenRun, lineKey, true, plant.fmt.tz).yMax,
    [zoom, p, r, scenRun, lineKey, targetQty, plant.fmt.tz],
  );

  const fulfil = (k: string | null) => (k && p ? (p.targets[k] ?? null) : null);
  const lineFul = fulfil(lineKey);
  const targetFul = fulfil(targetKey);
  const ceiling = p?.ceiling ?? null;

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4" data-testid="director">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">{p ? t("subtitle", { month: monthLabel(p.month), shifts: plant.fmt.num(p.shifts.remaining, 1) }) : "\u00a0"}</p>
        </div>
        <Button onClick={() => setPanel(true)} data-testid="open-what-if" className="h-10">
          <SlidersHorizontal aria-hidden />
          {t("whatIf.open")}
          {!overridesEmpty(scenario) ? <Badge tone="info" className="ml-1">{t("whatIf.active")}</Badge> : null}
        </Button>
      </div>

      {/* verdict strip (VIS-17) or «no plan for the month» (STAB-7) */}
      {hasPlan === false && p ? (
        <div role="note" data-testid="no-plan" className="flex items-start gap-3 rounded-lg border bg-card px-4 py-3 text-sm">
          <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <p>
            <b>{t("noPlan", { month: monthLabel(p.month) })}</b> <span className="text-muted-foreground">{t("noPlanHint")}</span>
          </p>
        </div>
      ) : r && p && lineKey ? (
        <Verdict forecast={r} plan={p} lineKey={lineKey} targetKey={targetKey} levers={levers.data} tone={toneOfP(r.p_reach[lineKey])} onApply={applyLever} />
      ) : (!r && base.error) || (!p && plan.error) ? (
        <div data-testid="director-verdict-error" className="flex min-h-[62px] items-center rounded-lg border bg-card px-4">
          <QueryState error={(!r && base.error) || plan.error} compact className="py-0" />
        </div>
      ) : (
        <div aria-hidden className="h-[62px] animate-pulse rounded-lg border bg-card" />
      )}

      {/* tiles */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Tile
          testid="tile-mtd"
          label={t("tiles.mtd")}
          loading={!p}
          value={p ? plant.fmt.int(p.mtd_output) : null}
          tone={lineFul && lineFul.fulfilment < 0.95 ? "warning" : null}
          sub={
            <>
              {lineFul && lineKey ? (
                <p data-testid="mtd-line">
                  <b className="font-semibold text-foreground">{plant.fmt.pct(lineFul.fulfilment, 1)}</b>{" "}
                  {t("tiles.mtdLine", { qty: plant.fmt.int(lineFul.qty), plan: plant.fmt.int(lineFul.plan_to_date) })}
                </p>
              ) : null}
              {targetFul ? (
                <p data-testid="mtd-target">{t("tiles.mtdTarget", { f: plant.fmt.pct(targetFul.fulfilment, 1), qty: plant.fmt.int(targetFul.qty) })}</p>
              ) : null}
              {hasPlan === false ? <p>{t("noPlan", { month: monthLabel(p?.month ?? month) })}</p> : null}
            </>
          }
        />
        <Tile
          testid="tile-forecast"
          label={t("tiles.forecast")}
          loading={!r}
          value={r ? <span data-testid="forecast-p50">{plant.fmt.int(r.summary.p50)}</span> : null}
          sub={
            r ? (
              <>
                <p>{t("tiles.forecastRange", { p10: plant.fmt.int(r.summary.p10), p90: plant.fmt.int(r.summary.p90) })}</p>
                <p data-testid="forecast-as-of">{t("asOf", { time: plant.fmt.time(r.as_of) })}</p>
              </>
            ) : null
          }
        />
        {hasPlan === false ? (
          <Tile
            testid="tile-no-plan"
            className="col-span-2"
            label={t("tiles.pReachNoPlan")}
            value={<span className="text-muted-foreground">—</span>}
            sub={t("noPlan", { month: monthLabel(p?.month ?? month) })}
          />
        ) : (
          [lineKey, targetKey].map((key, i) => {
            // while the plan loads both probability tiles keep their place (no layout shift)
            if (!key && i === 1 && hasPlan !== null) return null;
            const qty = qtyOf(key);
            const pr = key && r ? (r.p_reach[key] ?? 0) : null;
            const tone = toneOfP(pr);
            const isTarget = i === 1;
            return (
              <Tile
                key={key ?? `p-${i}`}
                testid={qty != null ? `tile-p-${qty}` : undefined}
                label={t("tiles.pReach", { qty: qty != null ? plant.fmt.int(qty) : "…" })}
                loading={pr == null}
                value={plant.fmt.pct(pr, 0)}
                tone={tone}
                icon={
                  tone ? (
                    <TrendingDown aria-hidden className={tone === "critical" ? "size-6 text-severity-critical" : "size-6 text-severity-warning"} />
                  ) : (
                    <TrendingUp aria-hidden className="size-6 text-muted-foreground" />
                  )
                }
                sub={
                  <>
                    <p>{t("tiles.shortfall", { n: plant.fmt.int(key ? (r?.expected_shortfall[key] ?? null) : null) })}</p>
                    {isTarget && ceiling ? (
                      <p data-testid="ceiling">
                        {t("ceiling", { qty: plant.fmt.int(ceiling.qty), rate: plant.fmt.num(ceiling.rate_per_shift, 1), shifts: ceiling.shifts })}
                      </p>
                    ) : null}
                    {isTarget && p && p.unallocated != null && p.unallocated !== 0 ? (
                      <p
                        data-testid="unallocated-note"
                        title={t("unallocatedText", {
                          target: plant.fmt.int(p.targets[targetKey ?? ""]?.qty ?? null),
                          line: plant.fmt.int(p.targets[lineKey ?? ""]?.qty ?? null),
                        })}
                      >
                        {t("tiles.unallocated", { gap: plant.fmt.signed(p.unallocated, 0) })}
                      </p>
                    ) : null}
                  </>
                }
              />
            );
          })
        )}
        <Tile
          testid="tile-losses"
          label={t("tiles.lossesBottleneck")}
          loading={!losses.data || bn.isLoading}
          value={bnLosses ? t("tiles.cars", { n: plant.fmt.int(bnLosses.totals.units) }) : "—"}
          badge={
            <Badge tone="warning" title={t("tiles.assumptionHint")}>
              {t("tiles.assumption")}
            </Badge>
          }
          sub={
            bnLosses ? (
              <>
                <p>{plant.fmt.money(bnLosses.totals.kzt)}</p>
                <p title={t("tiles.lossesHint")}>
                  {bnLosses.lines
                    .slice(0, 2)
                    .map((l) => t("tiles.bottleneckShare", { line: plant.entityName(l.line), share: plant.fmt.pct(l.share, 0) }))
                    .join(" · ")}
                </p>
              </>
            ) : null
          }
        />
        <Tile
          testid="tile-defects"
          label={t("tiles.defects")}
          loading={!areaKpi.data}
          value={worstDefect ? plant.fmt.pct(worstDefect.defect_rate ?? null, 2) : "—"}
          tone={worstDefect && defectLimit != null && (worstDefect.defect_rate ?? 0) > defectLimit ? "critical" : null}
          sub={
            worstDefect
              ? defectLimit != null
                ? t("tiles.defectsSub", { area: plant.entityName(worstDefect.code), limit: plant.fmt.pct(defectLimit, 0) })
                : plant.entityName(worstDefect.code)
              : null
          }
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader
            title={t("chart.title")}
            subtitle={r ? t("chart.subtitle", { n: plant.fmt.int(r.n_runs), ms: r.duration_ms ?? 0 }) : "\u00a0"}
            actions={
              <>
                {scenRun ? <Badge tone="info">{t("chart.withScenario")}</Badge> : null}
                {staleSince ? (
                  <button
                    type="button"
                    onClick={rerunShown}
                    disabled={run.isPending}
                    data-testid="scenario-stale"
                    title={t("whatIf.stale", { time: plant.fmt.time(staleSince), at: plant.fmt.time(scenAll?.as_of ?? null) })}
                    className="inline-flex items-center gap-1 rounded-md border border-severity-warning/60 px-2 py-0.5 text-xs font-medium whitespace-nowrap hover:bg-accent disabled:opacity-50"
                  >
                    {run.isPending ? <Loader2 aria-hidden className="size-3.5 animate-spin" /> : <History aria-hidden className="size-3.5 text-severity-warning" />}
                    {t("chart.scenarioStale")}
                  </button>
                ) : null}
                <ZoomSwitch zoom={zoom} onChange={setZoom} />
              </>
            }
          />
          <CardBody>
            <QueryState error={plan.error ?? base.error} compact />
            <ChartBox height={PLAN_CHART_HEIGHT} loading={plan.isLoading || base.isLoading}>
              {p ? <PlanChart plan={p} forecast={r} scenario={scenRun} lineKey={lineKey} zoom={zoom} /> : null}
            </ChartBox>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("chart.fanNote")}
              {targetAboveAxis ? ` · ${t("chart.zoomNote", { qty: plant.fmt.int(targetQty) })}` : ""}
            </p>
          </CardBody>
        </Card>
        <Card data-testid="forecast-histogram">
          <CardHeader
            title={t("histogram.title", { date: lastDay ? plant.fmt.dayMonth(lastDay) : "…" })}
            subtitle={scenRun ? t("histogram.withScenario") : t("histogram.subtitle")}
          />
          <CardBody>
            {r && lineKey && lineQty != null ? (
              <p className="flex flex-wrap items-baseline gap-x-2 text-[28px] leading-tight font-semibold tracking-tight tabular-nums" data-testid="histogram-p">
                {t("histogram.p", { qty: plant.fmt.int(lineQty), p: plant.fmt.pct(r.p_reach[lineKey] ?? 0, 0) })}
                {scenRun ? (
                  <span className="text-lg text-severity-info">
                    → {plant.fmt.pct(scenRun.p_reach[lineKey] ?? 0, 0)} {t("histogram.scenarioShort")}
                  </span>
                ) : null}
              </p>
            ) : (
              <p className="h-[34px]" />
            )}
            <ChartBox height={HISTOGRAM_HEIGHT} loading={base.isLoading}>
              {r ? <ForecastHistogram base={r} scenario={scenRun} lineQty={lineQty} targetQty={targetQty} /> : null}
            </ChartBox>
            {r && lineQty != null ? (
              <p className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                <span aria-hidden className="inline-block size-2.5 rounded-sm bg-foreground/85" />
                {t("histogram.legendReach", { qty: plant.fmt.int(lineQty) })}
                <span aria-hidden className="ml-2 inline-block size-2.5 rounded-sm bg-muted-foreground/35" />
                {t("histogram.legendBelow", { qty: plant.fmt.int(lineQty) })}
              </p>
            ) : null}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title={t("levers.title")} icon={<Lightbulb aria-hidden className="size-4" />} subtitle={t("levers.subtitle")} />
          <CardBody>
            <QueryState loading={levers.isLoading} error={levers.error}>
              {t("levers.loading")}
            </QueryState>
            {levers.data ? <LeversTable levers={levers.data.levers} targetKey={lineKey ?? ""} onApply={(l) => applyLever(l.overrides as Record<string, unknown>)} /> : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("losses.bottleneckTitle")} subtitle={t("losses.bottleneckSubtitle")} />
          <CardBody>
            <QueryState error={losses.error ?? bn.error} compact />
            <ChartBox height={260} loading={losses.isLoading || bn.isLoading}>
              {bnLosses ? <LossesPareto items={bnLosses.items} total={bnLosses.totals.units} /> : null}
            </ChartBox>
            {losses.data && !bnLosses && !bn.isLoading ? (
              <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <CircleAlert aria-hidden className="size-3.5" />
                {t("losses.noBottleneck")}
              </p>
            ) : null}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
        <Card>
          <CardHeader title={t("areas.title")} subtitle={t("areas.subtitle")} />
          <CardBody>
            <QueryState error={areaKpi.error} compact />
            <ChartBox height={320} loading={areaKpi.isLoading}>
              {areaRows.length ? <AreaChart rows={areaRows} bottleneckArea={bnArea} oeeTarget={thr?.oee_target ?? null} defectLimit={defectLimit} /> : null}
            </ChartBox>
            {bn.data?.overall ? (
              <p className="mt-1 text-xs text-muted-foreground">
                {t("areas.bottleneck", {
                  line: plant.entityName(bn.data.overall),
                  share: plant.fmt.pct(bn.data.shares[bn.data.overall]?.sole ?? null, 0),
                })}
              </p>
            ) : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("effect.title")} icon={<Sparkles aria-hidden className="size-4" />} />
          <CardBody>
            <QueryState loading={effect.isLoading} error={effect.error} compact />
            {effect.data ? (
              <div data-testid="system-effect">
                <p className="text-3xl font-semibold tabular-nums">{t("effect.cars", { n: plant.fmt.signed(effect.data.delta_cars.p50, 0) })}</p>
                <p className="text-xs text-muted-foreground tabular-nums">
                  {t("effect.range", { lo: plant.fmt.int(effect.data.delta_cars.p10), hi: plant.fmt.int(effect.data.delta_cars.p90) })}
                </p>
                <dl className="mt-3 grid grid-cols-2 gap-2 text-sm tabular-nums">
                  <div>
                    <dt className="text-xs text-muted-foreground">{t("effect.month")}</dt>
                    <dd className="font-semibold">{plant.fmt.money(effect.data.month_kzt.p50)}</dd>
                    <dd className="text-xs text-muted-foreground">
                      {plant.fmt.money(effect.data.month_kzt.p10)}–{plant.fmt.money(effect.data.month_kzt.p90)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">{t("effect.year")}</dt>
                    <dd className="font-semibold">{plant.fmt.money(effect.data.year_kzt.p50)}</dd>
                    <dd className="text-xs text-muted-foreground">
                      {plant.fmt.money(effect.data.year_kzt.p10)}–{plant.fmt.money(effect.data.year_kzt.p90)}
                    </dd>
                  </div>
                </dl>
                <p className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <Badge tone="warning">{t("tiles.assumption")}</Badge>
                  {t("effect.note")}
                </p>
              </div>
            ) : null}
          </CardBody>
        </Card>
        {/* the card keeps its place while the plan loads (no reflow of the 3-column row) */}
        {hasPlan === null || (hasPlan && targetKey && targetQty != null) ? (
          <Card>
            <CardHeader title={t("shifts.title", { qty: targetQty != null ? plant.fmt.int(targetQty) : "…" })} />
            <CardBody>
              <QueryState loading={(hasPlan === null && !plan.error) || levers.isLoading} error={levers.error ?? (hasPlan === null ? plan.error : null)} compact />
              {targetKey && targetQty != null ? <ShiftsNeededView data={levers.data?.shifts_needed_for_target[targetKey]} qty={targetQty} /> : null}
            </CardBody>
          </Card>
        ) : null}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title={t("alerts")} icon={<Bell aria-hidden className="size-4" />} />
          <AlertsFeed limit={6} className="max-h-[340px] overflow-y-auto" />
        </Card>
        <Card>
          <CardHeader title={t("report.title")} icon={<FileText aria-hidden className="size-4" />} />
          <CardBody>
            <QueryState loading={report.isLoading} error={report.error} compact />
            <LastReport data={report.data} canGenerate={Boolean(clock?.shift)} />
          </CardBody>
        </Card>
      </div>

      <WhatIfPanel
        open={panel}
        onClose={() => setPanel(false)}
        value={scenario}
        onChange={setScenario}
        onRun={runScenarioNow}
        onRerun={rerunShown}
        running={run.isPending}
        month={month}
        today={(clock?.shift?.date ?? p?.as_of ?? "").slice(0, 10)}
        areaKpi={areaMap}
        result={outcome?.run ?? null}
        effect={outcome?.effect ?? null}
        error={error}
        lineKey={lineKey}
        staleSince={staleSince}
      />
    </div>
  );
}
