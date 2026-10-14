"use client";

import { PowerOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { usePlant } from "@/components/plant-context";
import { useTicker } from "@/components/shell/header-widgets";
import { plantNow, useLive } from "@/lib/live-store";

/** How long (plant minutes) a shift may run with every line out of plan before we say so. */
const IDLE_GRACE_MIN = 2;

/**
 * «Симулятор не запущен» (VIS-21): the clock says a shift is on, yet every line has been
 * IDLE_NO_PLAN for more than {@link IDLE_GRACE_MIN} plant minutes — typically right after a demo
 * reset, before «Старт». Without it the plant just looks dead while the header clock runs.
 */
export function SimNotRunningBanner() {
  const t = useTranslations("shell.sim");
  const { assets } = usePlant();
  const clock = useLive((s) => s.clock);
  const flow = assets?.flow ?? [];
  // re-render on line state changes only (one string), not on KPI deltas
  const lineKey = useLive((s) => flow.map((c) => `${s.lines[c]?.state ?? ""}@${s.lines[c]?.since ?? ""}`).join("|"));
  const now = useTicker(5_000);
  const pn = plantNow(clock, now);
  if (!clock || clock.mode !== "sim" || !clock.shift || pn === null || flow.length === 0) return null;
  const inShiftMin = (clock.shift.elapsed_min ?? 0) + (pn - clock.plantMs) / 60_000;
  if (inShiftMin < IDLE_GRACE_MIN) return null;
  const allIdle = lineKey.split("|").every((part) => {
    const [state, since] = part.split("@");
    if (state !== "IDLE_NO_PLAN") return false;
    const from = since ? Date.parse(since) : Number.NaN;
    return Number.isNaN(from) || (pn - from) / 60_000 >= IDLE_GRACE_MIN;
  });
  if (!allIdle) return null;
  return (
    <div
      role="status"
      data-testid="sim-not-running"
      className="flex items-center justify-center gap-2 border-b border-severity-warning/60 bg-severity-warning/15 px-4 py-2 text-sm"
    >
      <PowerOff aria-hidden className="size-4 shrink-0" />
      <span className="font-semibold">{t("notRunning")}</span>
      <span className="text-muted-foreground">{t("notRunningHint")}</span>
    </div>
  );
}
