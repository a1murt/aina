"use client";

import { ArrowLeft, Check } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { categoryIcon, reasonIcon } from "@/components/live/icons";
import { usePlant } from "@/components/plant-context";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Stop reason: category grid → reason grid with icons → confirmation (SPEC §13.2). `touch` gives
 * the terminal sizes (targets ≥ 64 px).
 */
export function ReasonPicker({
  touch = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  touch?: boolean;
  busy?: boolean;
  onConfirm: (reasonCode: string) => void;
  onCancel?: () => void;
}) {
  const t = useTranslations("reasonPicker");
  const { categories, name } = usePlant();
  const [cat, setCat] = useState<string | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const cats = categories.filter((c) => c.code !== "UNK");
  const category = cats.find((c) => c.code === cat);
  const chosen = category?.reasons.find((r) => r.code === reason);
  const tile = cn(
    "flex flex-col items-center justify-center gap-2 rounded-xl border bg-card text-center font-medium transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring",
    touch ? "min-h-28 p-4 text-lg [&_svg]:size-9" : "min-h-20 p-3 text-sm [&_svg]:size-6",
  );

  if (chosen && category) {
    const Icon = reasonIcon(chosen.code, category.code);
    return (
      <div className="flex flex-col items-center gap-6 py-4 text-center" data-testid="reason-confirm">
        <Icon aria-hidden className={cn("text-foreground", touch ? "size-16" : "size-10")} />
        <div>
          <p className="text-sm text-muted-foreground">{name(category)}</p>
          <p className={cn("font-semibold", touch ? "text-3xl" : "text-xl")}>{name(chosen)}</p>
          <code className="text-xs text-muted-foreground">{chosen.code}</code>
        </div>
        <div className="flex w-full max-w-xl gap-3">
          <Button variant="outline" size={touch ? "touch" : "lg"} className="flex-1" onClick={() => setReason(null)} disabled={busy}>
            <ArrowLeft aria-hidden />
            {t("back")}
          </Button>
          <Button size={touch ? "touch" : "lg"} className="flex-1" onClick={() => onConfirm(chosen.code)} disabled={busy} data-testid="reason-submit">
            <Check aria-hidden />
            {t("confirm")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <p className={cn("font-medium", touch ? "text-xl" : "text-sm")}>{category ? name(category) : t("chooseCategory")}</p>
        {category ? (
          <Button variant="outline" size={touch ? "touch" : "sm"} onClick={() => setCat(null)}>
            <ArrowLeft aria-hidden />
            {t("categories")}
          </Button>
        ) : onCancel ? (
          <Button variant="ghost" size={touch ? "touch" : "sm"} onClick={onCancel}>
            {t("cancel")}
          </Button>
        ) : null}
      </div>
      <div className={cn("grid gap-3", touch ? "grid-cols-3 lg:grid-cols-4" : "grid-cols-2 sm:grid-cols-3")}>
        {category
          ? category.reasons.map((r) => {
              const Icon = reasonIcon(r.code, category.code);
              return (
                <button key={r.code} type="button" className={tile} onClick={() => setReason(r.code)} data-testid={`reason-${r.code}`}>
                  <Icon aria-hidden />
                  <span>{name(r)}</span>
                </button>
              );
            })
          : cats.map((c) => {
              const Icon = categoryIcon(c.code);
              return (
                <button key={c.code} type="button" className={tile} onClick={() => setCat(c.code)} data-testid={`category-${c.code}`}>
                  <Icon aria-hidden />
                  <span>{name(c)}</span>
                </button>
              );
            })}
      </div>
    </div>
  );
}
