"use client";

import { useTranslations } from "next-intl";

import { stateMeta, severityMeta } from "@/lib/states";
import { cn } from "@/lib/utils";

/** State = colour + icon + label (ISA-101, SPEC §13.1). */
export function StateBadge({
  state,
  size = "sm",
  className,
}: {
  state: string | null | undefined;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const t = useTranslations("states");
  const meta = stateMeta(state);
  const code = (state ?? "IDLE_NO_PLAN") as Parameters<typeof t>[0];
  return (
    <span
      data-state={state ?? "IDLE_NO_PLAN"}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border font-medium whitespace-nowrap",
        size === "sm" && "px-1.5 py-0.5 text-xs [&_svg]:size-3.5",
        size === "md" && "px-2 py-1 text-sm [&_svg]:size-4",
        size === "lg" && "px-3 py-1.5 text-base [&_svg]:size-5",
        meta.abnormal ? [meta.border, meta.text, meta.tint] : "border-border bg-muted text-muted-foreground",
      )}
    >
      <meta.Icon aria-hidden />
      <span className={cn(className)}>{t(code)}</span>
    </span>
  );
}

export function SeverityIcon({ severity, className }: { severity: string; className?: string }) {
  const meta = severityMeta(severity);
  return <meta.Icon aria-hidden className={cn("size-4 shrink-0", meta.text, className)} />;
}

export function SeverityBadge({ severity }: { severity: string }) {
  const t = useTranslations("severity");
  const meta = severityMeta(severity);
  const key = (["critical", "warning", "info"].includes(severity) ? severity : "info") as "critical" | "warning" | "info";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs font-medium",
        meta.border,
        severity === "warning" ? "text-foreground" : meta.text,
        meta.tint,
      )}
    >
      <meta.Icon aria-hidden className={cn("size-3.5", meta.text)} />
      {t(key)}
    </span>
  );
}
