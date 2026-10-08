"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  ClipboardPlus,
  Gauge,
  HeartPulse,
  Lightbulb,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { alertTexts, useOpenAlerts } from "@/components/alerts";
import { ProbabilitySpark, SignalChart } from "@/components/maintenance/charts";
import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { SeverityIcon, StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { api, ApiError, getClaims } from "@/lib/api/client";
import type { AlertView, HealthFull, LimitForecast, Page, PredictionView, TelemetryView, WorkOrderStatus, WorkOrderView } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

const PDM_RULES = ["AL-M1", "AL-M2"];
const WO_ROLES = ["maintenance", "master", "admin"];
const WO_WRITE_ROLES = ["maintenance", "admin"];
const PRED_ROLES = ["maintenance", "director", "admin"];
const COLUMNS: WorkOrderStatus[] = ["open", "in_progress", "done"];

function useWorkOrders(enabled: boolean) {
  return useQuery({
    queryKey: ["work-orders"],
    queryFn: () => api<Page<WorkOrderView>>("/api/v1/work-orders", { query: { limit: 100 } }),
    enabled,
    refetchInterval: 15_000,
  });
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.detail || err.title : String(err);
}

/** /maintenance — PdM, limit forecasts and work orders (SPEC §13.2, US-5, US-6). */
export function MaintenanceView() {
  const t = useTranslations("maintenance");
  const locale = useLocale();
  const plant = usePlant();
  const role = getClaims()?.role ?? "";
  const live = useLive((s) => s.equipment);
  const canPred = PRED_ROLES.includes(role);
  const canWo = WO_ROLES.includes(role);
  const canWrite = WO_WRITE_ROLES.includes(role);
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "warn" | "err"; text: string } | null>(null);

  const predictions = useQuery({
    queryKey: ["predictions", "latest"],
    queryFn: () => api<Page<PredictionView>>("/api/v1/predictions", { query: { latest: true } }),
    enabled: canPred,
    refetchInterval: 15_000,
  });
  const alerts = useOpenAlerts(100);
  const orders = useWorkOrders(canWo);

  const pdmAlerts = useMemo(
    () => (alerts.data?.items ?? []).filter((a) => PDM_RULES.includes(a.rule_id) && a.status !== "resolved"),
    [alerts.data],
  );
  const alertByEq = useMemo(() => {
    const m: Record<string, AlertView[]> = {};
    for (const a of pdmAlerts) (m[a.entity] ??= []).push(a);
    return m;
  }, [pdmAlerts]);
  const predByEq = useMemo(() => Object.fromEntries((predictions.data?.items ?? []).map((p) => [p.equipment, p])), [predictions.data]);
  const activeOrders = (orders.data?.items ?? []).filter((o) => o.status === "open" || o.status === "in_progress");
  const ordersByEq = useMemo(() => {
    const m: Record<string, number> = {};
    for (const o of activeOrders) m[o.equipment] = (m[o.equipment] ?? 0) + 1;
    return m;
  }, [activeOrders]);
  const orderByAlert = useMemo(() => Object.fromEntries(activeOrders.filter((o) => o.alert_id).map((o) => [o.alert_id as number, o])), [activeOrders]);

  const rows = useMemo(() => {
    const list = Object.values(plant.equipment).map((e) => {
      const p = predByEq[e.code];
      const al = alertByEq[e.code] ?? [];
      const sev = al.some((a) => a.severity === "critical") ? "critical" : al.length ? "warning" : null;
      return { e, p, al, sev, hi: p?.health_index ?? live[e.code]?.health_index ?? null };
    });
    const rank = (s: string | null) => (s === "critical" ? 2 : s === "warning" ? 1 : 0);
    list.sort((a, b) => rank(b.sev) - rank(a.sev) || (b.p?.p_failure ?? -1) - (a.p?.p_failure ?? -1) || a.e.code.localeCompare(b.e.code));
    return list;
  }, [plant.equipment, predByEq, alertByEq, live]);

  const current = selected ?? rows[0]?.e.code ?? null;

  const fromAlert = useMutation({
    mutationFn: (alert: AlertView) => api<WorkOrderView>("/api/v1/work-orders", { method: "POST", body: { alert_id: alert.id } }),
    onSuccess: (wo) => {
      setNote({ tone: "ok", text: t("woCreated", { id: wo.id, code: wo.equipment }) });
      void qc.invalidateQueries({ queryKey: ["work-orders"] });
    },
    onError: (err) => setNote({ tone: err instanceof ApiError && err.status === 409 ? "warn" : "err", text: err instanceof ApiError && err.status === 409 ? t("woExists") : errText(err) }),
  });

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4" data-testid="screen-maintenance">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </div>
      {note ? (
        <p
          role="status"
          className={cn(
            "rounded-md border px-3 py-2 text-sm",
            note.tone === "ok" && "border-border bg-muted",
            note.tone === "warn" && "border-severity-warning/50 bg-severity-warning/10",
            note.tone === "err" && "border-severity-critical/50 bg-severity-critical/10 text-severity-critical",
          )}
        >
          {note.text}
        </p>
      ) : null}

      {/* PdM alerts: AL-M2 (limit forecast with the recommended window) and AL-M1 (failure risk). */}
      <Card data-testid="pdm-alerts">
        <CardHeader title={t("alertsTitle")} icon={<TriangleAlert aria-hidden className="size-4" />} subtitle={t("alertsHint")} />
        <CardBody>
          <QueryState loading={alerts.isLoading} error={alerts.error} compact />
          {alerts.data && pdmAlerts.length === 0 ? <p className="text-sm text-muted-foreground">{t("noAlerts")}</p> : null}
          <ul className="grid gap-3 lg:grid-cols-2">
            {pdmAlerts.map((a) => {
              const tx = alertTexts(a, locale);
              const v = (a.value ?? {}) as Partial<LimitForecast> & { p_failure?: number };
              const wo = orderByAlert[a.id];
              return (
                <li
                  key={a.id}
                  data-testid={`pdm-alert-${a.entity}`}
                  className={cn(
                    "flex flex-col gap-2 rounded-lg border-l-4 border bg-card p-3",
                    a.severity === "critical" ? "border-l-severity-critical" : "border-l-severity-warning",
                  )}
                >
                  <div className="flex items-start gap-2">
                    <SeverityIcon severity={a.severity} className="mt-0.5 size-5 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <button type="button" className="font-semibold underline-offset-4 hover:underline" onClick={() => setSelected(a.entity)}>
                          {a.entity}
                        </button>
                        <Badge tone={a.severity === "critical" ? "critical" : "warning"}>{a.rule_id}</Badge>
                        <span className="text-xs text-muted-foreground">{plant.fmt.dateTime(a.ts)}</span>
                      </div>
                      <p className="text-sm font-medium">{tx.title}</p>
                      <p className="text-sm text-muted-foreground">{tx.message}</p>
                    </div>
                  </div>
                  {a.rule_id === "AL-M2" ? (
                    <div className="flex flex-wrap gap-2 text-sm">
                      <span className="inline-flex items-center gap-1.5 rounded-md bg-severity-info/12 px-2 py-1 font-medium text-severity-info">
                        <CalendarClock aria-hidden className="size-4" />
                        {v.window ? t("window", { time: plant.fmt.time(v.window) }) : t("serviceNow")}
                      </span>
                      {v.hours_to_limit != null ? (
                        <span className="inline-flex items-center gap-1.5 rounded-md bg-muted px-2 py-1">
                          <Gauge aria-hidden className="size-4" />
                          {t("toLimit", { h: plant.fmt.num(v.hours_to_limit, 1) })}
                        </span>
                      ) : null}
                      {(v.saving_min ?? 0) > 0 ? (
                        <span className="inline-flex items-center gap-1.5 rounded-md bg-isa-normal/12 px-2 py-1 font-medium">
                          <CheckCircle2 aria-hidden className="size-4" />
                          {t("saving", { min: plant.fmt.int(v.saving_min ?? 0), cars: plant.fmt.num(v.saving_cars ?? 0, 1) })}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                  {canWrite ? (
                    <div className="flex items-center gap-2">
                      {wo ? (
                        <Badge tone="outline">
                          <ClipboardList aria-hidden />
                          {t("woLinked", { id: wo.id, status: t(`status.${wo.status}`) })}
                        </Badge>
                      ) : (
                        <Button size="sm" onClick={() => fromAlert.mutate(a)} disabled={fromAlert.isPending} data-testid={`wo-from-${a.id}`}>
                          <ClipboardPlus aria-hidden />
                          {t("woFromAlert")}
                        </Button>
                      )}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </CardBody>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader title={t("tableTitle")} icon={<HeartPulse aria-hidden className="size-4" />} subtitle={t("tableHint")} />
          <CardBody className="overflow-x-auto px-0">
            {canPred ? <QueryState loading={predictions.isLoading} error={predictions.error} compact className="px-4" /> : null}
            <table className="w-full text-sm" data-testid="equipment-table">
              <thead className="text-left text-xs text-muted-foreground">
                <tr className="border-b">
                  <th className="px-4 py-2 font-medium">{t("col.unit")}</th>
                  <th className="px-2 py-2 font-medium">{t("col.class")}</th>
                  <th className="px-2 py-2 font-medium">{t("col.state")}</th>
                  <th className="px-2 py-2 text-right font-medium">{t("col.health")}</th>
                  <th className="px-2 py-2 text-right font-medium">{t("col.p")}</th>
                  <th className="px-2 py-2 font-medium">{t("col.factor")}</th>
                  <th className="px-4 py-2 text-right font-medium">{t("col.orders")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ e, p, sev, hi }) => (
                  <tr
                    key={e.code}
                    data-testid={`eq-row-${e.code}`}
                    onClick={() => setSelected(e.code)}
                    className={cn(
                      "cursor-pointer border-b last:border-b-0 hover:bg-accent/50",
                      current === e.code && "bg-accent",
                      sev === "critical" && "bg-severity-critical/8",
                      sev === "warning" && "bg-severity-warning/10",
                    )}
                  >
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-2">
                        {sev ? <SeverityIcon severity={sev} className="size-4 shrink-0" /> : null}
                        <div>
                          <div className="font-semibold">{e.code}</div>
                          <div className="text-xs text-muted-foreground">{plant.name(e)}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-2 py-2">
                      <Badge tone="outline">{e.criticality}</Badge>
                    </td>
                    <td className="px-2 py-2">
                      <StateBadge state={live[e.code]?.state} />
                    </td>
                    <td className="px-2 py-2 text-right tabular-nums">
                      <HealthBar value={hi} />
                    </td>
                    <td className={cn("px-2 py-2 text-right font-semibold tabular-nums", sev === "critical" && "text-severity-critical")}>
                      {p ? plant.fmt.pct(p.p_failure, 0) : "—"}
                    </td>
                    <td className="max-w-[260px] px-2 py-2 text-xs text-muted-foreground">
                      <span className="line-clamp-2">{p?.factors[0] ?? "—"}</span>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{ordersByEq[e.code] ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardBody>
        </Card>
        {current ? <EquipmentCard code={current} canPred={canPred} /> : null}
      </div>

      {canWo ? <Kanban orders={orders.data?.items ?? []} loading={orders.isLoading} error={orders.error} canWrite={canWrite} onNote={setNote} /> : null}
    </div>
  );
}

function HealthBar({ value }: { value: number | null }) {
  if (value == null) return <span>—</span>;
  const tone = value < 40 ? "bg-severity-critical" : value < 70 ? "bg-severity-warning" : "bg-isa-normal";
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden className="h-1.5 w-14 overflow-hidden rounded-full bg-muted">
        <span className={cn("block h-full rounded-full", tone)} style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
      </span>
      <span className="w-7">{Math.round(value)}</span>
    </span>
  );
}

function EquipmentCard({ code, canPred }: { code: string; canPred: boolean }) {
  const t = useTranslations("maintenance.card");
  const plant = usePlant();
  const [signal, setSignal] = useState<string | null>(null);
  const health = useQuery({
    queryKey: ["health", code, "full"],
    queryFn: () => api<HealthFull>(`/api/v1/equipment/${code}/health`),
    refetchInterval: 15_000,
  });
  const telemetry = useQuery({
    queryKey: ["telemetry", code, "24h"],
    queryFn: () => api<TelemetryView>(`/api/v1/equipment/${code}/telemetry`),
    refetchInterval: 30_000,
  });
  const history = useQuery({
    queryKey: ["predictions", "history", code],
    queryFn: () => api<Page<PredictionView>>("/api/v1/predictions", { query: { equipment: code, limit: 500 } }),
    enabled: canPred,
    refetchInterval: 30_000,
  });
  const h = health.data;
  const sigs = telemetry.data?.signals ?? [];
  const limited = h?.limits?.[0]?.signal;
  const chosen = sigs.find((s) => s.code === signal) ?? sigs.find((s) => s.code === limited) ?? sigs.find((s) => s.warn_hi != null || s.limit_hi != null) ?? sigs[0];
  const forecast = h?.limits?.find((l) => l.signal === chosen?.code) ?? null;
  const pts = (chosen?.points ?? []).map((p) => [Date.parse(p[0]), p[1] ?? null] as [number, number | null]);
  const hist = (history.data?.items ?? []).map((p) => [Date.parse(p.ts), p.p_failure] as [number, number]).sort((a, b) => a[0] - b[0]);
  const pred = h?.prediction;

  return (
    <Card data-testid="equipment-card">
      <CardHeader
        title={
          <span className="normal-case">
            <b className="text-foreground">{code}</b> · {plant.name(plant.equipment[code])}
          </span>
        }
        icon={<Wrench aria-hidden className="size-4" />}
        actions={<StateBadge state={h?.state ?? null} />}
      />
      <CardBody className="flex flex-col gap-4">
        <QueryState loading={health.isLoading} error={health.error} compact />
        {h ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Kpi label={t("health")} value={h.health_index != null ? plant.fmt.int(h.health_index) : "—"} />
            <Kpi
              label={t("p", { h: pred?.horizon_h ?? 8 })}
              value={pred ? plant.fmt.pct(pred.p_failure, 0) : "—"}
              tone={pred && pred.p_failure >= 0.5 ? "critical" : undefined}
            />
            <Kpi label={t("mtbf")} value={plant.fmt.num(h.reliability_30d?.mtbf_h ?? null, 0)} />
            <Kpi label={t("mttr")} value={plant.fmt.num(h.reliability_30d?.mttr_min ?? null, 0)} />
          </div>
        ) : null}
        {pred && pred.factors.length ? (
          <div>
            <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              <Lightbulb aria-hidden className="size-3.5" />
              {t("factors")}
            </h3>
            <ol className="list-decimal space-y-1 pl-5 text-sm" data-testid="shap-factors">
              {pred.factors.slice(0, 3).map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ol>
          </div>
        ) : h ? (
          <p className="text-xs text-muted-foreground">{t("noPrediction")}</p>
        ) : null}
        {canPred && hist.length > 1 ? (
          <div>
            <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{t("history")}</h3>
            <ProbabilitySpark points={hist} label={t("history")} />
          </div>
        ) : null}
        {forecast ? (
          <p className="rounded-md border border-severity-warning/50 bg-severity-warning/10 px-3 py-2 text-sm" data-testid="limit-forecast">
            {forecast.text_ru ??
              t("forecast", {
                signal: forecast.signal_name_ru,
                h: forecast.hours_to_limit != null ? plant.fmt.num(forecast.hours_to_limit, 1) : "—",
              })}
          </p>
        ) : null}
        <div>
          <div className="mb-1 flex flex-wrap gap-1">
            {sigs.map((s) => (
              <button
                key={s.code}
                type="button"
                onClick={() => setSignal(s.code)}
                aria-pressed={chosen?.code === s.code}
                className={cn(
                  "rounded-md border px-2 py-0.5 text-xs",
                  chosen?.code === s.code ? "border-primary bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
                )}
              >
                {plant.name(s)}
              </button>
            ))}
          </div>
          <QueryState loading={telemetry.isLoading} error={telemetry.error} compact />
          {chosen ? (
            <SignalChart
              points={pts}
              spec={chosen}
              forecast={forecast}
              label={plant.name(chosen)}
              fmtTime={(ms) => plant.fmt.time(ms)}
              labels={{ value: plant.name(chosen), warn: t("warn"), limit: t("limit"), projection: t("projection") }}
            />
          ) : null}
        </div>
      </CardBody>
    </Card>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "critical" }) {
  return (
    <div className={cn("rounded-md border px-3 py-2", tone === "critical" && "border-severity-critical/50 bg-severity-critical/10")}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("text-xl font-semibold tabular-nums", tone === "critical" && "text-severity-critical")}>{value}</div>
    </div>
  );
}

function Kanban({
  orders,
  loading,
  error,
  canWrite,
  onNote,
}: {
  orders: WorkOrderView[];
  loading: boolean;
  error: unknown;
  canWrite: boolean;
  onNote: (n: { tone: "ok" | "warn" | "err"; text: string }) => void;
}) {
  const t = useTranslations("maintenance");
  const plant = usePlant();
  const qc = useQueryClient();
  const move = useMutation({
    mutationFn: ({ id, status }: { id: number; status: WorkOrderStatus }) =>
      api<WorkOrderView>(`/api/v1/work-orders/${id}`, { method: "PATCH", body: { status } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["work-orders"] }),
    onError: (err) => onNote({ tone: "err", text: errText(err) }),
  });
  return (
    <Card data-testid="work-orders">
      <CardHeader title={t("kanbanTitle")} icon={<ClipboardList aria-hidden className="size-4" />} subtitle={t("kanbanHint")} />
      <CardBody>
        <QueryState loading={loading} error={error} compact />
        <div className="grid gap-3 md:grid-cols-3">
          {COLUMNS.map((col) => {
            const items = orders.filter((o) => o.status === col);
            return (
              <section key={col} className="rounded-lg border bg-muted/40 p-2" data-testid={`wo-col-${col}`}>
                <h3 className="mb-2 flex items-center justify-between px-1 text-sm font-semibold">
                  {t(`status.${col}`)}
                  <Badge tone="outline">{items.length}</Badge>
                </h3>
                <ul className="flex flex-col gap-2">
                  {items.map((o) => (
                    <li key={o.id} className="rounded-md border bg-card p-2.5 text-sm shadow-xs" data-testid={`wo-${o.id}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold">
                          #{o.id} · {o.equipment}
                        </span>
                        <Badge tone={o.priority === "urgent" ? "critical" : o.priority === "high" ? "warning" : "neutral"}>{t(`priority.${o.priority}`)}</Badge>
                      </div>
                      <p className="mt-1 line-clamp-2">{o.title}</p>
                      <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                        {o.alert_rule ? <span>{o.alert_rule}</span> : null}
                        <span>{t(`kind.${o.kind as "predictive" | "corrective" | "preventive"}`)}</span>
                        {o.due_ts ? (
                          <span className={cn(o.overdue && "font-semibold text-severity-critical")}>
                            {t("due", { time: plant.fmt.dateTime(o.due_ts) })}
                          </span>
                        ) : null}
                      </div>
                      {canWrite && col !== "done" ? (
                        <div className="mt-2 flex gap-1.5">
                          {col === "in_progress" ? (
                            <Button size="sm" variant="ghost" onClick={() => move.mutate({ id: o.id, status: "open" })} disabled={move.isPending}>
                              <ArrowLeft aria-hidden />
                              {t("status.open")}
                            </Button>
                          ) : null}
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => move.mutate({ id: o.id, status: col === "open" ? "in_progress" : "done" })}
                            disabled={move.isPending}
                            data-testid={`wo-move-${o.id}`}
                          >
                            {col === "open" ? t("status.in_progress") : t("status.done")}
                            <ArrowRight aria-hidden />
                          </Button>
                        </div>
                      ) : null}
                    </li>
                  ))}
                  {items.length === 0 ? <li className="px-1 text-xs text-muted-foreground">{t("empty")}</li> : null}
                </ul>
              </section>
            );
          })}
        </div>
      </CardBody>
    </Card>
  );
}
