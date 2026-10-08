"use client";

import ReactEChartsCore from "echarts-for-react/lib/core";
import { BarChart, CustomChart, LineChart } from "echarts/charts";
import {
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { SVGRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";
import { useEffect, useState } from "react";

import { useTheme } from "@/components/theme-sync";
import { cssVar } from "@/lib/states";
import { cn } from "@/lib/utils";

// Tree-shaken ECharts (SPEC §13.3): only the series and components the screens use, SVG renderer
// (crisp on TV screens, small DOM for sparklines).
echarts.use([
  LineChart,
  BarChart,
  CustomChart,
  GridComponent,
  GraphicComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
  MarkAreaComponent,
  MarkPointComponent,
  SVGRenderer,
]);

export interface ChartColors {
  fg: string;
  muted: string;
  border: string;
  card: string;
  normal: string;
  idle: string;
  alarm: string;
  warning: string;
  info: string;
  planned: string;
  state: (state: string) => string;
}

const STATE_VARS: Record<string, string> = {
  RUNNING: "--state-running",
  DEGRADED: "--state-degraded",
  STARVED: "--state-starved",
  BLOCKED: "--state-blocked",
  DOWN_UNPLANNED: "--state-down-unplanned",
  DOWN_PLANNED: "--state-down-planned",
  CHANGEOVER: "--state-changeover",
  IDLE_NO_PLAN: "--state-idle-no-plan",
};

function resolveColors(): ChartColors {
    const states: Record<string, string> = {};
    for (const [k, v] of Object.entries(STATE_VARS)) states[k] = cssVar(v);
    return {
      fg: cssVar("--foreground"),
      muted: cssVar("--muted-foreground"),
      border: cssVar("--border"),
      card: cssVar("--card"),
      normal: cssVar("--isa-normal"),
      idle: cssVar("--isa-idle"),
      alarm: cssVar("--isa-alarm"),
      warning: cssVar("--isa-warning"),
      info: cssVar("--isa-info"),
      planned: cssVar("--isa-planned"),
      state: (s) => states[s] ?? states.IDLE_NO_PLAN ?? "#888",
    };
}

/**
 * ISA-101 tokens resolved for the active theme; null until mounted (the `dark` class of <html>
 * is applied by ThemeSync first), recomputed when the theme flips.
 */
export function useChartColors(): ChartColors | null {
  const theme = useTheme();
  const [colors, setColors] = useState<ChartColors | null>(null);
  useEffect(() => {
    const id = requestAnimationFrame(() => setColors(resolveColors()));
    return () => cancelAnimationFrame(id);
  }, [theme]);
  return colors;
}

export function EChart({
  option,
  className,
  height = 280,
  onEvents,
  ariaLabel,
}: {
  option: EChartsOption;
  className?: string;
  height?: number | string;
  onEvents?: Record<string, (params: unknown) => void>;
  ariaLabel?: string;
}) {
  return (
    <div role="img" aria-label={ariaLabel} className={cn("w-full", className)} style={{ height }}>
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        notMerge
        lazyUpdate
        style={{ height: "100%", width: "100%" }}
        opts={{ renderer: "svg" }}
        onEvents={onEvents}
      />
    </div>
  );
}

/** Base text style / tooltip shared by all charts. */
export function baseOption(c: ChartColors): EChartsOption {
  return {
    animationDuration: 300,
    textStyle: { fontFamily: "Inter Variable, ui-sans-serif, system-ui, sans-serif", color: c.muted },
    tooltip: {
      backgroundColor: c.card,
      borderColor: c.border,
      textStyle: { color: c.fg, fontSize: 12 },
      extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,.18); font-variant-numeric: tabular-nums;",
    },
  };
}
