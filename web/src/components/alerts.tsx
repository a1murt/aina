"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Check, CheckCheck } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { SeverityIcon } from "@/components/state-badge";
import { api } from "@/lib/api/client";
import type { AlertsPage, AlertView } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

/**
 * Open alerts (REST), refreshed after every `alert` WS message. The engine publishes the message
 * when it decides, and its writer commits the row a moment later — so the list is re-read with
 * a short delay (and once more), plus a slow poll as a safety net.
 */
export function useOpenAlerts(limit = 50) {
  const qc = useQueryClient();
  const seq = useLive((s) => s.alertSeq);
  useEffect(() => {
    if (seq === 0) return;
    const refresh = () => void qc.invalidateQueries({ queryKey: ["alerts"] });
    const a = setTimeout(refresh, 800);
    const b = setTimeout(refresh, 3_000);
    return () => {
      clearTimeout(a);
      clearTimeout(b);
    };
  }, [seq, qc]);
  return useQuery({
    queryKey: ["alerts", "open", limit],
    queryFn: () => api<AlertsPage>("/api/v1/alerts", { query: { status: "open", limit } }),
    staleTime: 2_000,
    refetchInterval: 30_000,
  });
}

export function useAckAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<AlertView>(`/api/v1/alerts/${id}/ack`, { method: "POST", body: {} }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
}

export function alertTexts(a: AlertView, locale: string): { title: string; message: string } {
  const kk = locale === "kk";
  return {
    title: (kk && a.title_kk) || a.title_ru,
    message: (kk && a.message_kk) || a.message_ru,
  };
}

export function AlertRow({ alert, dense = false }: { alert: AlertView; dense?: boolean }) {
  const t = useTranslations("alerts");
  const locale = useLocale();
  const { fmt } = usePlant();
  const ack = useAckAlert();
  const { title, message } = alertTexts(alert, locale);
  const acked = alert.status === "ack" || Boolean(alert.ack_ts);
  return (
    <li
      data-testid="alert-row"
      data-id={alert.id}
      data-rule={alert.rule_id}
      data-entity={alert.entity}
      className={cn("flex gap-2.5 border-b px-3 last:border-b-0", dense ? "py-2" : "py-2.5")}
    >
      <SeverityIcon severity={alert.severity} className="mt-0.5" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <p className="truncate text-sm font-medium">{title}</p>
          <time className="shrink-0 text-xs text-muted-foreground tabular-nums">{fmt.time(alert.ts)}</time>
        </div>
        <p className={cn("text-xs text-muted-foreground", dense && "line-clamp-2")}>{message}</p>
        <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
          <code>{alert.rule_id}</code>
          {alert.escalation_level > 0 ? <span>{t("escalation", { level: alert.escalation_level })}</span> : null}
          {acked ? (
            <span className="inline-flex items-center gap-1">
              <CheckCheck aria-hidden className="size-3.5" />
              {t("acked", { who: alert.ack_by ?? "" })}
            </span>
          ) : alert.can_act ? (
            <button
              type="button"
              data-testid="ack-alert"
              onClick={() => ack.mutate(alert.id)}
              disabled={ack.isPending}
              className="ml-auto inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium text-foreground hover:bg-accent disabled:opacity-50"
            >
              <Check aria-hidden className="size-3.5" />
              {t("ack")}
            </button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

/** Alerts feed (director, live): newest first, critical first within the list. */
export function AlertsFeed({ limit = 8, className }: { limit?: number; className?: string }) {
  const t = useTranslations("alerts");
  const q = useOpenAlerts();
  const order = { critical: 0, warning: 1, info: 2 } as const;
  const items = [...(q.data?.items ?? [])]
    .sort((a, b) => order[a.severity] - order[b.severity] || b.ts.localeCompare(a.ts))
    .slice(0, limit);
  return (
    <div className={className}>
      <QueryState loading={q.isLoading} error={q.error} compact className="px-3" />
      {q.data && items.length === 0 ? <p className="px-3 py-4 text-sm text-muted-foreground">{t("none")}</p> : null}
      <ul data-testid="alerts-feed">{items.map((a) => <AlertRow key={a.id} alert={a} dense />)}</ul>
    </div>
  );
}

/** Header bell: counts by severity (colour + number) and a drop-down list with «Принять». */
export function AlertsBell() {
  const t = useTranslations("alerts");
  const q = useOpenAlerts();
  const live = useLive((s) => s.alertsOpen);
  const counts = q.data?.open_counts ?? live;
  const total = counts.critical + counts.warning + counts.info;
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="relative" ref={box}>
      <button
        type="button"
        data-testid="alerts-bell"
        aria-expanded={open}
        aria-label={t("bell", { count: total })}
        onClick={() => setOpen((v) => !v)}
        className="relative inline-flex h-9 items-center gap-1.5 rounded-md px-2 text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <Bell aria-hidden className="size-4.5" />
        {counts.critical > 0 ? (
          <span className="rounded bg-severity-critical px-1.5 text-xs font-bold text-white tabular-nums">{counts.critical}</span>
        ) : null}
        {counts.warning > 0 ? (
          <span className="rounded bg-severity-warning px-1.5 text-xs font-bold text-black tabular-nums">{counts.warning}</span>
        ) : null}
        {counts.critical + counts.warning === 0 && counts.info > 0 ? (
          <span className="rounded bg-muted px-1.5 text-xs font-semibold tabular-nums">{counts.info}</span>
        ) : null}
      </button>
      {open ? (
        <div className="absolute right-0 z-40 mt-2 w-[min(92vw,420px)] overflow-hidden rounded-lg border bg-popover text-popover-foreground shadow-xl">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-sm font-semibold">{t("title")}</span>
            <span className="text-xs text-muted-foreground tabular-nums">{t("openCount", { count: total })}</span>
          </div>
          <AlertsFeed limit={12} className="max-h-[60dvh] overflow-y-auto" />
        </div>
      ) : null}
    </div>
  );
}
