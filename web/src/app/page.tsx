import { Factory } from "lucide-react";
import { getTranslations } from "next-intl/server";

import { LocaleSwitcher } from "@/components/locale-switcher";
import { StateLegend } from "@/components/state-legend";

export default async function HomePage() {
  const t = await getTranslations("home");
  return (
    <main className="mx-auto flex max-w-5xl flex-col gap-10 px-6 py-10">
      <header className="flex flex-wrap items-start justify-between gap-6">
        <div className="flex items-center gap-4">
          <Factory aria-hidden className="size-10 text-isa-normal" />
          <div>
            <h1 className="text-3xl font-semibold tracking-tight">{t("title")}</h1>
            <p className="text-muted-foreground">{t("subtitle")}</p>
          </div>
        </div>
        <LocaleSwitcher />
      </header>

      <section aria-labelledby="legend" className="flex flex-col gap-4">
        <div>
          <h2 id="legend" className="text-xl font-semibold">
            {t("legendTitle")}
          </h2>
          <p className="text-sm text-muted-foreground">{t("legendNote")}</p>
        </div>
        <StateLegend />
      </section>
    </main>
  );
}
