"use client";

import { Hourglass } from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { usePlant, type Plant } from "@/components/plant-context";
import { equipmentIcon, storageIcon } from "@/components/live/icons";
import type { AreaAsset, BufferAsset, BufferLive, EquipmentAsset, EquipmentLive, LineLive, UnitEvent } from "@/lib/api/types";
import { onUnit, useLive } from "@/lib/live-store";
import { stateMeta } from "@/lib/states";
import { cn } from "@/lib/utils";

const NODE_W = 80;
const NODE_H = 48;

type Pt = [number, number];

// ------------------------------------------------------------------ geometry of the flow path

interface PathGeo {
  pts: Pt[];
  cum: number[];
  length: number;
}

function pathGeo(points: number[][]): PathGeo {
  const pts = points.map((p) => [p[0] ?? 0, p[1] ?? 0] as Pt);
  const cum = [0];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1] as Pt;
    const b = pts[i] as Pt;
    cum.push((cum[i - 1] ?? 0) + Math.hypot(b[0] - a[0], b[1] - a[1]));
  }
  return { pts, cum, length: cum[cum.length - 1] ?? 0 };
}

function pointAt(g: PathGeo, s: number): Pt {
  const d = Math.max(0, Math.min(g.length, s));
  for (let i = 1; i < g.pts.length; i++) {
    const c1 = g.cum[i] ?? 0;
    if (d <= c1 || i === g.pts.length - 1) {
      const c0 = g.cum[i - 1] ?? 0;
      const a = g.pts[i - 1] as Pt;
      const b = g.pts[i] as Pt;
      const f = c1 > c0 ? (d - c0) / (c1 - c0) : 0;
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    }
  }
  return g.pts[0] ?? [0, 0];
}

/** Arc length where the path first reaches x (the schema flows left → right). */
function arcAtX(g: PathGeo, x: number): number {
  for (let i = 1; i < g.pts.length; i++) {
    const a = g.pts[i - 1] as Pt;
    const b = g.pts[i] as Pt;
    const lo = Math.min(a[0], b[0]);
    const hi = Math.max(a[0], b[0]);
    if (x >= lo && x <= hi) {
      const f = b[0] !== a[0] ? (x - a[0]) / (b[0] - a[0]) : 0;
      return (g.cum[i - 1] ?? 0) + f * ((g.cum[i] ?? 0) - (g.cum[i - 1] ?? 0));
    }
  }
  return x < (g.pts[0]?.[0] ?? 0) ? 0 : g.length;
}

// ------------------------------------------------------------------ FlowPath: moving bodies

interface Body {
  el: SVGCircleElement;
  s0: number;
  s1: number;
  s2: number;
  /** progress 0..1 in plant seconds / ICT */
  p: number;
  ict: number;
}

/**
 * Flow path with bodies (SPEC §13.3): every completed unit (`unit` WS event) runs through its
 * line's section of the path and on to the next buffer; speed = simulation speed (progress is
 * plant seconds / ICT, frozen when paused), colour = product model.
 */
