"use client";

import { Loader2, Lock, WifiOff, CircleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { useErrorMessage } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/** Loading / error placeholder of a query; 403 is shown as «нет доступа» (FR-UI-03). */
export function QueryState({
  loading,
  error,
  className,
  children,
  compact = false,
}: {
  loading?: boolean;
  error?: unknown;
  className?: string;
  children?: ReactNode;
  compact?: boolean;
}) {
  const t = useTranslations("common");
  const describe = useErrorMessage();
  if (error) {
    const e = describe(error);
    const Icon = e.forbidden ? Lock : e.offline ? WifiOff : CircleAlert;
    // A failed panel names what failed in plant words; the server detail stays behind «Подробнее».
    const text = e.forbidden ? t("forbidden") : e.generic ? t("loadError") : e.text;
    return (
      <div role="status" className={cn("flex items-start gap-2 text-sm text-muted-foreground", compact ? "py-2" : "py-6", className)}>
        <Icon aria-hidden className={cn("mt-0.5 size-4 shrink-0", !e.forbidden && "text-severity-critical")} />
        <div className="min-w-0">
          <p>{text}</p>
          {!e.forbidden ? <ErrorDetails detail={e.detail} /> : null}
        </div>
      </div>
    );
  }
  if (loading) {
    return (
      <div role="status" className={cn("flex items-center gap-2 text-sm text-muted-foreground", compact ? "py-2" : "py-6", className)}>
        <Loader2 aria-hidden className="size-4 animate-spin" />
        {children ?? t("loading")}
      </div>
    );
  }
  return null;
}

/** Collapsed raw server detail under a localised error (FE-5). */
export function ErrorDetails({ detail, className }: { detail: string | null | undefined; className?: string }) {
  const t = useTranslations("errors");
  if (!detail) return null;
  return (
    <details className={cn("text-xs opacity-80", className)}>
      <summary className="cursor-pointer select-none">{t("details")}</summary>
      <p className="mt-1 whitespace-pre-line break-words font-mono text-[11px]">{detail}</p>
    </details>
  );
}

/** Localised one-line error with the raw detail behind «Подробнее»; for inline action errors. */
export function ErrorMessage({ error, className }: { error: unknown; className?: string }) {
  const describe = useErrorMessage();
  if (!error) return null;
  const e = describe(error);
  return (
    <div role="alert" className={cn("text-sm", className)}>
      <p>{e.text}</p>
      <ErrorDetails detail={e.detail} />
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-muted", className)} />;
}
