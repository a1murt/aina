"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Modal panel: a slide-out from the right (`side="right"`) or a centred dialog. Closes on Esc and
 * on the backdrop; focus moves into the panel and back to the opener.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
  side = "right",
  className,
  footer,
  labelledBy,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  side?: "right" | "center";
  className?: string;
  footer?: ReactNode;
  labelledBy?: string;
}) {
  const t = useTranslations("common");
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [open, onClose]);
  if (!open) return null;
  const headingId = labelledBy ?? "sheet-title";
  return (
    <div className="fixed inset-0 z-50 flex">
      <div aria-hidden className="absolute inset-0 bg-black/45 backdrop-blur-[1px]" onClick={onClose} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        className={cn(
          "relative flex max-h-dvh flex-col bg-card text-card-foreground shadow-2xl outline-none",
          side === "right"
            ? "ml-auto h-dvh w-full max-w-[520px] animate-[slide-in_160ms_ease-out] border-l"
            : "m-auto max-h-[92dvh] w-[min(92vw,960px)] rounded-xl border",
          className,
        )}
      >
        <header className="flex items-center justify-between gap-3 border-b px-5 py-3.5">
          <h2 id={headingId} className="text-lg font-semibold">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex size-9 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label={t("close")}
          >
            <X className="size-5" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer ? <footer className="border-t px-5 py-3">{footer}</footer> : null}
      </div>
    </div>
  );
}
