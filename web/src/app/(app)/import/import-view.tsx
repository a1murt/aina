"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleX, Download, FileSpreadsheet, History, Loader2, RefreshCw, Table2, Timer, UploadCloud } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRef, useState, type ChangeEvent, type DragEvent, type ReactNode } from "react";

import { usePlant, type Plant } from "@/components/plant-context";
import { ErrorDetails } from "@/components/query-state";
import { SeverityBadge, SeverityIcon } from "@/components/state-badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ApiError, downloadFile, getToken, NetworkError } from "@/lib/api/client";
import { useErrorMessage } from "@/lib/api/errors";
import { useRole } from "@/lib/api/use-role";
import type { ImportReport, Severity } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const ACCEPT = ".docx,.xlsx,.csv,.zip";
/** Roles of POST /api/v1/import?reanalyse=true (the roles of the import itself). */
const REANALYSE_ROLES = ["admin", "director"];
/** Statuses of an API build without `reanalyse`: the button hides and the stored result stays. */
const REANALYSE_UNSUPPORTED = [404, 405, 422];

type Phase =
  | { kind: "idle" }
  | { kind: "upload"; pct: number; reanalyse: boolean }
  | { kind: "analyse"; reanalyse: boolean }
  | { kind: "error"; title: string; detail: string | null; problems: string[] };

/** multipart upload with progress (fetch has no upload progress). */
function upload(files: File[], reanalyse: boolean, onProgress: (pct: number) => void): Promise<{ status: number; body: unknown }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", reanalyse ? "/api/v1/import?reanalyse=true" : "/api/v1/import");
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new NetworkError("upload failed"));
    xhr.send(form);
  });
}

/** Problem document of a failed upload as an {@link ApiError} (same shape as `api()` throws). */
function problemOf(status: number, body: unknown): ApiError {
  const p = (body ?? {}) as { type?: string; title?: string; detail?: string };
  const slug = (typeof p.type === "string" && p.type.split("/").pop()) || `http-${status}`;
  return new ApiError(status, slug, p.title ?? "", typeof p.detail === "string" ? p.detail : "");
}

