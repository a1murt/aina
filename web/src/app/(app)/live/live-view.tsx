"use client";

import { Activity, ListTodo, Tv, X } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { EquipmentDrawer } from "@/components/live/equipment-drawer";
import { Incidents } from "@/components/live/incidents";
import { LineKpis } from "@/components/live/line-kpis";
import { PlantSchema } from "@/components/live/plant-schema";
import { ShiftTimeline } from "@/components/live/shift-timeline";
import { usePlant } from "@/components/plant-context";
import { ConnectionBadge, PlantClock, SpeedBadge } from "@/components/shell/header-widgets";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { useLive } from "@/lib/live-store";
import { STATE_CODES, stateMeta } from "@/lib/states";
import { cn } from "@/lib/utils";

function StateLegend() {
  const t = useTranslations("states");
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label={t("legend")}>
      {STATE_CODES.map((code) => {
        const m = stateMeta(code);
        return (
          <li key={code} className="flex items-center gap-1.5">
            <span aria-hidden className={cn("h-3 w-1.5 rounded-full", m.bg)} />
            <m.Icon aria-hidden className={cn("size-3.5", m.abnormal ? m.text : "")} />
            {t(code)}
          </li>
        );
      })}
    </ul>
  );
}

/** Body colours of the flow (VIS-12): one dot per product model. */
function ModelLegend() {
  const t = useTranslations("live");
  const plant = usePlant();
  const products = plant.assets?.products ?? [];
  if (products.length === 0) return null;
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label={t("modelLegend")} data-testid="model-legend">
      <li className="font-medium">{t("modelLegend")}:</li>
      {products.map((p) => (
        <li key={p.code} className="flex items-center gap-1.5">
          <span aria-hidden className="size-3 rounded-full border border-card shadow-[0_0_0_1px_var(--border)]" style={{ background: plant.productColor(p.code) }} />
          {p.name}
        </li>
      ))}
    </ul>
  );
}

/** /live — the plant online (SPEC §13.2): schema, line KPIs, shift Gantt, incidents; ?tv=1. */
export function LiveView() {
  const t = useTranslations("live");
  const tv = useSearchParams().get("tv") === "1";
  const plant = usePlant();
  const ready = useLive((s) => s.ready);
  const [selected, setSelected] = useState<string | null>(null);

  if (tv) {
    return (
      <div className="flex min-h-dvh flex-col gap-4 p-5" data-testid="live-tv">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold">{plant.name(plant.assets?.site)}</h1>
          <div className="flex items-center gap-3">
            <PlantClock large />
            <SpeedBadge />
            <ConnectionBadge />
            <Link
              href="/live"
              aria-label={t("tvExit")}
              title={t("tvExit")}
              className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground opacity-40 hover:bg-accent hover:text-foreground hover:opacity-100 focus-visible:opacity-100"
            >
              <X aria-hidden className="size-4" />
            </Link>
          </div>
        </div>
        {/* wall screen: schema and line KPIs, open stops (with the loss) and alerts beside them (VIS-15) */}
        <div className="grid min-h-0 flex-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_400px]">
          <div className="flex min-w-0 flex-col gap-4">
            <PlantSchema selected={selected} onSelect={setSelected} />
            <LineKpis large />
            <Card>
              <CardHeader title={t("timeline.title")} icon={<Activity aria-hidden className="size-4" />} />
              <CardBody>
                <ShiftTimeline />
              </CardBody>
            </Card>
          </div>
          <Card className="flex max-h-[calc(100dvh-6.5rem)] min-h-0 flex-col overflow-hidden">
            <CardHeader title={t("incidents.title")} icon={<ListTodo aria-hidden className="size-4" />} />
            <div className="min-h-0 flex-1 overflow-y-auto">
              <Incidents onSelect={setSelected} />
            </div>
          </Card>
        </div>
        <EquipmentDrawer code={selected} onClose={() => setSelected(null)} />
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>
        <Link href="/live?tv=1" className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground">
          <Tv aria-hidden className="size-4" />
          {t("tvMode")}
        </Link>
      </div>
      <Card className="overflow-hidden">
        <div className="px-3 pt-4 pb-2">
          {ready ? null : <p className="px-2 pb-2 text-sm text-muted-foreground">{t("waiting")}</p>}
          <PlantSchema selected={selected} onSelect={setSelected} />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-t px-4 py-2.5">
          <StateLegend />
          <ModelLegend />
        </div>
      </Card>
      <LineKpis />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
        <Card>
          <CardHeader title={t("timeline.title")} icon={<Activity aria-hidden className="size-4" />} subtitle={t("timeline.subtitle")} />
          <CardBody>
            <ShiftTimeline />
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("incidents.title")} icon={<ListTodo aria-hidden className="size-4" />} />
          <Incidents onSelect={setSelected} />
        </Card>
      </div>
      <EquipmentDrawer code={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