const FlowPath = memo(function FlowPath({ geo, plant }: { geo: PathGeo; plant: Plant }) {
  const layer = useRef<SVGGElement>(null);
  const bodies = useRef<Body[]>([]);
  const sections = useMemo(() => {
    const out: Record<string, { s0: number; s1: number; s2: number; ict: number }> = {};
    const flow = plant.assets?.flow ?? [];
    flow.forEach((code, i) => {
      const line = plant.lines[code];
      const area = line ? plant.areas[line.area] : undefined;
      const r = area?.layout;
      if (!line || !r) return;
      const nextLine = flow[i + 1] ? plant.lines[flow[i + 1] as string] : undefined;
      const nextArea = nextLine ? plant.areas[nextLine.area]?.layout : undefined;
      const s0 = arcAtX(geo, r.x);
      const s1 = arcAtX(geo, r.x + r.w);
      const s2 = nextArea ? arcAtX(geo, nextArea.x) : geo.length;
      out[code] = { s0, s1, s2, ict: line.ict_seconds };
    });
    return out;
  }, [geo, plant.assets, plant.lines, plant.areas]);

  useEffect(() => {
    const g = layer.current;
    if (!g) return;
    const off = onUnit((u: UnitEvent) => {
      const sec = sections[u.line];
      if (!sec || bodies.current.length > 80) return;
      const el = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      el.setAttribute("r", "7");
      el.setAttribute("fill", plant.productColor(u.product));
      el.setAttribute("class", u.result === "pass" ? "body-ok" : "body-defect");
      el.setAttribute("data-product", u.product);
      const [x, y] = pointAt(geo, sec.s0);
      el.setAttribute("cx", String(x));
      el.setAttribute("cy", String(y));
      g.appendChild(el);
      bodies.current.push({ el, s0: sec.s0, s1: sec.s1, s2: sec.s2, p: 0, ict: sec.ict });
    });
    let raf = 0;
    let last = performance.now();
    const frame = (now: number) => {
      const dt = Math.min(0.25, (now - last) / 1000);
      last = now;
      const clock = useLive.getState().clock;
      const speed = clock && !clock.paused ? clock.speed : 0;
      const keep: Body[] = [];
      for (const b of bodies.current) {
        b.p += (dt * speed) / Math.max(b.ict, 1);
        if (b.p >= 1) {
          b.el.remove();
          continue;
        }
        // 80 % of the cycle inside the line, the rest on the way to the next buffer / area
        const s = b.p < 0.8 ? b.s0 + ((b.s1 - b.s0) * b.p) / 0.8 : b.s1 + ((b.s2 - b.s1) * (b.p - 0.8)) / 0.2;
        const [x, y] = pointAt(geo, s);
        b.el.setAttribute("cx", x.toFixed(1));
        b.el.setAttribute("cy", y.toFixed(1));
        keep.push(b);
      }
      bodies.current = keep;
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      off();
      cancelAnimationFrame(raf);
      for (const b of bodies.current) b.el.remove();
      bodies.current = [];
    };
  }, [geo, sections, plant]);

  const d = geo.pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");
  return (
    <g aria-hidden>
      <path d={d} className="fill-none stroke-border" strokeWidth={14} strokeLinecap="round" strokeLinejoin="round" />
      <path d={d} className="flow-dash fill-none stroke-muted-foreground/50" strokeWidth={2} strokeDasharray="6 10" />
      <g ref={layer} data-testid="bodies" />
    </g>
  );
});

// ------------------------------------------------------------------ nodes

function AlarmPulse({ cx, cy, w, h }: { cx: number; cy: number; w: number; h: number }) {
  return (
    <rect
      x={cx - w / 2 - 6}
      y={cy - h / 2 - 6}
      width={w + 12}
      height={h + 12}
      rx={12}
      className="alarm-pulse fill-none stroke-isa-alarm"
      strokeWidth={3}
      aria-hidden
    />
  );
}

function EquipmentNode({
  eq,
  live,
  selected,
  onSelect,
  onHover,
}: {
  eq: EquipmentAsset;
  live: EquipmentLive | undefined;
  selected: boolean;
  onSelect: (code: string) => void;
  onHover: (code: string | null) => void;
}) {
  const t = useTranslations("states");
  const pos = eq.layout;
  if (!pos) return null;
  const state = live?.state ?? "IDLE_NO_PLAN";
  const meta = stateMeta(state);
  const Icon = equipmentIcon(eq.type);
  const x = pos.x - NODE_W / 2;
  const y = pos.y - NODE_H / 2;
  const down = state === "DOWN_UNPLANNED";
  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={`${eq.code}: ${t(state as Parameters<typeof t>[0])}`}
      data-testid={`node-${eq.code}`}
      data-state={state}
      className="cursor-pointer outline-none [&:focus-visible>rect.node]:stroke-ring"
      onClick={() => onSelect(eq.code)}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(eq.code)}
      onMouseEnter={() => onHover(eq.code)}
      onMouseLeave={() => onHover(null)}
    >
      {live?.alarm || down ? <AlarmPulse cx={pos.x} cy={pos.y} w={NODE_W} h={NODE_H} /> : null}
      <rect
        x={x}
        y={y}
        width={NODE_W}
        height={NODE_H}
        rx={8}
        className={cn("node", meta.abnormal ? meta.fill : "fill-card", meta.abnormal ? meta.stroke : "stroke-isa-normal/60")}
        strokeWidth={selected ? 3 : meta.abnormal ? 2.5 : 1.5}
      />
      {/* type icon + code */}
      <Icon x={x + 8} y={y + 8} width={18} height={18} className={meta.abnormal ? meta.text : "text-muted-foreground"} strokeWidth={1.75} />
      <text x={x + 30} y={y + 22} className="fill-foreground text-[11.5px] font-semibold">
        {eq.code}
      </text>
      {/* state icon + short label */}
      <meta.Icon x={x + 8} y={y + 28} width={14} height={14} className={meta.abnormal ? meta.text : "text-muted-foreground"} strokeWidth={2} />
      <text x={x + 26} y={y + 40} className={cn("text-[10px]", meta.abnormal ? meta.textFill : "fill-muted-foreground")}>
        {t(`short.${state}` as Parameters<typeof t>[0])}
      </text>
    </g>
  );
}

