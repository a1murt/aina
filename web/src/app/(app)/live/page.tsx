import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { LiveView } from "./live-view";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("live") };
}

export default function LivePage() {
  return <LiveView />;
}
