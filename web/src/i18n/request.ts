import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";

import { defaultLocale, isLocale, LOCALE_COOKIE } from "./config";

// Locale comes from a cookie (no locale segment in URLs). Plant time zone: PLANT_TIMEZONE until
// the UI reads site.timezone from GET /api/v1/assets (stage M5).
export default getRequestConfig(async () => {
  const requested = (await cookies()).get(LOCALE_COOKIE)?.value;
  const locale = isLocale(requested) ? requested : defaultLocale;
  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
    timeZone: process.env.PLANT_TIMEZONE ?? "UTC",
  };
});
