"use client";

import { useTranslations } from "next-intl";

import { usePlant } from "@/components/plant-context";
import { StateBadge } from "@/components/state-badge";
import { useLive } from "@/lib/live-store";
import { stateMeta } from "@/lib/states";
import { cn } from "@/lib/utils";

function Meter({ value, target }: { value: number; target: number }) {
  const ratio = target > 0 ? Math.min(1.25, value / target) : 0;
  const behind = target > 0 && value < target * 0.95;
  return (
    <div className="relative mt-1.5 h-1.5 rounded-full bg-muted" aria-hidden>
      <div className={cn("absolute inset-y-0 left-0 rounded-full", behind ? "bg-isa-warning" : "bg-isa-normal")} style={{ width: `${Math.min(100, ratio * 80)}%` }} />
      <div className="absolute inset-y-[-3px] w-0.5 bg-foreground/70" style={{ left: "80%" }} />
    </div>
  );
}

/** KPI of the running shift per line (FR-KPI-03): state, OEE, A/E/Q, output against plan-to-now. */
export function LineKpis({ large = false }: { large?: boolean }) {
  const t = useTranslations("live.kpi");
  const plant = usePlant();
  const lines = useLive((s) => s.lines);
  const bn = useLive((s) => s.bottleneck?.current);
  const flow = plant.assets?.flow ?? [];
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="line-kpis">
      {flow.map((code) => {
        const l = lines[code];
        const meta = stateMeta(l?.state);
        const oeeLow = l?.oee != null && l.oee < 0.85;
        return (
          <div
            key={code}
            data-testid={`kpi-${code}`}
            className={cn("rounded-lg border bg-card p-3", meta.abnormal && "border-l-4", meta.abnormal && meta.border)}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className={cn("truncate font-semibold", large && "text-lg")}>{plant.name(plant.lines[code], code)}</div>
                <div className="text-xs text-muted-foreground">
                  {code}
                  {bn === code ? <span className="ml-1.5 font-semibold text-severity-info">· {t("bottleneck")}</span> : null}
                </div>
              </div>
              <StateBadge state={l?.state} size={large ? "md" : "sm"} />
            </div>
            <div className="mt-3 flex items-end justify-between gap-2">
              <div>
                <div className="text-xs text-muted-foreground">{t("oee")}</div>
                <div className={cn("font-semibold tabular-nums", large ? "text-4xl" : "text-3xl", oeeLow && "text-severity-warning")}>
                  {plant.fmt.pct(l?.oee ?? null)}
                </div>
              </div>
              <dl className="grid grid-cols-3 gap-x-3 text-right text-xs tabular-nums">
                <dt className="text-muted-foreground">A</dt>
                <dt className="text-muted-foreground">E</dt>
                <dt className="text-muted-foreground">Q</dt>
                <dd className="font-medium">{plant.fmt.pct(l?.availability ?? null, 0)}</dd>
                <dd className="font-medium">{plant.fmt.pct(l?.effectiveness ?? null, 0)}</dd>
                <dd className="font-medium">{plant.fmt.pct(l?.quality_ratio ?? null, 0)}</dd>
              </dl>
            </div>
            <div className="mt-3 flex items-baseline justify-between text-sm tabular-nums">
              <span className="text-muted-foreground">{t("output")}</span>
              <span>
                <b className="text-base">{plant.fmt.int(l?.gq ?? null)}</b>
                <span className="text-muted-foreground"> / {plant.fmt.int(l?.plan_to_now ?? null)}</span>
              </span>
            </div>
            <Meter value={l?.gq ?? 0} target={l?.plan_to_now ?? 0} />
          </div>
        );
      })}
    </div>
  );
}
