"use client";

import { Loader2, Lock, WifiOff, CircleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { ApiError, NetworkError } from "@/lib/api/client";
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
  if (error) {
    const forbidden = error instanceof ApiError && error.status === 403;
    const offline = error instanceof NetworkError;
    const Icon = forbidden ? Lock : offline ? WifiOff : CircleAlert;
    const text = forbidden ? t("forbidden") : offline ? t("networkError") : t("loadError");
    const detail = error instanceof ApiError && !forbidden ? error.detail || error.title : null;
    return (
      <div role="status" className={cn("flex items-start gap-2 text-sm text-muted-foreground", compact ? "py-2" : "py-6", className)}>
        <Icon aria-hidden className={cn("mt-0.5 size-4 shrink-0", !forbidden && "text-severity-critical")} />
        <div>
          <p>{text}</p>
          {detail ? <p className="text-xs opacity-80">{detail}</p> : null}
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

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-muted", className)} />;
}