function BufferTank({ buf, live, onHover }: { buf: BufferAsset; live: BufferLive | undefined; onHover: (c: string | null) => void }) {
  const t = useTranslations("live.schema");
  const r = buf.layout;
  if (!r) return null;
  const level = live?.level ?? 0;
  const cap = live?.capacity ?? buf.capacity;
  const ratio = cap > 0 ? Math.min(1, Math.max(0, level / cap)) : 0;
  const toFull = live?.minutes_to_full;
  const toEmpty = live?.minutes_to_empty;
  const risk = (toFull != null && toFull <= 30) || (toEmpty != null && toEmpty <= 30) || ratio >= 0.95 || ratio <= 0.05;
  const fillH = (r.h - 4) * ratio;
  return (
    <g data-testid={`buffer-${buf.code}`} onMouseEnter={() => onHover(buf.code)} onMouseLeave={() => onHover(null)}>
      <text x={r.x + r.w / 2} y={r.y - 8} textAnchor="middle" className="fill-muted-foreground text-[11px] font-semibold">
        {buf.code}
      </text>
      <rect x={r.x} y={r.y} width={r.w} height={r.h} rx={6} className={cn("fill-card", risk ? "stroke-isa-warning" : "stroke-isa-normal/60")} strokeWidth={risk ? 2 : 1.5} />
      <rect
        x={r.x + 2}
        y={r.y + r.h - 2 - fillH}
        width={r.w - 4}
        height={fillH}
        rx={4}
        className={cn("transition-all duration-700", risk ? "fill-isa-warning/70" : "fill-isa-normal/45")}
      />
      <text x={r.x + r.w / 2} y={r.y + r.h / 2 + 5} textAnchor="middle" className="fill-foreground text-[13px] font-semibold tabular-nums">
        {level}/{cap}
      </text>
      <text x={r.x + r.w / 2} y={r.y + r.h + 16} textAnchor="middle" className={cn("text-[10px] tabular-nums", risk ? "fill-foreground font-semibold" : "fill-muted-foreground")}>
        {toFull != null ? t("toFull", { min: Math.round(toFull) }) : toEmpty != null ? t("toEmpty", { min: Math.round(toEmpty) }) : t("steady")}
      </text>
    </g>
  );
}

