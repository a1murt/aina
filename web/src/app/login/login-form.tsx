"use client";

import { Loader2, LogIn } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";

import { LocaleSwitcher } from "@/components/locale-switcher";
import { BrandMark } from "@/components/shell/brand-mark";
import { Button } from "@/components/ui/button";
import { api, ApiError, saveToken } from "@/lib/api/client";
import type { TokenView } from "@/lib/api/types";
import { canAccess, decodeToken, ROLES, startPage } from "@/lib/auth";

/**
 * Path + query of `next` when it points into this app (same origin), else null — `//host` or
 * `/\\host` are other sites to the browser (FE-11, open redirect).
 */
export function safeNext(next: string | null, origin: string): string | null {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return null;
  try {
    const u = new URL(next, origin);
    return u.origin === origin ? `${u.pathname}${u.search}` : null;
  } catch {
    return null;
  }
}

export function LoginForm() {
  const t = useTranslations("login");
  const tr = useTranslations("roles");
  const tApp = useTranslations("app");
  const router = useRouter();
  const next = useSearchParams().get("next");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const tok = await api<TokenView>("/api/v1/auth/login", {
        method: "POST",
        body: { username: username.trim(), password },
        anonymous: true,
      });
      saveToken(tok.access_token, tok.expires_at);
      const claims = decodeToken(tok.access_token);
      const local = safeNext(next, window.location.origin);
      const target = claims && local && canAccess(claims, local.split("?")[0] ?? local) ? local : claims ? startPage(claims) : "/";
      router.replace(target);
      router.refresh();
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 429) setError(t("throttled"));
      else if (err instanceof ApiError && (err.status === 401 || err.status === 422)) setError(t("invalid"));
      else setError(t("unavailable"));
    }
  }

  return (
    <main className="relative flex min-h-dvh items-center justify-center bg-background px-4">
      <div className="absolute top-4 right-4">
        <LocaleSwitcher />
      </div>
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-3">
          <BrandMark className="size-11 rounded-lg" />
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{tApp("name")}</h1>
            <p className="text-sm text-muted-foreground">{tApp("tagline")}</p>
          </div>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-xl border bg-card p-6 shadow-sm" aria-labelledby="login-title">
          <h2 id="login-title" className="text-lg font-semibold">
            {t("title")}
          </h2>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">{t("username")}</span>
            <input
              name="username"
              autoComplete="username"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="h-10 w-full rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">{t("password")}</span>
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="h-10 w-full rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            />
          </label>
          {error ? (
            <p role="alert" className="text-sm text-severity-critical">
              {error}
            </p>
          ) : null}
          <Button type="submit" className="h-10 w-full" disabled={busy}>
            {busy ? <Loader2 aria-hidden className="animate-spin" /> : <LogIn aria-hidden />}
            {t("submit")}
          </Button>
        </form>
        <div className="mt-5">
          <p className="mb-2 text-xs text-muted-foreground">{t("demoUsers")}</p>
          <div className="flex flex-wrap gap-1.5">
            {ROLES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setUsername(r)}
                className="rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                {tr(r)} · <code>{r}</code>
              </button>
            ))}
          </div>
        </div>
      </div>
    </main>
  );
}
