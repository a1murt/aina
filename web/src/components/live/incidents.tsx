"use client";

import { useMutation } from "@tanstack/react-query";
import { CircleHelp, Tag } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { AlertsFeed } from "@/components/alerts";
import { CLASSIFY_ROLES } from "@/components/live/equipment-drawer";
import { usePlant } from "@/components/plant-context";
import { ReasonPicker } from "@/components/reason-picker";
import { useTicker } from "@/components/shell/header-widgets";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Sheet } from "@/components/ui/sheet";
import { sendAction } from "@/lib/actions";
import { ApiError, getClaims } from "@/lib/api/client";
import type { DowntimeLive } from "@/lib/api/types";
import { plantNow, useLive } from "@/lib/live-store";

/** Open stops (with «needs classification») and open alerts with actions (SPEC §13.2 /live). */
export function Incidents({ onSelect }: { onSelect: (code: string) => void }) {
  const t = useTranslations("live.incidents");
  const plant = usePlant();
  const open = useLive((s) => s.downtimeOpen);
  const clock = useLive((s) => s.clock);
  const now = useTicker(1000);
  const role = getClaims()?.role ?? "";
  const [target, setTarget] = useState<DowntimeLive | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pn = plantNow(clock, now);
  const stops = Object.values(open)
    .filter((d) => !d.end_ts)
    .sort((a, b) => Number(b.state === "DOWN_UNPLANNED") - Number(a.state === "DOWN_UNPLANNED") || a.start_ts.localeCompare(b.start_ts));
  const classify = useMutation({
    mutationFn: ({ d, reason }: { d: DowntimeLive; reason: string }) =>
      sendAction({ kind: "classify", entity: d.entity, start_ts: d.start_ts, reason_code: reason }),
    onSuccess: () => {
      setTarget(null);
      setError(null);
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail || err.title : String(err)),
  });

  return (
    <div className="flex flex-col">
      <h3 className="px-3 pt-1 pb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{t("stops", { count: stops.length })}</h3>
      {stops.length === 0 ? <p className="px-3 pb-3 text-sm text-muted-foreground">{t("noStops")}</p> : null}
      <ul data-testid="open-stops" className="border-y">
        {stops.map((d) => {
          const min = pn ? (pn - Date.parse(d.start_ts)) / 60_000 : d.duration_min;
          const reason = d.reason_code ? plant.reasons[d.reason_code] : undefined;
          return (
            <li key={d.entity} data-testid="open-stop" data-entity={d.entity} className="flex items-center gap-3 border-b px-3 py-2 last:border-b-0">
              <button type="button" onClick={() => onSelect(d.entity)} className="min-w-0 flex-1 text-left">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold whitespace-nowrap">{d.entity}</span>
                  <span className="text-sm font-semibold tabular-nums">{plant.fmt.clock(min)}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <StateBadge state={d.state} />
                  {d.needs_classification || !reason || d.reason_code === "UNK" ? (
                    <Badge tone="warning">
                      <CircleHelp aria-hidden />
                      {t("needsClassification")}
                    </Badge>
                  ) : (
                    <span data-testid="stop-reason" className="font-medium text-foreground">
                      {plant.name(reason)}
                    </span>
                  )}
                  <span>· {plant.entityName(d.line)}</span>
                </div>
              </button>
              {CLASSIFY_ROLES.includes(role) ? (
                <button
                  type="button"
                  onClick={() => setTarget(d)}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium hover:bg-accent"
                >
                  <Tag aria-hidden className="size-3.5" />
                  {t("classify")}
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
      <h3 className="px-3 pt-3 pb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{t("alerts")}</h3>
      <AlertsFeed limit={10} />
      <Sheet open={target !== null} onClose={() => setTarget(null)} side="center" title={t("classifyTitle", { entity: target?.entity ?? "" })}>
        <div className="p-5">
          {error ? (
            <p role="alert" className="mb-3 text-sm text-severity-critical">
              {error}
            </p>
          ) : null}
          {target ? (
            <ReasonPicker busy={classify.isPending} onCancel={() => setTarget(null)} onConfirm={(reason) => classify.mutate({ d: target, reason })} />
          ) : null}
        </div>
      </Sheet>
    </div>
  );
}
