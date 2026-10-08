"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";

import { setLocale } from "@/i18n/actions";
import { locales } from "@/i18n/config";
import { cn } from "@/lib/utils";

/** Compact RU / KK switch for the header (SPEC §13.1); the language lives in a cookie. */
export function LocaleSwitcher({ className }: { className?: string }) {
  const t = useTranslations("locale");
  const current = useLocale();
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  return (
    <div role="group" aria-label={t("label")} className={cn("flex items-center rounded-md border p-0.5", className)}>
      {locales.map((locale) => (
        <button
          key={locale}
          type="button"
          lang={locale}
          title={t(locale)}
          aria-pressed={locale === current}
          disabled={pending}
          onClick={() =>
            startTransition(async () => {
              await setLocale(locale);
              router.refresh();
            })
          }
          className={cn(
            "rounded px-2 py-1 text-xs font-semibold uppercase",
            locale === current ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {locale}
        </button>
      ))}
    </div>
  );
}
