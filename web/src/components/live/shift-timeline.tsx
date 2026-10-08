"use client";

import { useQuery } from "@tanstack/react-query";
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams, EChartsOption } from "echarts";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { baseOption, EChart, useChartColors } from "@/components/charts/echart";
import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { api } from "@/lib/api/client";
import type { TimelineView } from "@/lib/api/types";
import { plantNow, useLive } from "@/lib/live-store";

/** State timeline of the shift (Gantt): lines and class-A equipment (SPEC §13.2 /live). */
export function ShiftTimeline() {
  const t = useTranslations("live.timeline");
  const ts = useTranslations("states");
  const plant = usePlant();
  const c = useChartColors();
  const clock = useLive((s) => s.clock);
  const shiftKey = clock?.shift ? `${clock.shift.date}/${clock.shift.code}` : "none";
  const entities = useMemo(
    () => [
      ...(plant.assets?.flow ?? []),
      ...Object.values(plant.equipment)
        .filter((e) => e.criticality === "A")
        .map((e) => e.code),
    ],
    [plant.assets, plant.equipment],
  );
  const q = useQuery({
    queryKey: ["timeline", shiftKey, entities.join(",")],
    enabled: entities.length > 0 && clock !== null,
    refetchInterval: 8_000,
    queryFn: () => {
      const st = useLive.getState().clock;
      const now = plantNow(st) ?? Date.now();
      const elapsed = st?.shift?.elapsed_min ?? 60;
      const from = new Date(now - Math.max(elapsed, 30) * 60_000 - 60_000).toISOString();
      return api<TimelineView>("/api/v1/history/timeline", { query: { entity: entities, from, to: new Date(now).toISOString() } });
    },
  });

  const option = useMemo<EChartsOption | null>(() => {
    if (!c || !q.data) return null;
    const rows = entities.filter((e) => q.data.entities.some((x) => x.code === e)).reverse();
    const toMs = Date.parse(q.data.to);
    const fromMs = Date.parse(q.data.from);
    const data: Array<{ value: [number, number, number, string, string]; itemStyle: { color: string } }> = [];
    for (const ent of q.data.entities) {
      const y = rows.indexOf(ent.code);
      if (y < 0) continue;
      for (const iv of ent.intervals) {
        const s = Math.max(Date.parse(iv.start), fromMs);
        const e = iv.end ? Date.parse(iv.end) : toMs;
        if (e <= s) continue;
        data.push({ value: [y, s, e, iv.state, iv.reason_code ?? ""], itemStyle: { color: c.state(iv.state) } });
      }
    }
    const renderItem = (params: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => {
      const y = api.value(0) as number;
      const start = api.coord([api.value(1), y]);
      const end = api.coord([api.value(2), y]);
      const size = api.size?.([0, 1]) as number[] | undefined;
      const h = (size?.[1] ?? 20) * 0.62;
      const x0 = start[0] ?? 0;
      const width = Math.max(1, (end[0] ?? 0) - x0);
      return {
        type: "rect" as const,
        shape: { x: x0, y: (start[1] ?? 0) - h / 2, width, height: h, r: 2 },
        style: api.style(),
      };
    };
    return {
      ...baseOption(c),
      grid: { left: 92, right: 16, top: 8, bottom: 28 },
      xAxis: {
        type: "time",
        min: fromMs,
        max: toMs,
        axisLabel: { color: c.muted, formatter: (v: number) => plant.fmt.time(v) },
        axisLine: { lineStyle: { color: c.border } },
        splitLine: { show: true, lineStyle: { color: c.border, opacity: 0.5 } },
      },
      yAxis: {
        type: "category",
        data: rows,
        axisLabel: { color: c.fg, fontSize: 11, fontWeight: 500 },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      tooltip: {
        ...(baseOption(c).tooltip as object),
        formatter: (p: unknown) => {
          const v = (p as { value: [number, number, number, string, string] }).value;
          const reason = v[4] ? ` · ${plant.name(plant.reasons[v[4]], v[4])}` : "";
          const st = ts(v[3] as Parameters<typeof ts>[0]);
          return `<b>${rows[v[0]] ?? ""}</b><br/>${st}${reason}<br/>${plant.fmt.time(v[1])}–${plant.fmt.time(v[2])} · ${plant.fmt.clock((v[2] - v[1]) / 60_000)}`;
        },
      },
      series: [{ type: "custom", renderItem: renderItem as never, encode: { x: [1, 2], y: 0 }, data }],
    };
  }, [c, q.data, entities, plant, ts]);

  return (
    <div data-testid="shift-timeline">
      <QueryState loading={q.isLoading} error={q.error} compact />
      {option ? <EChart option={option} height={Math.max(220, entities.length * 24 + 40)} ariaLabel={t("label")} /> : null}
    </div>
  );
}
