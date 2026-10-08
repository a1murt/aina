import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PlannedScreen } from "@/components/planned-screen";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("maintenance") };
}

/** P1 screen (SPEC §13.2): start page of the role until its stage lands. */
export default function Page() {
  return <PlannedScreen screen="maintenance" />;
}