function AreaBlock({
  area,
  line,
  isBottleneck,
  share,
  children,
}: {
  area: AreaAsset;
  line: LineLive | undefined;
  isBottleneck: boolean;
  share: number | null;
  children?: ReactNode;
}) {
  const t = useTranslations("live.schema");
  const ts = useTranslations("states");
  const { name, fmt } = usePlant();
  const r = area.layout;
  if (!r) return null;
  const storage = area.kind === "storage";
  const state = line?.state ?? (storage ? null : "IDLE_NO_PLAN");
  const meta = stateMeta(state);
  const narrow = r.w < 200;
  const SIcon = storageIcon(area.code);
  return (
    <g data-testid={`area-${area.code}`} data-state={state ?? undefined}>
      <rect
        x={r.x}
        y={r.y}
        width={r.w}
        height={r.h}
        rx={12}
        className={cn(
          storage ? "fill-muted/40 stroke-border" : "fill-card/60",
          !storage && (meta.abnormal ? meta.stroke : "stroke-border"),
        )}
        strokeWidth={!storage && meta.abnormal ? 2.5 : 1.25}
        strokeDasharray={storage ? "5 5" : undefined}
      />
      {storage ? (
        <>
          <SIcon x={r.x + r.w / 2 - 14} y={r.y + r.h / 2 - 34} width={28} height={28} className="text-muted-foreground" strokeWidth={1.5} />
          <text x={r.x + r.w / 2} y={r.y + r.h / 2 + 12} textAnchor="middle" className="fill-muted-foreground text-[12px] font-medium">
            {area.code}
          </text>
        </>
      ) : (
        <>
          <text x={r.x + 12} y={r.y + 22} className={cn("fill-foreground font-semibold", narrow ? "text-[12px]" : "text-[14px]")}>
            {name(area)}
          </text>
          {state ? (
            <g>
              <title>{ts(state as Parameters<typeof ts>[0])}</title>
              <meta.Icon x={r.x + r.w - (narrow ? 22 : 112)} y={r.y + 9} width={16} height={16} className={meta.abnormal ? meta.text : "text-muted-foreground"} strokeWidth={2} />
              {narrow ? null : (
                <text x={r.x + r.w - 92} y={r.y + 22} className={cn("text-[12px] font-semibold", meta.abnormal ? meta.textFill : "fill-muted-foreground")}>
                  {ts(`short.${state}` as Parameters<typeof ts>[0])}
                </text>
              )}
            </g>
          ) : null}
          {/* KPI strip */}
          <line x1={r.x + 12} x2={r.x + r.w - 12} y1={r.y + r.h - 34} y2={r.y + r.h - 34} className="stroke-border" />
          <text x={r.x + 14} y={r.y + r.h - 13} className="fill-muted-foreground text-[11px]">
            {t("oee")}
            <tspan className="fill-foreground text-[14px] font-semibold tabular-nums" dx={6}>
              {fmt.pct(line?.oee ?? null)}
            </tspan>
          </text>
          <text x={r.x + r.w - 14} y={r.y + r.h - 13} textAnchor="end" className="fill-muted-foreground text-[11px] tabular-nums">
            <tspan className="fill-foreground text-[14px] font-semibold">{fmt.int(line?.gq ?? null)}</tspan>
            <tspan dx={3}>/ {fmt.int(line?.plan_to_now ?? null)}</tspan>
          </text>
        </>
      )}
      {isBottleneck ? (
        <g data-testid={`bottleneck-${area.code}`}>
          <rect x={r.x + r.w / 2 - 92} y={r.y - 25} width={184} height={24} rx={12} className="fill-severity-info" />
          <Hourglass x={r.x + r.w / 2 - 82} y={r.y - 20} width={14} height={14} className="text-white" strokeWidth={2.25} />
          <text x={r.x + r.w / 2 - 62} y={r.y - 8.5} className="fill-white text-[11.5px] font-semibold">
            {share != null ? t("bottleneckShare", { share: Math.round(share * 100) }) : t("bottleneck")}
          </text>
        </g>
      ) : null}
      {children}
    </g>
  );
}

// ------------------------------------------------------------------ schema

