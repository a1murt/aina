"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

const FOCUSABLE =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), iframe, [contenteditable="true"], [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.getClientRects().length > 0 && !el.closest("[inert]"),
  );
}

/**
 * Panel: a slide-out from the right (`side="right"`) or a centred dialog.
 *
 * - `modal` (default): dimmed backdrop that closes on click, the page behind is inert to the
 *   pointer, Tab / Shift+Tab cycle inside the panel, Esc closes.
 * - `modal={false}`: no backdrop, the page stays interactive (a side tool panel next to a chart);
 *   Esc closes while focus is inside the panel.
 *
 * Focus moves into the panel when it opens and back to the opener when it closes. The effect
 * depends on `open` only (`onClose` is read through a ref), so parent re-renders — inline
 * `onClose`, clock ticks, typing — never move the focus (FE-1).
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
  side = "right",
  modal = true,
  className,
  footer,
  labelledBy,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  side?: "right" | "center";
  /** false: no backdrop and no focus trap, the page behind stays usable. */
  modal?: boolean;
  className?: string;
  footer?: ReactNode;
  labelledBy?: string;
}) {
  const t = useTranslations("common");
  const panel = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const modalRef = useRef(modal);
  useEffect(() => {
    onCloseRef.current = onClose;
    modalRef.current = modal;
  });
  const autoId = useId();

  useEffect(() => {
    if (!open) return;
    const el = panel.current;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || !el) return;
      const inside = e.target instanceof Node && el.contains(e.target);
      if (e.key === "Escape") {
        if (modalRef.current || inside) onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !modalRef.current) return;
      const items = focusables(el);
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        e.preventDefault();
        el.focus();
        return;
      }
      const active = document.activeElement;
      if (!inside || !el.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (active === first || active === el)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    el?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      // give the focus back only if it is still ours (closed panel → body); a non-modal panel
      // must not pull the focus away from where the user went on the page
      const active = document.activeElement;
      if (!active || active === document.body || (el?.contains(active) ?? false)) opener?.focus?.();
    };
  }, [open]);

  if (!open) return null;
  const headingId = labelledBy ?? `sheet-title-${autoId}`;
  return (
    <div className={cn("fixed inset-0 z-50 flex", !modal && "pointer-events-none")}>
      {modal ? (
        <div
          aria-hidden
          className="absolute inset-0 bg-black/45 backdrop-blur-[1px]"
          onClick={() => onCloseRef.current()}
        />
      ) : null}
      <div
        ref={panel}
        role="dialog"
        aria-modal={modal}
        aria-labelledby={headingId}
        tabIndex={-1}
        className={cn(
          "pointer-events-auto relative flex max-h-dvh flex-col bg-card text-card-foreground shadow-2xl outline-none",
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
            onClick={() => onCloseRef.current()}
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
