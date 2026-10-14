"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, FileText, Printer, Sparkles, TriangleAlert } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import { usePlant } from "@/components/plant-context";
import { ErrorDetails, QueryState } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { api } from "@/lib/api/client";
import { useErrorMessage } from "@/lib/api/errors";
import { useClosedShifts } from "@/lib/api/shifts";
import type { ReportView } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/** How many closed shifts the picker offers (the report is built after the engine closes a shift). */
const PICK_SHIFTS = 6;

/** /reports — shift reports (FR-LLM, SPEC §11.4): generate, list, «цифры проверены», print. */
export function ReportsView() {
  const t = useTranslations("reports");
  const plant = usePlant();
  const locale = useLocale();
  const qc = useQueryClient();
  const describe = useErrorMessage();
  const shifts = plant.assets?.calendar.shifts ?? [];
  // The last closed shifts are the shifts with final KPI rows of the past week (no live tail).
  const closed = useClosedShifts("plant");
  const options = (closed.data?.items ?? [])
    .filter((r) => r.shift && r.final !== false)
    .map((r) => `${r.date}/${r.shift}`)
    .reverse()
    .slice(0, PICK_SHIFTS);
  const [picked, setPicked] = useState<string | null>(null);
  const key = picked && options.includes(picked) ? picked : (options[0] ?? "");
  const [date = "", sh = ""] = key.split("/");
  const lang = locale === "kk" ? "kk" : "ru";
  const shiftSpec = (c: string) => shifts.find((s) => s.code === c);

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
            {t("pick")}
            <select
              value={key}
              onChange={(e) => setPicked(e.target.value)}
              disabled={!options.length}
              className="h-9 min-w-56 rounded-md border bg-background px-2 text-sm text-foreground"
              data-testid="report-shift"
            >
              {options.length ? null : <option value="">{!closed.data && !closed.error ? "…" : t("noClosed")}</option>}
              {options.map((o) => {
                const [d = "", c = ""] = o.split("/");
                const spec = shiftSpec(c);
                return (
                  <option key={o} value={o}>
                    {t("pickOption", { date: plant.fmt.date(d), shift: c, start: spec?.start ?? "", end: spec?.end ?? "" })}
                  </option>
                );
              })}
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
        <div role="status" className="rounded-md border border-severity-critical/50 bg-severity-critical/10 px-3 py-2 text-sm print:hidden">
          <p className="text-severity-critical">{describe(gen.error).text}</p>
          <ErrorDetails detail={describe(gen.error).detail} className="text-muted-foreground" />
        </div>
      ) : null}
      <QueryState loading={list.isLoading} error={list.error} compact />
      {list.data && reports.length === 0 ? <p className="text-sm text-muted-foreground print:hidden">{t("empty")}</p> : null}
      {reports.map((r, i) => (
        <Card key={r.id} className={cn(i > 0 && "print:hidden")} data-testid={`report-${r.id}`}>
          <CardHeader
            title={t("heading", { date: plant.fmt.date(r.shift_date), shift: r.shift_code })}
            icon={<FileText aria-hidden className="size-4" />}
            subtitle={t("meta", {
              ts: plant.fmt.dateTime(r.created_ts),
              source: r.generated_by === "llm" && r.model ? t("byModel", { model: r.model }) : t("byTemplate"),
            })}
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
