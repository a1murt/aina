"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { create } from "zustand";

import { effectiveTheme, isTheme, THEME_COOKIE, type Theme } from "@/lib/theme";

interface ThemeState {
  choice: Theme | null;
  setChoice: (t: Theme) => void;
}

function cookieChoice(): Theme | null {
  if (typeof document === "undefined") return null;
  const raw = document.cookie.split("; ").find((c) => c.startsWith(`${THEME_COOKIE}=`));
  const v = raw?.slice(THEME_COOKIE.length + 1);
  return isTheme(v) ? v : null;
}

export const useThemeChoice = create<ThemeState>((set) => ({
  choice: cookieChoice(),
  setChoice: (choice) => {
    document.cookie = `${THEME_COOKIE}=${choice}; path=/; max-age=31536000; samesite=lax`;
    set({ choice });
  },
}));

/** Current theme: the stored choice, else the route default (dark for /live and /operator). */
export function useTheme(): Theme {
  const pathname = usePathname();
  const choice = useThemeChoice((s) => s.choice);
  return effectiveTheme(pathname, choice);
}

/**
 * Keeps the `dark` class of <html> in sync with the route and the user's choice (the server
 * renders the first class from the same cookie, so there is no flash).
 */
export function ThemeSync() {
  const theme = useTheme();
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
  }, [theme]);
  return null;
}
