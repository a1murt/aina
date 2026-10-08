"use client";

import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { createContext, useContext, useMemo, type ReactNode } from "react";

import { api } from "@/lib/api/client";
import type {
  AreaAsset,
  AssetsView,
  BufferAsset,
  DefectCodeView,
  EquipmentAsset,
  LineAsset,
  Named,
  ReasonCategory,
  ReasonView,
} from "@/lib/api/types";
import { makeFmt, type Fmt } from "@/lib/format";

export interface Plant {
  assets: AssetsView | null;
  fmt: Fmt;
  /** Localised name: name_kk in Kazakh when present, else name_ru. */
  name: (o: Named | null | undefined, fallback?: string) => string;
  lines: Record<string, LineAsset & { area: string }>;
  equipment: Record<string, EquipmentAsset>;
  areas: Record<string, AreaAsset>;
  buffers: Record<string, BufferAsset>;
  reasons: Record<string, ReasonView & { category: string }>;
  categories: ReasonCategory[];
  defects: DefectCodeView[];
  productColor: (code: string) => string;
  /** Name of any entity code: line, unit, area, buffer. */
  entityName: (code: string | null | undefined) => string;
}

const PlantCtx = createContext<Plant | null>(null);

export function usePlant(): Plant {
  const ctx = useContext(PlantCtx);
  if (!ctx) throw new Error("usePlant outside PlantProvider");
  return ctx;
}

/** Asset tree, reference books and plant-time formatting for every screen (loaded once). */
export function PlantProvider({ children }: { children: ReactNode }) {
  const locale = useLocale();
  const tu = useTranslations("units");
  const assetsQ = useQuery({ queryKey: ["assets"], queryFn: () => api<AssetsView>("/api/v1/assets"), staleTime: Infinity });
  const reasonsQ = useQuery({
    queryKey: ["config", "reasons"],
    queryFn: () => api<{ categories: ReasonCategory[] }>("/api/v1/config/reasons"),
    staleTime: Infinity,
  });
  const defectsQ = useQuery({
    queryKey: ["config", "defects"],
    queryFn: () => api<{ defects: DefectCodeView[] }>("/api/v1/config/defects"),
    staleTime: Infinity,
  });

  const value = useMemo<Plant>(() => {
    const assets = assetsQ.data ?? null;
    // Until /assets arrives the browser zone is used; screens wait for assets before showing times.
    const tz = assets?.site.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
    const fmt = makeFmt(tz, { bn: tu("bn"), mn: tu("mn"), k: tu("k"), currency: tu("currency") });
    const name = (o: Named | null | undefined, fallback = "—") =>
      !o ? fallback : locale === "kk" && o.name_kk ? o.name_kk : o.name_ru;
    const lines: Plant["lines"] = {};
    const equipment: Plant["equipment"] = {};
    const areas: Plant["areas"] = {};
    for (const a of assets?.areas ?? []) {
      areas[a.code] = a;
      for (const l of a.lines) {
        lines[l.code] = { ...l, area: a.code };
        for (const e of l.equipment) equipment[e.code] = e;
      }
    }
    const buffers = Object.fromEntries((assets?.buffers ?? []).map((b) => [b.code, b]));
    const categories = reasonsQ.data?.categories ?? [];
    const reasons: Plant["reasons"] = {};
    for (const c of categories) for (const r of c.reasons) reasons[r.code] = { ...r, category: c.code };
    const colors = Object.fromEntries((assets?.products ?? []).map((p) => [p.code, p.color_hex]));
    return {
      assets,
      fmt,
      name,
      lines,
      equipment,
      areas,
      buffers,
      reasons,
      categories,
      defects: defectsQ.data?.defects ?? [],
      productColor: (code) => colors[code] ?? "#9AA5B1",
      entityName: (code) => {
        if (!code) return "—";
        const o = lines[code] ?? equipment[code] ?? areas[code] ?? buffers[code];
        return o ? name(o) : code;
      },
    };
  }, [assetsQ.data, reasonsQ.data, defectsQ.data, locale, tu]);

  return <PlantCtx.Provider value={value}>{children}</PlantCtx.Provider>;
}
