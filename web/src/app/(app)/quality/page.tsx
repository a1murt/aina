import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { QualityView } from "./quality-view";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("quality") };
}

export default function Page() {
  return <QualityView />;
}
