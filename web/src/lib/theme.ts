/**
 * Themes (SPEC §13.1): dark is the default of the control-room screens (/live, /operator),
 * light elsewhere; the header toggle stores an explicit choice in a cookie that wins everywhere.
 */
export type Theme = "light" | "dark";
export const THEME_COOKIE = "qost_theme";

export function routeTheme(pathname: string): Theme {
  return pathname.startsWith("/live") || pathname.startsWith("/operator") ? "dark" : "light";
}

export function isTheme(v: unknown): v is Theme {
  return v === "light" || v === "dark";
}

export function effectiveTheme(pathname: string, choice: string | undefined | null): Theme {
  return isTheme(choice) ? choice : routeTheme(pathname);
}
