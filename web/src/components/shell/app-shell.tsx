"use client";

import {
  Factory,
  FileText,
  FileUp,
  LayoutDashboard,
  ShieldCheck,
  SlidersHorizontal,
  Tablet,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState, type ReactNode } from "react";

import { AlertsBell } from "@/components/alerts";
import { COPILOT_ROLES, CopilotButton } from "@/components/copilot";
import { LocaleSwitcher } from "@/components/locale-switcher";
import { PlantProvider } from "@/components/plant-context";
import { ConnectionBadge, PlantClock, SpeedBadge, ThemeToggle, UserMenu } from "@/components/shell/header-widgets";
import { getClaims, logout } from "@/lib/api/client";
import { visibleScreens, type Claims, type ScreenKey } from "@/lib/auth";
import { useLiveConnection } from "@/lib/ws";
import { cn } from "@/lib/utils";

const ICONS: Record<ScreenKey, LucideIcon> = {
  director: LayoutDashboard,
  live: Factory,
  operator: Tablet,
  maintenance: Wrench,
  quality: ShieldCheck,
  import: FileUp,
  reports: FileText,
  demo: SlidersHorizontal,
};

function navHref(key: ScreenKey, href: string, claims: Claims): string {
  if (key === "operator" && claims.role === "operator" && claims.lines[0]) return `/operator/${claims.lines[0]}`;
  return href;
}

function Nav({ claims }: { claims: Claims }) {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const many = visibleScreens(claims.role).length > 4;
  return (
    <nav aria-label={t("label")} className="flex min-w-0 items-center gap-0.5 overflow-x-auto">
      {visibleScreens(claims.role).map((s) => {
        const Icon = ICONS[s.key];
        const active = pathname === s.href || pathname.startsWith(`${s.href}/`);
        return (
          <Link
            key={s.key}
            href={navHref(s.key, s.href, claims)}
            data-testid={`nav-${s.key}`}
            aria-current={active ? "page" : undefined}
            title={t(s.key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm font-medium whitespace-nowrap",
              active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
            )}
          >
            <Icon aria-hidden className="size-4" />
            <span className={many ? "hidden xl:inline" : undefined}>{t(s.key)}</span>
          </Link>
        );
      })}
    </nav>
  );
}

/**
 * Authenticated layout: one WS connection for the tab, plant reference data, header with plant
 * time, shift, sim speed, connection, alerts, language, theme and user (SPEC §13.1). `?tv=1`
 * hides the header (TV mode of /live).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const t = useTranslations("app");
  const tv = useSearchParams().get("tv") === "1";
  const [claims, setClaims] = useState<Claims | null>(null);
  useEffect(() => {
    const c = getClaims();
    if (!c) logout();
    else setClaims(c);
  }, []);
  useLiveConnection(claims !== null);
  const pathname = usePathname();
  const copilot =
    claims !== null && COPILOT_ROLES.includes(claims.role) && ["/director", "/live", "/maintenance", "/quality"].some((p) => pathname.startsWith(p));

  return (
    <PlantProvider>
      <div className="flex min-h-dvh flex-col bg-background text-foreground" data-tv={tv ? "1" : undefined}>
        {!tv && claims ? (
          <header className="sticky top-0 z-30 border-b bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80">
            <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-3 px-4">
              <Link href="/" className="flex shrink-0 items-center gap-2 font-semibold tracking-tight">
                <span className="inline-flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
                  <Factory aria-hidden className="size-4" />
                </span>
                <span className="hidden 2xl:inline">{t("name")}</span>
              </Link>
              <Nav claims={claims} />
              <div className="ml-auto flex shrink-0 items-center gap-2.5">
                {copilot ? <CopilotButton /> : null}
                <PlantClock />
                <SpeedBadge />
                <ConnectionBadge />
                <AlertsBell />
                <LocaleSwitcher />
                <ThemeToggle />
                <UserMenu claims={claims} />
              </div>
            </div>
          </header>
        ) : null}
        <main className="flex-1">{claims ? children : null}</main>
      </div>
    </PlantProvider>
  );
}
