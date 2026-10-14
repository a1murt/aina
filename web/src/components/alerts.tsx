"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Check, CheckCheck, FileSpreadsheet } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { usePlant, type Plant } from "@/components/plant-context";
import { invalidateQueries } from "@/components/providers";
import { QueryState } from "@/components/query-state";
import { SeverityIcon } from "@/components/state-badge";
import { api } from "@/lib/api/client";
import type { AlertsPage, AlertView } from "@/lib/api/types";
import { plantNow, useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

// ------------------------------------------------------------------ one refresh per alert message

/**
 * The engine publishes an `alert` message when it decides and commits the row on its next commit
 * (about a second later), so the lists are re-read once after a short delay. One watcher serves
 * the whole tab, however many lists are mounted (VIS-19): a burst of messages → one refresh.
 */
const ALERT_REFRESH_DELAY_MS = 1_600;
let watchers = 0;
let unsubscribe: (() => void) | null = null;
let pending: ReturnType<typeof setTimeout> | null = null;
const followers = new Set<() => void>();

function watchAlerts(): () => void {
  if (watchers++ === 0) {
    unsubscribe = useLive.subscribe((s, prev) => {
      if (s.alertSeq === prev.alertSeq || pending) return;
      pending = setTimeout(() => {
        pending = null;
        void invalidateQueries({ queryKey: ["alerts"] });
        for (const fn of followers) fn();
      }, ALERT_REFRESH_DELAY_MS);
    });
  }
  return () => {
    if (--watchers > 0) return;
    unsubscribe?.();
    unsubscribe = null;
    if (pending) clearTimeout(pending);
    pending = null;
  };
}

/** Run `fn` together with the alert-list refresh (e.g. predictions next to AL-M1 on /maintenance). */
export function useAlertsRefresh(fn: () => void): void {
  const ref = useRef(fn);
  useEffect(() => {
    ref.current = fn;
  });
  useEffect(() => {
    const call = () => ref.current();
    followers.add(call);
    const stop = watchAlerts();
    return () => {
      followers.delete(call);
      stop();
    };
  }, []);
}

/** Open alerts (REST), re-read once per `alert` WS message burst, plus a slow poll as a safety net. */
export function useOpenAlerts(limit = 50) {
  useEffect(() => watchAlerts(), []);
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

const DATE_IN_TEXT = /(\d{2})\.(\d{2})\.(\d{4})/;
const dayKey = (s: string): number | null => {
  const m = DATE_IN_TEXT.exec(s);
  return m ? Number(`${m[3]}${m[2]}${m[1]}`) : null;
};

/**
 * An alert about an earlier day than the moment it was raised comes from an imported file (the
 * case data): the importer raises findings «now» about the days in the file, the engine raises
 * them when the shift or day closes. The day is the first dd.MM.yyyy of the server text.
 */
export function isImportedAlert(a: AlertView, plant: Plant): boolean {
  const about = dayKey(a.message_ru);
  const raised = dayKey(plant.fmt.date(a.ts));
  return about != null && raised != null && about < raised;
}

/** Plant calendar day now («16.10»), or null before the first clock; changes once a day. */
function usePlantDay(): string | null {
  const { fmt } = usePlant();
  return useLive((s) => {
    const now = plantNow(s.clock);
    return now == null ? null : fmt.dayMonth(now);
  });
}

export function AlertRow({ alert, dense = false, today }: { alert: AlertView; dense?: boolean; today?: string | null }) {
  const t = useTranslations("alerts");
  const locale = useLocale();
  const plant = usePlant();
  const { fmt } = plant;
  const ack = useAckAlert();
  const [expanded, setExpanded] = useState(false);
  const { title, message } = alertTexts(alert, locale);
  const acked = alert.status === "ack" || Boolean(alert.ack_ts);
  const sameDay = today != null && fmt.dayMonth(alert.ts) === today;
  const imported = isImportedAlert(alert, plant);
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
          <time dateTime={alert.ts} title={fmt.dateTime(alert.ts)} className="shrink-0 text-xs text-muted-foreground tabular-nums">
            {sameDay ? fmt.time(alert.ts) : fmt.dateShort(alert.ts)}
          </time>
        </div>
        {dense ? (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? undefined : t("expand")}
            className={cn("w-full text-left text-xs text-muted-foreground", !expanded && "line-clamp-3")}
          >
            {message}
          </button>
        ) : (
          <p className="text-xs text-muted-foreground">{message}</p>
        )}
        <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
          <code>{alert.rule_id}</code>
          {imported ? (
            <span className="inline-flex items-center gap-1" data-testid="alert-imported">
              <FileSpreadsheet aria-hidden className="size-3" />
              {t("imported")}
            </span>
          ) : null}
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
  const today = usePlantDay();
  const order = { critical: 0, warning: 1, info: 2 } as const;
  const items = [...(q.data?.items ?? [])]
    .sort((a, b) => order[a.severity] - order[b.severity] || b.ts.localeCompare(a.ts))
    .slice(0, limit);
  return (
    <div className={className}>
      <QueryState loading={q.isLoading} error={q.error} compact className="px-3" />
      {q.data && items.length === 0 ? <p className="px-3 py-4 text-sm text-muted-foreground">{t("none")}</p> : null}
      <ul data-testid="alerts-feed">{items.map((a) => <AlertRow key={a.id} alert={a} dense today={today} />)}</ul>
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
