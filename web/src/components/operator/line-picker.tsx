"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { usePlant } from "@/components/plant-context";
import { StateBadge } from "@/components/state-badge";
import { useLive } from "@/lib/live-store";

export function LinePicker() {
  const t = useTranslations("operator");
  const plant = usePlant();
  const lines = useLive((s) => s.lines);
  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="mb-1 text-2xl font-semibold">{t("pickLine")}</h1>
      <p className="mb-6 text-muted-foreground">{t("pickLineHint")}</p>
      <div className="grid grid-cols-2 gap-4">
        {(plant.assets?.flow ?? []).map((code) => (
          <Link
            key={code}
            href={`/operator/${code}`}
            className="flex min-h-28 flex-col justify-between rounded-2xl border-2 bg-card p-5 hover:bg-accent"
          >
            <span className="text-2xl font-semibold">{plant.name(plant.lines[code], code)}</span>
            <span className="flex items-center justify-between text-muted-foreground">
              <code>{code}</code>
              <StateBadge state={lines[code]?.state} size="md" />
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}
