import { Lock } from "lucide-react";
import Link from "next/link";
import { cookies } from "next/headers";
import { getTranslations } from "next-intl/server";

import { decodeToken, startPage, TOKEN_COOKIE } from "@/lib/auth";

/** A screen the role may not open (FR-UI-03): the middleware rewrites here with HTTP 403. */
export default async function ForbiddenPage() {
  const t = await getTranslations("forbidden");
  const claims = decodeToken((await cookies()).get(TOKEN_COOKIE)?.value);
  return (
    <main className="flex min-h-dvh items-center justify-center bg-background px-4">
      <div className="max-w-md text-center" data-testid="forbidden">
        <Lock aria-hidden className="mx-auto mb-4 size-10 text-muted-foreground" />
        <h1 className="text-xl font-semibold">{t("title")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">{t("text")}</p>
        <Link
          href={claims ? startPage(claims) : "/login"}
          className="mt-6 inline-flex h-10 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground"
        >
          {t("back")}
        </Link>
      </div>
    </main>
  );
}
