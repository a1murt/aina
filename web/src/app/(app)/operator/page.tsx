import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { LinePicker } from "@/components/operator/line-picker";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("operator") };
}

/** Terminal without a line (master, admin): choose the line. */
export default function OperatorIndexPage() {
  return <LinePicker />;
}
