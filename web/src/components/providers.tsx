"use client";

import {
  QueryClient,
  QueryClientProvider,
  type InvalidateQueryFilters,
  type QueryFilters,
} from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import { selectEpochSeq, useLive } from "@/lib/live-store";
import { ThemeSync } from "@/components/theme-sync";

/** The browser's query client, for code outside React (WS handlers, stores, actions). */
let browserClient: QueryClient | null = null;

/** The query client of this tab, or null outside the browser / before {@link Providers} mounted. */
export function getQueryClient(): QueryClient | null {
  return browserClient;
}

/**
 * Mark queries stale and refetch the active ones in the background (data stays on screen until
 * the new answer arrives). No filters = every query.
 */
export function invalidateQueries(filters?: InvalidateQueryFilters): Promise<void> {
  return browserClient?.invalidateQueries(filters) ?? Promise.resolve();
}

/** Drop cached data back to the initial state and refetch the active queries (shows loading). */
export function resetQueries(filters?: QueryFilters): Promise<void> {
  return browserClient?.resetQueries(filters) ?? Promise.resolve();
}

function makeClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        // 4xx are answers (403 is shown as «нет доступа»), retry only transport errors
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
      },
    },
  });
  // registered at creation so children's mount effects can already use the helpers
  if (typeof window !== "undefined") browserClient = client;
  return client;
}

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(makeClient);

  useEffect(() => {
    browserClient = client;
    // A new plant timeline (demo reset) makes every cached answer stale (FE-13).
    const unsubscribe = useLive.subscribe((s, prev) => {
      if (selectEpochSeq(s) !== selectEpochSeq(prev)) void client.invalidateQueries();
    });
    return () => {
      unsubscribe();
      if (browserClient === client) browserClient = null;
    };
  }, [client]);

  return (
    <QueryClientProvider client={client}>
      <ThemeSync />
      {children}
    </QueryClientProvider>
  );
}
