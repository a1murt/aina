import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { DirectorView } from "./director-view";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("director") };
}

export default function Page() {
  return <DirectorView />;
}
