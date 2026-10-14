import type { BottleneckResponse, LossItem, LossesResponse } from "@/lib/api/types";

/** One loss item counted against the plant output, scaled by the bottleneck weight of its line. */
export interface BottleneckLossItem extends LossItem {
  /** Share of the period's shifts in which the item's line limited the plant (1 for output quality). */
  weight: number;
}

export interface BottleneckLosses {
  items: BottleneckLossItem[];
  totals: { minutes: number; units: number; kzt: number };
  /** Bottleneck lines with their share of shifts, largest first. */
  lines: Array<{ line: string; share: number }>;
}

/** Loss category of quality (scrap and rework) in GET /kpi/losses. */
const QUALITY = "quality";

/**
 * Losses that cost plant output (WIN-4, CONS-7): only `loss = true` items of the line that was
 * the bottleneck, weighted by the share of shifts it was the bottleneck (GET /bottleneck), plus
 * quality losses of the output line. Starvation and blocking of non-bottleneck lines are
 * consequences of the bottleneck and are left out, as is planned downtime (`loss = false`).
 *
 * The API totals keep their ISO 22400 meaning (sum over all lines); this is a view filter only.
 * Returns null while the bottleneck is unknown. The copilot uses the same definition
 * (`qost_api.queries.losses.bottleneck_loss_view`); keep the two in sync.
 */
export function bottleneckLosses(
  losses: LossesResponse,
  bottleneck: BottleneckResponse | null | undefined,
  outputLine: string | null,
): BottleneckLosses | null {
  const raw: Record<string, number> = {};
  for (const [line, s] of Object.entries(bottleneck?.shares ?? {})) {
    const w = (s.sole ?? 0) + (s.shifting ?? 0);
    if (w > 0) raw[line] = w;
  }
  let sum = Object.values(raw).reduce((a, b) => a + b, 0);
  if (sum <= 0 && bottleneck?.overall) {
    raw[bottleneck.overall] = 1;
    sum = 1;
  }
  if (sum <= 0) return null;
  const weights: Record<string, number> = {};
  for (const [line, w] of Object.entries(raw)) weights[line] = w / sum;

  const items: BottleneckLossItem[] = [];
  for (const item of losses.items) {
    if (!item.loss) continue;
    const line = item.line ?? "";
    let weight = weights[line] ?? 0;
    if (item.category === QUALITY && outputLine && line === outputLine) weight = 1;
    if (weight <= 0) continue;
    items.push({ ...item, weight, minutes: item.minutes * weight, units: item.units * weight, kzt: item.kzt * weight });
  }
  items.sort((a, b) => b.units - a.units);
  const totals = items.reduce(
    (acc, i) => ({ minutes: acc.minutes + i.minutes, units: acc.units + i.units, kzt: acc.kzt + i.kzt }),
    { minutes: 0, units: 0, kzt: 0 },
  );
  const lines = Object.entries(weights)
    .map(([line, share]) => ({ line, share }))
    .sort((a, b) => b.share - a.share);
  return { items, totals, lines };
}
