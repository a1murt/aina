"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardPlus, HeartPulse, ListChecks, Tag } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Sparkline } from "@/components/charts/sparkline";
import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { ReasonPicker } from "@/components/reason-picker";
import { useTicker } from "@/components/shell/header-widgets";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { sendAction } from "@/lib/actions";
import { api, ApiError, getClaims } from "@/lib/api/client";
import type { DowntimeView, HealthView, Page, TelemetryView } from "@/lib/api/types";
import { plantNow, useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

export const CLASSIFY_ROLES = ["master", "operator"];
const WORK_ORDER_ROLES = ["maintenance", "master", "admin"];

function Section({ title, icon, children }: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="border-b px-5 py-4 last:border-b-0">
      <h3 className="mb-3 flex items-center gap-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

/** Equipment card (SPEC §13.2 /live): state, reason, telemetry with limits, health, events, actions. */
export function EquipmentDrawer({ code, onClose }: { code: string | null; onClose: () => void }) {
  const t = useTranslations("live.drawer");
  const plant = usePlant();
  const qc = useQueryClient();
  const live = useLive((s) => (code ? s.equipment[code] : undefined));
  const openStop = useLive((s) => (code ? s.downtimeOpen[code] : undefined));
  const clock = useLive((s) => s.clock);
  const now = useTicker(1000);
  const role = getClaims()?.role ?? "";
  const [picking, setPicking] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "warn" | "err"; text: string } | null>(null);
  const eq = code ? plant.equipment[code] : undefined;

  const health = useQuery({
    queryKey: ["health", code],
    queryFn: () => api<HealthView>(`/api/v1/equipment/${code}/health`),
    enabled: Boolean(code),
    refetchInterval: 15_000,
  });
  const telemetry = useQuery({
    queryKey: ["telemetry", code],
    queryFn: () => api<TelemetryView>(`/api/v1/equipment/${code}/telemetry`),
    enabled: Boolean(code),
    refetchInterval: 60_000,
  });
  const events = useQuery({
    queryKey: ["downtime", "entity", code],
    queryFn: () => api<Page<DowntimeView>>("/api/v1/downtime", { query: { entity: code, limit: 6 } }),
    enabled: Boolean(code),
    refetchInterval: 10_000,
  });

  const classify = useMutation({
    mutationFn: (reason: string) => {
      const target = openStop ?? live?.downtime ?? null;
      const last = events.data?.items.find((d) => !d.import_id);
      return sendAction({
        kind: "classify",
        entity: code ?? "",
        start_ts: target?.start_ts ?? last?.start_ts ?? null,
        downtime_id: target ? undefined : last?.id,
        reason_code: reason,
      });
    },
    onSuccess: () => {
      setPicking(false);
      setNote({ tone: "ok", text: t("classified") });
      void qc.invalidateQueries({ queryKey: ["downtime"] });
    },
    onError: (err) => setNote({ tone: "err", text: err instanceof ApiError ? err.detail || err.title : String(err) }),
  });

  const workOrder = useMutation({
    mutationFn: () =>
      api("/api/v1/work-orders", {
        method: "POST",
        body: {
          equipment: code,
          title: t("woTitle", { code: code ?? "" }),
          reason_code: live?.reason_code ?? null,
          priority: live?.state === "DOWN_UNPLANNED" ? "high" : "normal",
        },
      }),
    onSuccess: () => setNote({ tone: "ok", text: t("woCreated") }),
    onError: (err) =>
      setNote(
        err instanceof ApiError && (err.status === 404 || err.status === 405)
          ? { tone: "warn", text: t("woUnavailable") }
          : { tone: "err", text: err instanceof ApiError ? err.detail || err.title : String(err) },
      ),
  });

  const pn = plantNow(clock, now);
  const sinceMin = live?.since && pn ? (pn - Date.parse(live.since)) / 60_000 : null;
  const reason = live?.reason_code ? plant.reasons[live.reason_code] : undefined;
  const series = new Map((telemetry.data?.signals ?? []).map((s) => [s.code, s]));
  const canClassify = CLASSIFY_ROLES.includes(role);
  const canWo = WORK_ORDER_ROLES.includes(role);
  const h = health.data;

  return (
    <Sheet
      open={Boolean(code)}
      onClose={() => {
        setPicking(false);
        setNote(null);
        onClose();
      }}
      title={
        <span className="flex items-baseline gap-2">
          {code}
          <span className="text-sm font-normal text-muted-foreground">{plant.name(eq)}</span>
        </span>
      }
      footer={
        <div className="flex flex-wrap gap-2">
          {canClassify ? (
            <Button onClick={() => setPicking(true)} data-testid="drawer-classify">
              <Tag aria-hidden />
              {t("classify")}
            </Button>
          ) : null}
          {canWo ? (
            <Button variant="outline" onClick={() => workOrder.mutate()} disabled={workOrder.isPending} data-testid="drawer-work-order">
              <ClipboardPlus aria-hidden />
              {t("workOrder")}
            </Button>
          ) : null}
        </div>
      }
    >
      <div data-testid="equipment-drawer">
        {note ? (
          <p
            role="status"
            className={cn(
              "mx-5 mt-4 rounded-md border px-3 py-2 text-sm",
              note.tone === "ok" && "border-border bg-muted",
              note.tone === "warn" && "border-severity-warning/50 bg-severity-warning/10",
              note.tone === "err" && "border-severity-critical/50 bg-severity-critical/10 text-severity-critical",
            )}
          >
            {note.text}
          </p>
        ) : null}
        <Section title={t("state")}>
          <div className="flex flex-wrap items-center gap-3">
            <StateBadge state={live?.state} size="lg" />
            <span className="text-2xl font-semibold tabular-nums">{plant.fmt.clock(sinceMin)}</span>
            {live?.alarm ? <Badge tone="critical">{t("alarm")}</Badge> : null}
          </div>
          <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">{t("since")}</dt>
            <dd className="tabular-nums">{plant.fmt.dateTime(live?.since)}</dd>
            <dt className="text-muted-foreground">{t("reason")}</dt>
            <dd>{reason ? `${plant.name(reason)} (${reason.code})` : live?.reason_code ?? "—"}</dd>
            <dt className="text-muted-foreground">{t("class")}</dt>
            <dd>
              {eq?.criticality ?? "—"} · {plant.name(plant.assets?.equipment_types[eq?.type ?? ""], eq?.type)}
            </dd>
          </dl>
        </Section>

        {picking ? (
          <Section title={t("classify")}>
            <ReasonPicker onConfirm={(r) => classify.mutate(r)} onCancel={() => setPicking(false)} busy={classify.isPending} />
          </Section>
        ) : null}

        <Section title={t("health")} icon={<HeartPulse aria-hidden className="size-4" />}>
          <QueryState loading={health.isLoading} error={health.error} compact />
          {h ? (
            <div className="grid grid-cols-3 gap-3 text-sm">
              <div>
                <div className="text-xs text-muted-foreground">{t("healthIndex")}</div>
                <div className="text-xl font-semibold tabular-nums">{h.health_index != null ? plant.fmt.int(h.health_index) : "—"}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">{t("mtbf")}</div>
                <div className="text-xl font-semibold tabular-nums">{plant.fmt.num(h.reliability_30d?.mtbf_h ?? null, 0)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">{t("mttr")}</div>
                <div className="text-xl font-semibold tabular-nums">{plant.fmt.num(h.reliability_30d?.mttr_min ?? null, 0)}</div>
              </div>
              {h.prediction?.p_failure != null ? (
                <p className="col-span-3 text-sm">
                  {t("prediction", { p: plant.fmt.pct(h.prediction.p_failure, 0), h: h.prediction.horizon_h ?? 8 })}
                </p>
              ) : (
                <p className="col-span-3 text-xs text-muted-foreground">{t("noPrediction")}</p>
              )}
            </div>
          ) : null}
        </Section>

        <Section title={t("telemetry")}>
          {h && h.signals.length === 0 ? <p className="text-sm text-muted-foreground">{t("noSignals")}</p> : null}
          <ul className="space-y-3">
            {(h?.signals ?? []).map((s) => {
              const ser = series.get(s.code);
              const pts = (ser?.points ?? []).map((p) => [Date.parse(p[0]), p[1] ?? null] as [number, number | null]);
              const status = s.status === "normal" ? null : s.status;
              return (
                <li key={s.code} data-testid={`signal-${s.code}`}>
                  <div className="flex items-baseline justify-between gap-2 text-sm">
                    <span>{plant.name(s)}</span>
                    <span className="flex items-baseline gap-2 tabular-nums">
                      {status ? <Badge tone={status === "alarm" || status === "limit" ? "critical" : "warning"}>{status}</Badge> : null}
                      <span className="font-semibold">{plant.fmt.num(s.value, 2)}</span>
                      <span className="text-xs text-muted-foreground">{s.unit}</span>
                    </span>
                  </div>
                  <Sparkline points={pts} spec={s} label={plant.name(s)} />
                  <div className="flex justify-between text-[11px] text-muted-foreground tabular-nums">
                    <span>{t("warn", { v: [s.warn_lo, s.warn_hi].filter((v) => v != null).join(" / ") || "—" })}</span>
                    <span>{t("limit", { v: [s.limit_lo, s.limit_hi].filter((v) => v != null).join(" / ") || "—" })}</span>
                  </div>
                </li>
              );
            })}
          </ul>
        </Section>

        <Section title={t("events")} icon={<ListChecks aria-hidden className="size-4" />}>
          <QueryState loading={events.isLoading} error={events.error} compact />
          <ul className="divide-y text-sm">
            {(events.data?.items ?? []).map((d) => (
              <li key={d.id} className="flex items-center justify-between gap-2 py-1.5">
                <span className="tabular-nums text-muted-foreground">{plant.fmt.dateTime(d.start_ts)}</span>
                <span className="min-w-0 flex-1 truncate">
                  {d.reason_code ? plant.name(plant.reasons[d.reason_code], d.reason_code) : t("unclassified")}
                </span>
                <span className="tabular-nums">{d.open ? t("open") : plant.fmt.clock((d.duration_s ?? 0) / 60)}</span>
              </li>
            ))}
          </ul>
          {events.data && events.data.items.length === 0 ? <p className="text-sm text-muted-foreground">{t("noEvents")}</p> : null}
        </Section>
      </div>
    </Sheet>
  );
}
