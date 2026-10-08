import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { DemoView } from "./demo-view";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("demo") };
}

export default function Page() {
  return <DemoView />;
}
