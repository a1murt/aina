"use client";

import { Factory, Loader2, LogIn } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";

import { LocaleSwitcher } from "@/components/locale-switcher";
import { Button } from "@/components/ui/button";
import { api, ApiError, saveToken } from "@/lib/api/client";
import type { TokenView } from "@/lib/api/types";
import { canAccess, decodeToken, ROLES, startPage } from "@/lib/auth";

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
      const target = claims && next && next.startsWith("/") && canAccess(claims, next.split("?")[0] ?? next) ? next : claims ? startPage(claims) : "/";
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
          <span className="inline-flex size-11 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Factory aria-hidden className="size-6" />
          </span>
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
