import type messages from "./messages/ru.json";
import type { Locale } from "@/i18n/config";

// Type-safe next-intl: keys are checked against the Russian (reference) messages.
declare module "next-intl" {
  interface AppConfig {
    Locale: Locale;
    Messages: typeof messages;
  }
}
