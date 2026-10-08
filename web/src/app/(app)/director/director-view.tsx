"use client";

import { useMutation } from "@tanstack/react-query";
import { Bell, FileText, Info, Lightbulb, SlidersHorizontal, Sparkles, TrendingDown, TrendingUp } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { AlertsFeed } from "@/components/alerts";
import {
  useAreaKpi,
  useBaseForecast,
  useBottleneckMonth,
  useLastReport,
  useLevers,
  useLosses,
  usePlanProgress,
  useSystemEffect,
  runScenario,
} from "@/components/director/data";
import { AreaChart, LastReport, LeversTable, LossesPareto, ShiftsNeededView, Tile } from "@/components/director/panels";
import { PlanChart } from "@/components/director/plan-chart";
import { Comparison, mergeOverrides, overridesEmpty, WhatIfPanel } from "@/components/director/what-if";
import { usePlant } from "@/components/plant-context";
import { QueryState, Skeleton } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ApiError } from "@/lib/api/client";
import type { EffectResult, ForecastRunView, Overrides } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";

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
  const clock = useLive((s) => s.clock);
  const shiftCodes = plant.assets?.calendar.shifts.map((s) => s.code) ?? [];
  const report = useLastReport(clock?.shift?.date ?? plan.data?.as_of.slice(0, 10) ?? null, clock?.shift?.code ?? null, shiftCodes);

  const [panel, setPanel] = useState(false);
  const [scenario, setScenario] = useState<Overrides>({});
  const [outcome, setOutcome] = useState<{ run: ForecastRunView; effect: EffectResult | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => runScenario(scenario),
    onSuccess: (o) => {
      setOutcome(o);
      setError(null);
    },
    onError: (err) => setError(err instanceof ApiError ? `${err.title}: ${err.detail}` : String(err)),
  });

  const r = base.data?.result ?? null;
  const p = plan.data;
  const targets = r ? Object.entries(r.targets) : [];
  const [mainKey, mainQty] = targets[0] ?? ["plant_target", 0];
  const lineKey = targets[1]?.[0] ?? "line_plan";
  const areaRows = useMemo(
    () => (areaKpi.data?.items ?? []).filter((x) => plant.areas[x.code]?.kind === "production"),
    [areaKpi.data, plant.areas],
  );
  const areaMap = useMemo(() => Object.fromEntries(areaRows.map((x) => [x.code, x])), [areaRows]);
  const bnArea = bn.data?.overall ? (plant.lines[bn.data.overall]?.area ?? null) : null;
  const defectLimit = 0.02;
  const worstDefect = [...areaRows].sort((a, b) => (b.defect_rate ?? 0) - (a.defect_rate ?? 0))[0];
  const scenRun = outcome?.run.result ?? null;

  const applyLever = (overrides: Record<string, unknown>) => {
    setScenario((s) => mergeOverrides(s, overrides));
    setPanel(true);
  };

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4" data-testid="director">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">
            {p ? t("subtitle", { month: plant.fmt.date(`${p.month}-01`).slice(3), shifts: plant.fmt.num(p.shifts.remaining, 1) }) : " "}
          </p>
        </div>
        <Button onClick={() => setPanel(true)} data-testid="open-what-if" className="h-10">
          <SlidersHorizontal aria-hidden />
          {t("whatIf.open")}
          {!overridesEmpty(scenario) ? <Badge tone="info" className="ml-1">{t("whatIf.active")}</Badge> : null}
        </Button>
      </div>

      {p && p.unallocated !== 0 ? (
        <div role="note" data-testid="unallocated-banner" className="flex items-start gap-3 rounded-lg border border-severity-info/40 bg-severity-info/10 px-4 py-3 text-sm">
          <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-severity-info" />
          <p>
            <b>{t("unallocated", { gap: plant.fmt.signed(p.unallocated, 0) })}</b>{" "}
            <span className="text-muted-foreground">
              {t("unallocatedText", {
                target: plant.fmt.int(p.targets.plant_target?.qty ?? 0),
                line: plant.fmt.int(p.targets.line_plan?.qty ?? 0),
              })}
            </span>
          </p>
        </div>
      ) : null}

      {/* tiles */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {p ? (
          <Tile
            testid="tile-mtd"
            label={t("tiles.mtd")}
            value={plant.fmt.int(p.mtd_output)}
            tone={(p.targets[mainKey]?.fulfilment ?? 1) < 0.95 ? "warning" : null}
            sub={t("tiles.mtdSub", {
              f: plant.fmt.pct(p.targets[mainKey]?.fulfilment ?? null, 1),
              plan: plant.fmt.int(p.targets[mainKey]?.plan_to_date ?? null),
            })}
          />
        ) : (
          <Skeleton className="h-[124px]" />
        )}
        {r ? (
          <Tile
            testid="tile-forecast"
            label={t("tiles.forecast")}
            value={<span data-testid="forecast-p50">{plant.fmt.int(r.summary.p50)}</span>}
            sub={t("tiles.forecastSub", { p10: plant.fmt.int(r.summary.p10), p90: plant.fmt.int(r.summary.p90), target: plant.fmt.int(mainQty) })}
          />
        ) : (
          <Skeleton className="h-[124px]" />
        )}
        {targets.map(([key, qty]) => {
          const pr = r?.p_reach[key] ?? 0;
          return (
            <Tile
              key={key}
              testid={`tile-p-${qty}`}
              label={t("tiles.pReach", { qty: plant.fmt.int(qty) })}
              value={plant.fmt.pct(pr, 0)}
              tone={pr < 0.2 ? "critical" : pr < 0.5 ? "warning" : null}
              icon={pr < 0.5 ? <TrendingDown aria-hidden className={pr < 0.2 ? "size-6 text-severity-critical" : "size-6 text-severity-warning"} /> : <TrendingUp aria-hidden className="size-6 text-muted-foreground" />}
              sub={t("tiles.shortfall", { n: plant.fmt.int(r?.expected_shortfall[key] ?? null) })}
            />
          );
        })}
        {!r ? (
          <>
            <Skeleton className="h-[124px]" />
            <Skeleton className="h-[124px]" />
          </>
        ) : null}
        {losses.data ? (
          <Tile
            testid="tile-losses"
            label={t("tiles.losses")}
            value={t("tiles.cars", { n: plant.fmt.int(losses.data.totals.units) })}
            badge={
              <Badge tone="warning" title={t("tiles.assumptionHint")}>
                {t("tiles.assumption")}
              </Badge>
            }
            sub={t("tiles.lossesSub", { kzt: plant.fmt.money(losses.data.totals.kzt), perCar: plant.fmt.money(losses.data.kzt_per_car) })}
          />
        ) : (
          <Skeleton className="h-[124px]" />
        )}
        {worstDefect ? (
          <Tile
            testid="tile-defects"
            label={t("tiles.defects")}
            value={plant.fmt.pct(worstDefect.defect_rate ?? null, 2)}
            tone={(worstDefect.defect_rate ?? 0) > defectLimit ? "critical" : null}
            sub={t("tiles.defectsSub", { area: plant.entityName(worstDefect.code), limit: plant.fmt.pct(defectLimit, 0) })}
          />
        ) : (
          <Skeleton className="h-[124px]" />
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader
            title={t("chart.title")}
            subtitle={r ? t("chart.subtitle", { n: plant.fmt.int(r.n_runs), ms: r.duration_ms ?? 0 }) : undefined}
            actions={scenRun ? <Badge tone="info">{t("chart.withScenario")}</Badge> : null}
          />
          <CardBody>
            <QueryState loading={plan.isLoading || base.isLoading} error={plan.error ?? base.error} />
            {p ? <PlanChart plan={p} fan={r?.fan ?? null} scenarioFan={scenRun?.fan ?? null} /> : null}
            {outcome ? (
              <div className="mt-3 rounded-lg border bg-muted/30 p-3">
                <Comparison run={outcome.run} effect={outcome.effect} compact />
              </div>
            ) : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("areas.title")} subtitle={t("areas.subtitle")} />
          <CardBody>
            <QueryState loading={areaKpi.isLoading} error={areaKpi.error} />
            {areaRows.length ? <AreaChart rows={areaRows} bottleneckArea={bnArea} defectLimit={defectLimit} /> : null}
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
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title={t("losses.title")} subtitle={t("losses.subtitle")} />
          <CardBody>
            <QueryState loading={losses.isLoading} error={losses.error} />
            {losses.data ? <LossesPareto losses={losses.data} /> : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("levers.title")} icon={<Lightbulb aria-hidden className="size-4" />} subtitle={t("levers.subtitle")} />
          <CardBody>
            <QueryState loading={levers.isLoading} error={levers.error}>
              {t("levers.loading")}
            </QueryState>
            {levers.data ? <LeversTable levers={levers.data.levers} targetKey={lineKey} onApply={(l) => applyLever(l.overrides as Record<string, unknown>)} /> : null}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-4">
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
        <Card>
          <CardHeader title={t("shifts.title", { qty: plant.fmt.int(mainQty) })} />
          <CardBody>
            <QueryState loading={levers.isLoading} error={levers.error} compact />
            <ShiftsNeededView data={levers.data?.shifts_needed_for_target[mainKey]} qty={mainQty} />
          </CardBody>
        </Card>
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
        onRun={() => run.mutate()}
        running={run.isPending}
        month={p?.month ?? r?.month ?? ""}
        today={(clock?.shift?.date ?? p?.as_of ?? "").slice(0, 10)}
        areaKpi={areaMap}
        result={outcome?.run ?? null}
        effect={outcome?.effect ?? null}
        error={error}
      />
    </div>
  );
}
