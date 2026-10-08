import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MaintenanceView } from "./maintenance-view";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("maintenance") };
}

export default function Page() {
  return <MaintenanceView />;
}
