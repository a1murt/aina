"use server";

import { cookies } from "next/headers";

import { isLocale, LOCALE_COOKIE } from "./config";

const ONE_YEAR_S = 60 * 60 * 24 * 365;

/** Persist the UI language; the current route re-renders with the new messages. */
export async function setLocale(locale: string): Promise<void> {
  if (!isLocale(locale)) return;
  (await cookies()).set(LOCALE_COOKIE, locale, { path: "/", maxAge: ONE_YEAR_S, sameSite: "lax" });
}
