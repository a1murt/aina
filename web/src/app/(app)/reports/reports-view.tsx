"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, FileText, Printer, Sparkles, TriangleAlert } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api/client";
import type { ReportView } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";
import { cn } from "@/lib/utils";

/** /reports — shift reports (FR-LLM, SPEC §11.4): generate, list, «цифры проверены», print. */
export function ReportsView() {
  const t = useTranslations("reports");
  const plant = usePlant();
  const locale = useLocale();
  const qc = useQueryClient();
  const shift = useLive((s) => s.clock?.shift ?? null);
  const shifts = plant.assets?.calendar.shifts ?? [];
  const [day, setDay] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  // Default: the last shift of the previous day — the report is built after the engine closes a shift.
  const prev = shift?.date ? new Date(Date.parse(`${shift.date}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10) : "";
  const date = day ?? prev;
  const sh = code ?? shifts[shifts.length - 1]?.code ?? "";
  const lang = locale === "kk" ? "kk" : "ru";

  const list = useQuery({
    queryKey: ["reports", date, sh, lang],
    queryFn: () => api<ReportView[]>("/api/v1/reports/shift", { query: { date, shift: sh, lang } }),
    enabled: Boolean(date && sh),
  });
  const gen = useMutation({
    mutationFn: () => api<ReportView>("/api/v1/reports/shift", { method: "POST", body: { date, shift: sh, lang } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["reports"] }),
  });
  const reports = list.data ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-4 print:max-w-none print:p-0" data-testid="screen-reports">
      <div className="flex flex-wrap items-end justify-between gap-3 print:hidden">
        <h1 className="text-xl font-semibold">{t("title")}</h1>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col text-xs text-muted-foreground">
            {t("date")}
            <input type="date" value={date} onChange={(e) => setDay(e.target.value)} className="h-9 rounded-md border bg-background px-2 text-sm text-foreground" />
          </label>
          <label className="flex flex-col text-xs text-muted-foreground">
            {t("shift")}
            <select value={sh} onChange={(e) => setCode(e.target.value)} className="h-9 rounded-md border bg-background px-2 text-sm text-foreground">
              {shifts.map((s) => (
                <option key={s.code} value={s.code}>
                  {plant.name(s, s.code)} ({s.start}–{s.end})
                </option>
              ))}
            </select>
          </label>
          <Button onClick={() => gen.mutate()} disabled={gen.isPending || !date || !sh} data-testid="report-generate">
            <Sparkles aria-hidden />
            {gen.isPending ? t("generating") : t("generate")}
          </Button>
          <Button variant="outline" onClick={() => window.print()} disabled={!reports.length}>
            <Printer aria-hidden />
            {t("print")}
          </Button>
        </div>
      </div>
      {gen.error ? (
        <p role="status" className="rounded-md border border-severity-critical/50 bg-severity-critical/10 px-3 py-2 text-sm text-severity-critical print:hidden">
          {gen.error instanceof ApiError ? gen.error.detail || gen.error.title : String(gen.error)}
        </p>
      ) : null}
      <QueryState loading={list.isLoading} error={list.error} compact />
      {list.data && reports.length === 0 ? <p className="text-sm text-muted-foreground print:hidden">{t("empty")}</p> : null}
      {reports.map((r, i) => (
        <Card key={r.id} className={cn(i > 0 && "print:hidden")} data-testid={`report-${r.id}`}>
          <CardHeader
            title={t("heading", { date: plant.fmt.date(r.shift_date), shift: r.shift_code })}
            icon={<FileText aria-hidden className="size-4" />}
            subtitle={t("meta", { ts: plant.fmt.dateTime(r.created_ts), by: r.generated_by, model: r.model ?? "—" })}
            actions={
              <div className="flex gap-1.5">
                {r.numbers_verified ? (
                  <Badge tone="info" data-testid="numbers-verified">
                    <BadgeCheck aria-hidden />
                    {t("verified")}
                  </Badge>
                ) : (
                  <Badge tone="warning">
                    <TriangleAlert aria-hidden />
                    {t("notVerified")}
                  </Badge>
                )}
                {r.translation === "draft" ? <Badge tone="neutral">{t("draft")}</Badge> : null}
              </div>
            }
          />
          <CardBody>
            <div className="text-[15px] leading-relaxed whitespace-pre-line">{r.text}</div>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}
