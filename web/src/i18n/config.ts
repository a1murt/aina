/** UI languages (SPEC §13.1): Russian is the default, Kazakh strings await native review. */
export const locales = ["ru", "kk"] as const;
export type Locale = (typeof locales)[number];
export const defaultLocale: Locale = "ru";
export const LOCALE_COOKIE = "NEXT_LOCALE";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (locales as readonly string[]).includes(value);
}