/** Stored duration of the analysis (contract (a)); older API builds do not send it. */
function analysisMs(r: ImportReport): number | null {
  const v = r.analysis_ms ?? r.job.analysis_ms;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** /import — drag-and-drop of the case files and the «audit in 10 seconds» report (US-1, §7.4). */
export function ImportView() {
  const t = useTranslations("import");
  const qc = useQueryClient();
  const describe = useErrorMessage();
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [report, setReport] = useState<ImportReport | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [reanalyseOff, setReanalyseOff] = useState(false);
  const busy = phase.kind === "upload" || phase.kind === "analyse";
  const role = useRole() ?? "";

  async function start(list: FileList | File[] | null, reanalyse = false) {
    const picked = Array.from(list ?? []);
    if (!picked.length || busy) return;
    const t0 = performance.now();
    // A re-check keeps the audit on screen until the new one arrives: on any failure it stays,
    // with the error shown next to it (the first demo step must never go blank).
    if (!reanalyse) setReport(null);
    setPhase({ kind: "upload", pct: 0, reanalyse });
    // the audit stays on screen; the reason goes under «Подробнее»
    const failedRecheck = (why: string, detail: string | null): Phase => ({
      kind: "error",
      title: t("reanalyseFailed"),
      detail: [why, detail].filter(Boolean).join(" · "),
      problems: [],
    });
    const keepPrevious = () => {
      setReanalyseOff(true);
      setPhase({ kind: "idle" });
    };
    try {
      const res = await upload(picked, reanalyse, (pct) => setPhase(pct >= 100 ? { kind: "analyse", reanalyse } : { kind: "upload", pct, reanalyse }));
      if (res.status === 200 || res.status === 201) {
        const body = res.body as ImportReport;
        // An API build without `reanalyse` ignores the parameter and returns the stored result.
        if (reanalyse && body?.reanalysed !== true) return keepPrevious();
        setFiles(picked);
        setReport(body);
        setElapsed((performance.now() - t0) / 1000);
        setPhase({ kind: "idle" });
        if (!reanalyse) void qc.invalidateQueries();
        return;
      }
      if (reanalyse && REANALYSE_UNSUPPORTED.includes(res.status)) return keepPrevious();
      const err = problemOf(res.status, res.body);
      const e = describe(err);
      const raw = (res.body ?? {}) as { problems?: Array<{ msg?: string; message?: string } | string> };
      const problems = (raw.problems ?? []).map((x) => (typeof x === "string" ? x : (x.msg ?? x.message ?? JSON.stringify(x))));
      const title = res.status === 403 ? t("forbidden") : e.text;
      setPhase(reanalyse ? failedRecheck(title, e.detail) : { kind: "error", title, detail: e.detail, problems });
    } catch (err) {
      const e = describe(err instanceof NetworkError ? err : new NetworkError(String(err)));
      setPhase(reanalyse ? failedRecheck(e.text, e.detail) : { kind: "error", title: e.text, detail: null, problems: [] });
    }
  }

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    // Reset so that picking the same file again fires `change` (DEMO-9).
    e.target.value = "";
    void start(picked);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    void start(e.dataTransfer.files);
  };

  const canReanalyse = !!report && files.length > 0 && !reanalyseOff && REANALYSE_ROLES.includes(role) && analysisMs(report) !== null;
  const reanalysing = (phase.kind === "upload" || phase.kind === "analyse") && phase.reanalyse;
  return (
    <div className="mx-auto flex max-w-[1400px] flex-col gap-4 p-4" data-testid="import">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>
        <Button variant="outline" onClick={() => void downloadFile("/api/v1/import/template.xlsx", "qost-import-template.xlsx")}>
          <Download aria-hidden />
          {t("template")}
        </Button>
      </div>

      <div
        role="button"
        tabIndex={0}
        aria-label={t("drop")}
        aria-busy={busy}
        data-testid="dropzone"
        onClick={() => !busy && input.current?.click()}
        onKeyDown={(e) => !busy && (e.key === "Enter" || e.key === " ") && input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed bg-card px-6 text-center transition-colors",
          report ? "py-6" : "py-14",
          drag ? "border-primary bg-accent" : "border-border hover:border-muted-foreground/50",
        )}
      >
        {busy ? <Loader2 aria-hidden className="size-10 animate-spin text-muted-foreground" /> : <UploadCloud aria-hidden className="size-10 text-muted-foreground" />}
        <div>
          <p className="text-lg font-medium">
            {busy ? (phase.kind === "analyse" ? (reanalysing ? t("reanalysing") : t("analysing")) : t("uploading")) : t("drop")}
          </p>
          <p className="text-sm text-muted-foreground">{t("formats")}</p>
        </div>
        {phase.kind === "upload" || phase.kind === "analyse" ? (
          <div className="h-2 w-full max-w-md overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuenow={phase.kind === "upload" ? phase.pct : 100}>
            <div className={cn("h-full rounded-full bg-primary transition-all", phase.kind === "analyse" && "animate-pulse")} style={{ width: `${phase.kind === "upload" ? phase.pct : 100}%` }} />
          </div>
        ) : null}
        <input ref={input} type="file" accept={ACCEPT} multiple hidden data-testid="file-input" onChange={onPick} />
      </div>

      {phase.kind === "error" ? (
        <div role="alert" className="rounded-lg border border-severity-critical/50 bg-severity-critical/10 px-4 py-3 text-sm">
          <p className="flex items-center gap-2 font-medium text-severity-critical">
            <CircleX aria-hidden className="size-4" />
            {phase.title}
          </p>
          {phase.problems.length ? (
            <ul className="mt-2 list-disc pl-6">
              {phase.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : null}
          <ErrorDetails detail={phase.detail} className="mt-1 text-muted-foreground" />
        </div>
      ) : null}

      {report ? (
        <Report
          report={report}
          elapsed={elapsed}
          reanalyse={
            canReanalyse ? (
              <Button variant="outline" size="sm" onClick={() => void start(files, true)} disabled={busy} title={t("reanalyseHint")} data-testid="import-reanalyse">
                <RefreshCw aria-hidden />
                {t("reanalyse")}
              </Button>
            ) : null
          }
        />
      ) : null}
      {!report && phase.kind === "idle" ? <p className="text-center text-sm text-muted-foreground">{t("hint")}</p> : null}
    </div>
  );
}

// ------------------------------------------------------------------ report

/** Configured value of a constraint the report was checked against (rules.yaml via the API). */
function limitOf(report: ImportReport, key: string): number | null {
  const c = report.constraint_checks.find((x) => x.key === key);
  const v = c?.configured ?? c?.value ?? report.constraints_parsed[key];
  return typeof v === "number" ? v : null;
}

/** Percent of a ratio with as many digits as it needs (0.02 → «2 %», 0.025 → «2,5 %»). */
function pctAuto(plant: Plant, r: number | null): string {
  if (r == null) return plant.fmt.pct(null);
  const p = r * 100;
  return plant.fmt.pct(r, Math.abs(p - Math.round(p)) < 1e-9 ? 0 : Math.abs(p * 10 - Math.round(p * 10)) < 1e-9 ? 1 : 2);
}

/** Constraints that are ratios (shown in %); the others are counts, hours and minutes. */
const RATIO_CONSTRAINTS = new Set(["oee_target", "defect_rate_limit"]);

const n = (v: unknown) => (typeof v === "number" ? v : Number(v));

function useExplain(report: ImportReport) {
  const ta = useTranslations("import.alertText");
  const td = useTranslations("import.dqText");
  const plant = usePlant();
  const alert = (a: ImportReport["alerts"][number]): string => {
    const entity = plant.entityName(a.entity);
    const date = a.date ? plant.fmt.date(a.date) : "";
    switch (a.rule_id) {
      case "AL-Q1":
        return ta("AL-Q1", { entity, date, value: plant.fmt.pct(n(a.value), 2), limit: pctAuto(plant, limitOf(report, "defect_rate_limit")) });
      case "AL-O1":
      case "AL-O2":
        return ta(a.rule_id, { entity, date, value: plant.fmt.pct(n(a.value), 1), limit: pctAuto(plant, limitOf(report, "oee_target")) });
      case "AL-D1":
        return ta("AL-D1", { entity, date, value: plant.fmt.int(n(a.value)), limit: plant.fmt.int(limitOf(report, "critical_downtime_limit_min_per_day")) });
      case "AL-Q3": {
        const v = (a.value ?? {}) as Record<string, number>;
        return ta("AL-Q3", { list: Object.entries(v).map(([k, x]) => `${plant.entityName(k)} ${plant.fmt.pct(x, 2)}`).join(", ") });
      }
      default:
        return ta("other", { rule: a.rule_id, entity, date });
    }
  };
  const dq = (d: ImportReport["data_quality_issues"][number]): string => {
    const x = d.details;
    const entity = d.entity.includes("->") ? d.entity.split("->").map((c) => plant.entityName(c)).join(" → ") : plant.entityName(d.entity);
    const date = d.date ? plant.fmt.date(d.date) : "";
    switch (d.rule_id) {
      case "DQ-01":
        return td("DQ-01", { entity, date, reported: plant.fmt.num(n(x.reported_load_pct), 1), computed: plant.fmt.num(n(x.computed_availability_pct), 1), diff: plant.fmt.num(n(x.diff_pp), 1) });
      case "DQ-02":
        return td(x.direction === "unlogged_loss" ? "DQ-02-unlogged" : "DQ-02-excess", {
          entity,
          date,
          logged: plant.fmt.int(n(x.logged_downtime_min)),
          lost: plant.fmt.int(n(x.lost_time_min)),
          diff: plant.fmt.int(Math.abs(n(x.diff_min))),
        });
      case "DQ-03":
        return td("DQ-03", { n: n(x.records_without_shift) });
      case "DQ-04":
        return td("DQ-04", { entity, up: plant.fmt.int(n(x.upstream_produced)), down: plant.fmt.int(n(x.downstream_produced)), buf: plant.fmt.signed(n(x.buffer_change_units), 0) });
      case "DQ-05":
        return td("DQ-05", { target: plant.fmt.int(n(x.plant_target)), line: plant.fmt.int(n(x.line_model_plan)), gap: plant.fmt.signed(n(x.gap), 0) });
      default:
        return td("other", { rule: d.rule_id, entity, date });
    }
  };
  return { alert, dq };
}

interface Headline {
  key: string;
  severity: Severity;
  label: string;
  value: string;
  note: string;
}

const SEV_RANK: Record<Severity, number> = { critical: 0, warning: 1, info: 2 };
const worstSeverity = (xs: Severity[]): Severity => xs.reduce<Severity>((w, x) => (SEV_RANK[x] < SEV_RANK[w] ? x : w), "info");

/** The four numbers a director remembers (VIS-11): worst defect, critical downtime, log mismatches, plan gap. */
function useHeadlines(report: ImportReport): Headline[] {
  const t = useTranslations("import.hero");
  const plant = usePlant();
  const worst = (rule: string) =>
    report.alerts
      .filter((a) => a.rule_id === rule && typeof a.value === "number")
      .sort((a, b) => n(b.value) - n(a.value))[0];
  const out: Headline[] = [];
  const defect = worst("AL-Q1");
  if (defect) {
    out.push({
      key: "defect",
      severity: defect.severity,
      label: t("defect", { entity: plant.entityName(defect.entity) }),
      value: plant.fmt.pct(n(defect.value), 2),
      note: t("defectNote", { limit: pctAuto(plant, limitOf(report, "defect_rate_limit")), date: plant.fmt.dayMonth(defect.date) }),
    });
  }
  const down = worst("AL-D1");
  if (down) {
    out.push({
      key: "downtime",
      severity: down.severity,
      label: t("downtime", { entity: plant.entityName(down.entity) }),
      value: t("downtimeValue", { value: plant.fmt.int(n(down.value)), limit: plant.fmt.int(limitOf(report, "critical_downtime_limit_min_per_day")) }),
      note: t("downtimeNote", { date: plant.fmt.dayMonth(down.date) }),
    });
  }
  const journal = report.data_quality_issues.filter((d) => d.rule_id === "DQ-02");
  if (journal.length) {
    out.push({
      key: "journal",
      severity: worstSeverity(journal.map((d) => d.severity)),
      label: t("journal"),
      value: t("journalValue", { n: journal.length }),
      note: t("journalNote", { min: plant.fmt.int(journal.reduce((s, d) => s + Math.abs(n(d.details.diff_min) || 0), 0)) }),
    });
  }
  if (report.plan.gap) {
    const dq05 = report.data_quality_issues.find((d) => d.rule_id === "DQ-05");
    out.push({
      key: "plan",
      severity: dq05?.severity ?? "warning",
      label: t("plan"),
      value: t("planValue", { gap: plant.fmt.signed(report.plan.gap, 0).replace("-", "\u2212") }),
      note: t("planNote", { line: plant.fmt.int(report.plan.line_model_total), target: plant.fmt.int(report.plan.plant_target) }),
    });
  }
  return out;
}

const SEV_BORDER: Record<Severity, string> = {
  critical: "border-l-severity-critical",
  warning: "border-l-severity-warning",
  info: "border-l-severity-info",
};

function Headlines({ items }: { items: Headline[] }) {
  const t = useTranslations("import.hero");
  if (!items.length) return null;
  return (
    <section aria-label={t("title")} className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="import-headlines">
      {items.map((h) => (
        <div key={h.key} className={cn("rounded-lg border border-l-4 bg-card px-4 py-3", SEV_BORDER[h.severity])} data-testid={`import-headline-${h.key}`}>
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <SeverityIcon severity={h.severity} className="size-3.5 shrink-0" />
            <span className="line-clamp-2 min-w-0" title={h.label}>
              {h.label}
            </span>
          </p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">{h.value}</p>
          <p className="text-xs text-muted-foreground">{h.note}</p>
        </div>
      ))}
    </section>
  );
}

/** «Аудит за N с» for a fresh parse; «Результат от ДД.ММ ЧЧ:ММ, разбор N с» for a stored one. */
function ResultLine({ report, elapsed }: { report: ImportReport; elapsed: number | null }) {
  const t = useTranslations("import.report");
  const plant = usePlant();
  const ms = analysisMs(report);
  // A parse takes milliseconds: under a second the duration is shown in ms, never as «0,01 с».
  const dur = (x: number) => (x < 1000 ? t("durMs", { n: plant.fmt.num(Math.max(1, Math.round(x)), 0) }) : t("durS", { n: plant.fmt.num(x / 1000, 1) }));
  const fresh = report.reanalysed === true || report.job.created === true;
  if (fresh) {
    const d = ms ?? (elapsed != null ? elapsed * 1000 : null);
    if (d == null) return null;
    return (
      <span className="ml-auto flex items-center gap-1.5 font-semibold tabular-nums" data-testid="import-elapsed">
        <Timer aria-hidden className="size-4" />
        {t(report.reanalysed ? "reanalysed" : "elapsed", { d: dur(d) })}
      </span>
    );
  }
  const when = plant.fmt.dateShort(report.job.created_ts);
  return (
    <span className="ml-auto flex items-center gap-1.5 text-muted-foreground tabular-nums" title={t("cachedHint")} data-testid="import-cached">
      <History aria-hidden className="size-4" />
      {ms != null ? t("cachedWithTime", { when, d: dur(ms) }) : t("cached", { when })}
    </span>
  );
}

function Report({ report, elapsed, reanalyse }: { report: ImportReport; elapsed: number | null; reanalyse: ReactNode }) {
  const t = useTranslations("import.report");
  const tc = useTranslations("import.constraint");
  const plant = usePlant();
  const explain = useExplain(report);
  const headlines = useHeadlines(report);
  const alerts = [...report.alerts].sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity]);
  const dq = [...report.data_quality_issues].sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity]);
  const lineToArea = new Map(report.shift_reports.map((r) => [r.line, r.area]));
  return (
    <div className="flex flex-col gap-4" data-testid="import-report">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border bg-card px-4 py-3 text-sm">
        <span className="flex items-center gap-2 font-medium">
          <CheckCircle2 aria-hidden className="size-4 text-isa-normal" />
          {report.job.filename}
        </span>
        <span className="text-muted-foreground">
          {t("period", { from: plant.fmt.date(report.meta.period.from), to: plant.fmt.date(report.meta.period.to) })}
        </span>
        <span className="text-muted-foreground">{t("kind", { kind: report.meta.kind.toUpperCase() })}</span>
        <ResultLine report={report} elapsed={elapsed} />
        {reanalyse}
      </div>

      <Headlines items={headlines} />

      {/* findings: the «audit in 10 seconds» */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title={t("alerts", { n: alerts.length })} subtitle={t("alertsHint")} />
          <ul className="border-t" data-testid="import-alerts">
            {alerts.map((a, i) => (
              <li key={`${a.rule_id}-${a.entity}-${i}`} className="flex gap-3 border-b px-4 py-2.5 last:border-b-0" data-testid="import-alert">
                <SeverityIcon severity={a.severity} className="mt-0.5" />
                <div className="min-w-0 flex-1 text-sm">
                  <p>{explain.alert(a)}</p>
                  <p className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                    <code>{a.rule_id}</code>
                    <SeverityBadge severity={a.severity} />
                  </p>
                </div>
              </li>
            ))}
          </ul>
        </Card>
        <Card>
          <CardHeader title={t("dq", { n: dq.length })} subtitle={t("dqHint")} />
          <ul className="border-t" data-testid="import-dq">
            {dq.map((d, i) => (
              <li key={`${d.rule_id}-${d.entity}-${i}`} className="flex gap-3 border-b px-4 py-2.5 last:border-b-0" data-testid="import-dq-item">
                <SeverityIcon severity={d.severity} className="mt-0.5" />
                <div className="min-w-0 flex-1 text-sm">
                  <p>{explain.dq(d)}</p>
                  <p className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                    <code>{d.rule_id}</code>
                    <SeverityBadge severity={d.severity} />
                  </p>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      {/* KPI per shift */}
      <Card>
        <CardHeader title={t("kpi")} subtitle={t("kpiHint")} />
        <CardBody className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm" data-testid="import-kpi">
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                {["date", "line", "produced", "good", "lost", "availability", "effectiveness", "quality", "oee", "defects"].map((h) => (
                  <th key={h} className={cn("py-2 font-medium", h === "date" || h === "line" ? "text-left" : "text-right")}>
                    {t(`col.${h}` as "col.date")}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {report.shift_reports.map((r) => (
                <tr key={`${r.date}-${r.line}`} className="border-b last:border-b-0">
                  <td className="py-2 tabular-nums">
                    {plant.fmt.date(r.date)} {r.shift ? <span className="text-muted-foreground">· {r.shift}</span> : null}
                  </td>
                  <td className="py-2">{plant.name(plant.lines[r.line], r.line)}</td>
                  <td className="py-2 text-right">{plant.fmt.int(r.produced_qty)}</td>
                  <td className="py-2 text-right">{plant.fmt.int(r.good_qty)}</td>
                  <td className="py-2 text-right">{plant.fmt.int(r.lost_min)}</td>
                  <td className="py-2 text-right">{plant.fmt.pct(r.availability, 1)}</td>
                  <td className="py-2 text-right">{plant.fmt.pct(r.effectiveness, 1)}</td>
                  <td className="py-2 text-right">{plant.fmt.pct(r.quality_ratio, 1)}</td>
                  <td className={cn("py-2 text-right font-semibold", r.oee < 0.85 && "text-severity-warning")}>{plant.fmt.pct(r.oee, 1)}</td>
                  <td className={cn("py-2 text-right", r.defect_rate > 0.02 && "font-semibold text-severity-critical")}>{plant.fmt.pct(r.defect_rate, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title={t("plan")} />
          <CardBody>
            <dl className="grid grid-cols-2 gap-3 text-sm tabular-nums">
              <div>
                <dt className="text-xs text-muted-foreground">{t("planTarget")}</dt>
                <dd className="text-2xl font-semibold">{plant.fmt.int(report.plan.plant_target)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">{t("planLines")}</dt>
                <dd className="text-2xl font-semibold">
                  {plant.fmt.int(report.plan.line_model_total)} <span className="text-base text-severity-critical">({plant.fmt.signed(report.plan.gap, 0)})</span>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">{t("naive")}</dt>
                <dd className="text-lg font-semibold">{plant.fmt.int(report.plan.naive_month_projection)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">{t("rates")}</dt>
                <dd className="text-sm">
                  {t("ratesText", {
                    target: plant.fmt.num(report.plan.required_rate_per_shift_target, 1),
                    line: plant.fmt.num(report.plan.required_rate_per_shift_line_plan, 1),
                    actual: plant.fmt.num(report.plan.mean_sustainable_rate_per_shift, 1),
                  })}
                </dd>
              </div>
            </dl>
            {report.bottleneck_aggregate.overall ? (
              <p className="mt-3 text-sm text-muted-foreground">
                {t(report.bottleneck_aggregate.shifting ? "bottleneckShifting" : "bottleneck", {
                  area: plant.entityName(report.bottleneck_aggregate.overall),
                  days: Object.entries(report.bottleneck_aggregate.by_day)
                    .map(([d, a]) => `${plant.fmt.dayMonth(d)} — ${plant.entityName(a)}`)
                    .join(", "),
                })}
              </p>
            ) : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("constraints")} subtitle={t("constraintsHint")} />
          <CardBody>
            <table className="w-full text-sm tabular-nums" data-testid="import-constraints">
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th className="py-1.5 text-left font-medium">{t("constraintCol")}</th>
                  <th className="py-1.5 text-right font-medium">{t("fromText")}</th>
                  <th className="py-1.5 text-right font-medium">{t("inConfig")}</th>
                  <th className="py-1.5" />
                </tr>
              </thead>
              <tbody>
                {report.constraint_checks.map((c) => (
                  <tr key={c.key} className="border-b last:border-b-0">
                    <td className="py-1.5">{tc(c.key as "oee_target")}</td>
                    <td className="py-1.5 text-right">{RATIO_CONSTRAINTS.has(c.key) ? pctAuto(plant, c.value) : plant.fmt.num(c.value, 1)}</td>
                    <td className="py-1.5 text-right">{RATIO_CONSTRAINTS.has(c.key) ? pctAuto(plant, c.configured) : plant.fmt.num(c.configured, 1)}</td>
                    <td className="py-1.5 pl-2 text-right">
                      {c.matches ? (
                        <CheckCircle2 aria-label={t("matches")} className="ml-auto size-4 text-muted-foreground" />
                      ) : (
                        <CircleX aria-label={t("differs")} className="ml-auto size-4 text-severity-critical" />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardBody>
        </Card>
      </div>

      {/* recognised tables and name → code mapping */}
      <Card>
        <CardHeader title={t("tables")} icon={<Table2 aria-hidden className="size-4" />} subtitle={t("tablesHint")} />
        <CardBody className="grid gap-4 lg:grid-cols-3">
          <MappingTable
            title={t("tableLines", { n: report.shift_reports.length })}
            rows={Array.from(new Set(report.shift_reports.map((r) => r.line))).map((line) => [plant.name(plant.areas[lineToArea.get(line) ?? ""], line), line])}
          />
          <MappingTable
            title={t("tableDowntime", { n: report.downtime.length })}
            rows={report.downtime.map((d) => [`${d.reason_text_src}${d.equipment ? ` · ${d.equipment}` : ""}`, `${d.reason_code} · ${plant.fmt.int(d.duration_min)} ${t("min")}`])}
          />
          <MappingTable title={t("tablePlan", { n: report.plan.rows.length })} rows={report.plan.rows.map((r) => [r.model_src, `${r.model} · ${plant.fmt.int(r.qty)}`])} />
        </CardBody>
      </Card>
    </div>
  );
}

function MappingTable({ title, rows }: { title: string; rows: Array<[string, string]> }) {
  return (
    <div className="rounded-lg border">
      <p className="flex items-center gap-2 border-b bg-muted/40 px-3 py-2 text-sm font-medium">
        <FileSpreadsheet aria-hidden className="size-4 text-muted-foreground" />
        {title}
      </p>
      <table className="w-full text-sm">
        <tbody>
          {rows.map(([src, code], i) => (
            <tr key={`${src}-${i}`} className="border-b last:border-b-0">
              <td className="px-3 py-1.5">{src}</td>
              <td className="px-1 text-muted-foreground">→</td>
              <td className="px-3 py-1.5 text-right font-mono text-xs">{code}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
