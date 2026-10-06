"use client";

import { useTransition } from "react";
import { Languages } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { setLocale } from "@/i18n/actions";
import { locales } from "@/i18n/config";

export function LocaleSwitcher() {
  const t = useTranslations("locale");
  const current = useLocale();
  const [pending, startTransition] = useTransition();

  return (
    <div role="group" aria-label={t("label")} className="flex items-center gap-2">
      <Languages aria-hidden className="size-4 text-muted-foreground" />
      {locales.map((locale) => (
        <Button
          key={locale}
          size="sm"
          variant={locale === current ? "default" : "outline"}
          aria-pressed={locale === current}
          disabled={pending}
          onClick={() => startTransition(() => setLocale(locale))}
        >
          {t(locale)}
        </Button>
      ))}
    </div>
  );
}
