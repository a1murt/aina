"use client";

import { useSyncExternalStore } from "react";

import type { Role } from "@/lib/auth";

import { getClaims } from "./client";

const noop = () => () => {};

/**
 * Role of the session token for UI gating; null on the server and during hydration, so the first
 * client render matches the server markup (the API checks every request anyway).
 */
export function useRole(): Role | null {
  return useSyncExternalStore(
    noop,
    () => getClaims()?.role ?? null,
    () => null,
  );
}