export function PlantSchema({
  selected,
  onSelect,
  className,
}: {
  selected: string | null;
  onSelect: (code: string) => void;
  className?: string;
}) {
  const plant = usePlant();
  const t = useTranslations("live.schema");
  const ts = useTranslations("states");
  const equipment = useLive((s) => s.equipment);
  const lines = useLive((s) => s.lines);
  const buffers = useLive((s) => s.buffers);
  const bottleneck = useLive((s) => s.bottleneck);
  const [hover, setHover] = useState<string | null>(null);
  const assets = plant.assets;
  const geo = useMemo(() => pathGeo(assets?.layout.flow_path ?? []), [assets]);
  if (!assets) return <div className={cn("aspect-[16/5.6] animate-pulse rounded-xl bg-muted", className)} />;
  // crop the configured viewBox to the drawn content (areas + buffers + bottleneck badge room)
  const vb = assets.layout.viewbox;
  const rects = [...assets.areas.map((a) => a.layout), ...assets.buffers.map((b) => b.layout)].filter((r): r is NonNullable<typeof r> => Boolean(r));
  const minX = Math.max(vb[0] ?? 0, Math.min(...rects.map((r) => r.x)) - 12);
  const maxX = Math.min((vb[0] ?? 0) + (vb[2] ?? 1600), Math.max(...rects.map((r) => r.x + r.w)) + 12);
  const minY = Math.max(vb[1] ?? 0, Math.min(...rects.map((r) => r.y)) - 30);
  const maxY = Math.min((vb[1] ?? 0) + (vb[3] ?? 560), Math.max(...rects.map((r) => r.y + r.h)) + 14);
  const [vx, vy, vw, vh] = rects.length ? [minX, minY, maxX - minX, maxY - minY] : [vb[0] ?? 0, vb[1] ?? 0, vb[2] ?? 1600, vb[3] ?? 560];
  const bnLine = bottleneck?.current ?? null;
  const bnArea = bnLine ? plant.lines[bnLine]?.area : undefined;
  const bnShare = bnLine ? (bottleneck?.shift_shares?.[bnLine]?.sole ?? null) : null;

  // hover card
  let tip: { x: number; y: number; title: string; lines: string[] } | null = null;
  if (hover) {
    const eq = plant.equipment[hover];
    const buf = plant.buffers[hover];
    if (eq?.layout) {
      const lv = equipment[hover];
      const st = (lv?.state ?? "IDLE_NO_PLAN") as Parameters<typeof ts>[0];
      tip = {
        x: eq.layout.x,
        y: eq.layout.y - NODE_H / 2 - 8,
        title: `${eq.code} · ${plant.name(eq)}`,
        lines: [
          `${ts(st)}${lv?.since ? ` · ${t("since", { time: plant.fmt.time(lv.since) })}` : ""}`,
          lv?.reason_code ? plant.name(plant.reasons[lv.reason_code], lv.reason_code) : "",
          t("clickForDetails"),
        ].filter(Boolean),
      };
    } else if (buf?.layout) {
      const lv = buffers[hover];
      tip = {
        x: buf.layout.x + buf.layout.w / 2,
        y: buf.layout.y - 22,
        title: `${buf.code} · ${plant.name(buf)}`,
        lines: [
          t("level", { level: lv?.level ?? 0, capacity: lv?.capacity ?? buf.capacity }),
          lv?.rate_per_min != null ? t("rate", { rate: plant.fmt.num(lv.rate_per_min * 60, 1) }) : "",
        ].filter(Boolean),
      };
    }
  }

  return (
    <div className={cn("relative w-full select-none", className)}>
      <svg viewBox={`${vx} ${vy} ${vw} ${vh}`} className="block h-auto w-full" role="group" aria-label={t("label")} data-testid="plant-schema">
        <defs>
          <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M40 0H0V40" className="fill-none stroke-border/50" strokeWidth={0.75} />
          </pattern>
        </defs>
        <rect x={vx} y={vy} width={vw} height={vh} fill="url(#grid)" />
        {assets.areas.map((a) => {
          const lc = a.lines[0]?.code;
          return <AreaBlock key={a.code} area={a} line={lc ? lines[lc] : undefined} isBottleneck={bnArea === a.code} share={bnShare} />;
        })}
        <FlowPath geo={geo} plant={plant} />
        {assets.buffers.map((b) => (
          <BufferTank key={b.code} buf={b} live={buffers[b.code]} onHover={setHover} />
        ))}
        {Object.values(plant.equipment).map((eq) => (
          <EquipmentNode key={eq.code} eq={eq} live={equipment[eq.code]} selected={selected === eq.code} onSelect={onSelect} onHover={setHover} />
        ))}
      </svg>
      {tip ? (
        <div
          role="tooltip"
          className="pointer-events-none absolute z-10 max-w-64 -translate-x-1/2 -translate-y-full rounded-md border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-lg"
          style={{ left: `${((tip.x - vx) / vw) * 100}%`, top: `${((tip.y - vy) / vh) * 100}%` }}
        >
          <div className="font-semibold">{tip.title}</div>
          {tip.lines.map((l) => (
            <div key={l} className="text-muted-foreground">
              {l}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
