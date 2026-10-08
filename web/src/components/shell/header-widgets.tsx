"use client";

import { Gauge, LogOut, Moon, Pause, Radio, Sun, UserRound, WifiOff, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { usePlant } from "@/components/plant-context";
import { useTheme, useThemeChoice } from "@/components/theme-sync";
import { logout } from "@/lib/api/client";
import type { Claims } from "@/lib/auth";
import { plantNow, useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

/** Re-render every `ms` (clock displays). */
export function useTicker(ms: number): number {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const id = setInterval(() => setNow(performance.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

/** Plant time, date and shift, extrapolated between WS clock ticks with the sim speed. */
export function PlantClock({ large = false }: { large?: boolean }) {
  const t = useTranslations("header");
  const { fmt, assets } = usePlant();
  const clock = useLive((s) => s.clock);
  const now = useTicker(clock && clock.speed > 10 ? 250 : 1000);
  const ms = plantNow(clock, now);
  if (ms === null || !assets) return <span className="text-sm text-muted-foreground">—</span>;
  const shift = clock?.shift;
  return (
    <div className="flex items-baseline gap-2 tabular-nums" aria-label={t("plantTime")} data-testid="plant-clock">
      <span className={cn("font-semibold", large ? "text-2xl" : "text-base")}>{fmt.time(ms, true)}</span>
      <span className="text-xs text-muted-foreground">{fmt.dayMonth(ms)}</span>
      {shift ? (
        <span className="hidden text-xs text-muted-foreground lg:inline">
          {t("shift", { code: shift.code })}
        </span>
      ) : (
        <span className="hidden text-xs text-muted-foreground lg:inline">{t("noShift")}</span>
      )}
    </div>
  );
}

export function SpeedBadge() {
  const t = useTranslations("header");
  const clock = useLive((s) => s.clock);
  if (!clock || clock.mode !== "sim") return null;
  return (
    <span
      title={t("simSpeed")}
      className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs font-semibold tabular-nums text-muted-foreground"
    >
      {clock.paused ? <Pause aria-hidden className="size-3.5" /> : <Gauge aria-hidden className="size-3.5" />}
      {clock.paused ? t("paused") : `×${clock.speed}`}
    </span>
  );
}

/** LIVE / переподключение / офлайн (SPEC §13.1). */
export function ConnectionBadge() {
  const t = useTranslations("connection");
  const status = useLive((s) => s.status);
  const view =
    status === "live"
      ? { Icon: Radio, cls: "border-border text-foreground", dot: "bg-isa-normal animate-pulse", label: t("live") }
      : status === "offline"
        ? { Icon: WifiOff, cls: "border-severity-critical/50 text-severity-critical bg-severity-critical/10", dot: "bg-severity-critical", label: t("offline") }
        : { Icon: RefreshCw, cls: "border-severity-warning/60 text-foreground bg-severity-warning/15", dot: "bg-severity-warning", label: t(status === "connecting" ? "connecting" : "reconnecting") };
  return (
    <span
      role="status"
      data-testid="connection-status"
      data-status={status}
      className={cn("inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-semibold tracking-wide", view.cls)}
    >
      <span aria-hidden className={cn("size-2 rounded-full", view.dot)} />
      <view.Icon aria-hidden className="size-3.5" />
      {view.label}
    </span>
  );
}

export function ThemeToggle() {
  const t = useTranslations("header");
  const theme = useTheme();
  const setChoice = useThemeChoice((s) => s.setChoice);
  return (
    <button
      type="button"
      onClick={() => setChoice(theme === "dark" ? "light" : "dark")}
      className="inline-flex size-9 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
      aria-label={t("toggleTheme")}
      title={t("toggleTheme")}
    >
      <Sun aria-hidden className="hidden size-4.5 dark:block" />
      <Moon aria-hidden className="size-4.5 dark:hidden" />
    </button>
  );
}

export function UserMenu({ claims }: { claims: Claims }) {
  const t = useTranslations("header");
  const tr = useTranslations("roles");
  return (
    <div className="flex items-center gap-2">
      <div className="hidden text-right leading-tight 2xl:block">
        <div className="text-sm font-medium">{claims.name || claims.usr}</div>
        <div className="text-xs text-muted-foreground">{tr(claims.role)}</div>
      </div>
      <span title={`${claims.name || claims.usr} · ${tr(claims.role)}`} className="2xl:hidden">
        <UserRound aria-hidden className="size-5 text-muted-foreground" />
      </span>
      <button
        type="button"
        onClick={() => logout(false)}
        className="inline-flex size-9 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        aria-label={t("logout")}
        title={t("logout")}
      >
        <LogOut aria-hidden className="size-4.5" />
      </button>
    </div>
  );
}
