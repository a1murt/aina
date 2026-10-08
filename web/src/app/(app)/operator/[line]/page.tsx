import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { OperatorTerminal } from "@/components/operator/terminal";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("nav");
  return { title: t("operator") };
}

export default async function OperatorPage({ params }: { params: Promise<{ line: string }> }) {
  const { line } = await params;
  return <OperatorTerminal line={decodeURIComponent(line)} />;
}
