import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ImportView } from "./import-view";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("import") };
}

export default function Page() {
  return <ImportView />;
}
