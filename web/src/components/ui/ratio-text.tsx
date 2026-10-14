"use client";

import { useTranslations } from "next-intl";

import { usePlant } from "@/components/plant-context";
import { cn } from "@/lib/utils";

/**
 * A KPI ratio (OEE, A, E, Q) shown as a percentage held within 0..100 % (DEMO-6, VIS-5). Early
 * in a shift E can exceed 1 (WIP carried in at the shift start); the display then reads
 * "100 %" and the tooltip carries the raw early estimate. The KPI value itself is untouched.
 */
export function RatioText({
  value,
  digits = 1,
  className,
  testId,
}: {
  value: number | null | undefined;
  digits?: number;
  className?: string;
  testId?: string;
}) {
  const t = useTranslations("live");
  const { fmt } = usePlant();
  const r = fmt.ratioPct(value, digits);
  return (
    <span
      data-testid={testId}
      data-raw={r.raw ?? undefined}
      data-clamped={r.clamped ? "1" : undefined}
      title={r.clamped ? t("earlyEstimate", { value: r.rawText }) : undefined}
      className={cn(r.clamped && "cursor-help underline decoration-muted-foreground/50 decoration-dotted underline-offset-4", className)}
    >
      {r.text}
    </span>
  );
}
