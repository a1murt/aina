"use client";

import { Activity, ListTodo, Tv } from "lucide-react";
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
          </div>
        </div>
        <PlantSchema selected={selected} onSelect={setSelected} />
        <LineKpis large />
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
        <div className="border-t px-4 py-2.5">
          <StateLegend />
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
