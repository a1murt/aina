"use client";

import { api } from "@/lib/api/client";
import type { DowntimeView, Page } from "@/lib/api/types";

/**
 * Operator / master actions. A classification of a stop that is known only from the live view
 * (entity + start) resolves its downtime id at send time, so it can wait in the offline queue.
 */
export type Action =
  | { kind: "request"; method: "POST" | "PATCH"; path: string; body: unknown }
  | { kind: "classify"; entity: string; start_ts: string | null; downtime_id?: number; reason_code: string; comment?: string };

export async function findDowntimeId(entity: string, startTs: string | null): Promise<number | null> {
  const page = await api<Page<DowntimeView>>("/api/v1/downtime", { query: { entity, limit: 20 } });
  const start = startTs ? Date.parse(startTs) : NaN;
  const match =
    page.items.find((d) => d.start_ts && Number.isFinite(start) && Math.abs(Date.parse(d.start_ts) - start) < 5_000) ??
    page.items.find((d) => d.open && !d.import_id) ??
    null;
  return match?.id ?? null;
}

export async function sendAction(a: Action): Promise<unknown> {
  if (a.kind === "request") return api(a.path, { method: a.method, body: a.body });
  const id = a.downtime_id ?? (await findDowntimeId(a.entity, a.start_ts));
  if (id == null) throw new Error(`downtime of ${a.entity} not found`);
  return api(`/api/v1/downtime/${id}`, {
    method: "PATCH",
    body: { reason_code: a.reason_code, comment: a.comment ?? null },
  });
}
