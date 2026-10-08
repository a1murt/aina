"use client";

import { Construction, Factory } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { usePlant } from "@/components/plant-context";
import { StateBadge } from "@/components/state-badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { useLive } from "@/lib/live-store";

/**
 * Start page of maintenance / quality until their P1 screens (stage M7b): what is coming and the
 * live states of the units, so the role lands on something useful.
 */
export function PlannedScreen({ screen }: { screen: "maintenance" | "quality" }) {
  const t = useTranslations("planned");
  const tn = useTranslations("nav");
  const plant = usePlant();
  const equipment = useLive((s) => s.equipment);
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4" data-testid={`screen-${screen}`}>
      <h1 className="text-xl font-semibold">{tn(screen)}</h1>
      <div className="flex items-start gap-3 rounded-lg border bg-card p-4">
        <Construction aria-hidden className="mt-0.5 size-5 text-muted-foreground" />
        <div>
          <p className="font-medium">{t("title")}</p>
          <p className="text-sm text-muted-foreground">{t(screen)}</p>
          <Link href="/live" className="mt-2 inline-flex items-center gap-1.5 text-sm font-medium underline-offset-4 hover:underline">
            <Factory aria-hidden className="size-4" />
            {tn("live")}
          </Link>
        </div>
      </div>
      <Card>
        <CardHeader title={t("units")} />
        <CardBody>
          <ul className="grid gap-2 sm:grid-cols-2">
            {Object.values(plant.equipment).map((e) => (
              <li key={e.code} className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm">
                <span>
                  <b>{e.code}</b> <span className="text-muted-foreground">{plant.name(e)}</span>
                </span>
                <StateBadge state={equipment[e.code]?.state} />
              </li>
            ))}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}
