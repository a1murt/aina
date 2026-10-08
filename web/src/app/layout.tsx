import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { cookies, headers } from "next/headers";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";

// Fonts are bundled from node_modules — no requests to external font CDNs (OFFLINE, FR-UI-02).
import "@fontsource-variable/inter";
import "./globals.css";

import { Providers } from "@/components/providers";
import { effectiveTheme, THEME_COOKIE } from "@/lib/theme";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("app");
  return { title: { default: t("name"), template: `%s · ${t("name")}` }, description: t("tagline") };
}

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  const locale = await getLocale();
  const pathname = (await headers()).get("x-pathname") ?? "/";
  const theme = effectiveTheme(pathname, (await cookies()).get(THEME_COOKIE)?.value);
  return (
    <html lang={locale} className={theme === "dark" ? "dark" : undefined} style={{ colorScheme: theme }} suppressHydrationWarning>
      <body className="min-h-dvh font-sans antialiased">
        <NextIntlClientProvider>
          <Providers>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
