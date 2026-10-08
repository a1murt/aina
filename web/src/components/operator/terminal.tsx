"use client";

import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, CloudOff, Minus, PackageX, Plus, RefreshCw, ShieldAlert, Siren, Tag, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { usePlant } from "@/components/plant-context";
import { ReasonPicker } from "@/components/reason-picker";
import { useTicker } from "@/components/shell/header-widgets";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api/client";
import type { DowntimeLive, DowntimeView, Page } from "@/lib/api/types";
import { plantNow, useLive } from "@/lib/live-store";
import { useOfflineQueue } from "@/lib/offline-queue";
import { stateMeta } from "@/lib/states";
import { cn } from "@/lib/utils";

type Mode = null | "reason" | "defect";
const UNCLASSIFIED_WINDOW_MIN = 120;

/** Stops of the line that may be classified: open ones first, else unclassified of the last 2 h. */
function useClassifiable(line: string) {
  const open = useLive((s) => s.downtimeOpen);
  const seq = useLive((s) => s.stateSeq);
  // the unit that caused the stop first, then the line record itself
  const openOfLine = Object.values(open)
    .filter((d) => d.line === line && !d.end_ts)
    .sort((a, b) => Number(a.entity === line) - Number(b.entity === line));
  const recent = useQuery({
    queryKey: ["downtime", "unclassified", line, Math.floor(seq / 20)],
    queryFn: () => {
      const now = plantNow(useLive.getState().clock) ?? Date.now();
      return api<Page<DowntimeView>>("/api/v1/downtime", {
        query: { line, needs_classification: true, from: new Date(now - UNCLASSIFIED_WINDOW_MIN * 60_000).toISOString(), limit: 5 },
      });
    },
    refetchInterval: 15_000,
  });
  const recentItems = (recent.data?.items ?? []).filter((d) => !d.import_id);
  return { openOfLine, recent: recentItems };
}

function Toast({ text, tone, onClose }: { text: string; tone: "ok" | "queued" | "err"; onClose: () => void }) {
  useEffect(() => {
    const id = setTimeout(onClose, 4000);
    return () => clearTimeout(id);
  }, [onClose, text]);
  const Icon = tone === "ok" ? CheckCircle2 : tone === "queued" ? CloudOff : X;
  return (
    <div
      role="status"
      data-testid="terminal-toast"
      className={cn(
        "fixed inset-x-0 bottom-6 z-50 mx-auto flex w-fit max-w-[90vw] items-center gap-3 rounded-xl border px-5 py-4 text-lg font-medium shadow-2xl",
        tone === "ok" && "border-border bg-card",
        tone === "queued" && "border-severity-warning bg-card",
        tone === "err" && "border-severity-critical bg-card text-severity-critical",
      )}
    >
      <Icon aria-hidden className="size-6" />
      {text}
    </div>
  );
}

/** Operator terminal (SPEC §13.2): tablet ≥ 1024×768, touch targets ≥ 64 px, offline queue. */
export function OperatorTerminal({ line }: { line: string }) {
  const t = useTranslations("operator");
  const ts = useTranslations("states");
  const plant = usePlant();
  const live = useLive((s) => s.lines[line]);
  const clock = useLive((s) => s.clock);
  const now = useTicker(1000);
  const { pending, submit, flush } = useOfflineQueue();
  const { openOfLine, recent } = useClassifiable(line);
  const [mode, setMode] = useState<Mode>(null);
  const [toast, setToast] = useState<{ text: string; tone: "ok" | "queued" | "err" } | null>(null);
  const [busy, setBusy] = useState(false);

  const lineAsset = plant.lines[line];
  const state = live?.state ?? "IDLE_NO_PLAN";
  const meta = stateMeta(state);
  const stopped = state.startsWith("DOWN") || state === "STARVED" || state === "BLOCKED";
  const pn = plantNow(clock, now);
  const sinceMin = live?.since && pn ? (pn - Date.parse(live.since)) / 60_000 : null;
  const stop: DowntimeLive | undefined =
    openOfLine.find((d) => d.needs_classification) ?? openOfLine.find((d) => d.state === "DOWN_UNPLANNED") ?? openOfLine[0];
  const reasonTarget = stop
    ? { entity: stop.entity, start_ts: stop.start_ts as string | null, downtime_id: undefined as number | undefined }
    : recent[0]
      ? { entity: recent[0].entity, start_ts: recent[0].start_ts, downtime_id: recent[0].id }
      : null;
  const canClassify = reasonTarget !== null;
  const reason = live?.reason_code ? plant.reasons[live.reason_code] : stop?.reason_code ? plant.reasons[stop.reason_code] : undefined;

  async function run(action: Parameters<typeof submit>[0], label: string, okText: string) {
    setBusy(true);
    try {
      const res = await submit(action, label);
      setToast(res === "sent" ? { text: okText, tone: "ok" } : { text: t("queued"), tone: "queued" });
      setMode(null);
    } catch (err) {
      setToast({ text: err instanceof ApiError ? err.detail || err.title : String(err), tone: "err" });
    } finally {
      setBusy(false);
    }
  }

  const gq = live?.gq ?? 0;
  const plan = live?.plan_to_now ?? 0;
  const ratio = plan > 0 ? gq / plan : 0;

  return (
    <div className="mx-auto flex min-h-[calc(100dvh-3.5rem)] max-w-[1280px] flex-col gap-4 p-4" data-testid="operator-terminal" data-line={line}>
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-sm text-muted-foreground">{t("terminal")}</p>
          <h1 className="text-3xl font-semibold tracking-tight">
            {plant.name(lineAsset, line)} <span className="text-lg font-normal text-muted-foreground">{line}</span>
          </h1>
        </div>
        {pending.length > 0 ? (
          <button
            type="button"
            onClick={() => void flush()}
            data-testid="offline-queue"
            className="inline-flex min-h-16 items-center gap-3 rounded-xl border-2 border-severity-warning bg-severity-warning/15 px-5 text-lg font-semibold"
          >
            <CloudOff aria-hidden className="size-6" />
            {t("pending", { count: pending.length })}
            <RefreshCw aria-hidden className="size-5" />
          </button>
        ) : null}
      </div>

      {/* big status */}
      <section
        data-testid="line-status"
        data-state={state}
        className={cn(
          "flex items-center gap-6 rounded-2xl border-2 px-6 py-5",
          meta.abnormal ? [meta.border, meta.tint] : "border-border bg-card",
        )}
      >
        <meta.Icon aria-hidden className={cn("size-16 shrink-0", meta.abnormal ? meta.text : "text-muted-foreground")} strokeWidth={2.25} />
        <div className="min-w-0 flex-1">
          <p className={cn("text-4xl font-bold tracking-tight uppercase", meta.abnormal && meta.text)}>{ts(state as Parameters<typeof ts>[0])}</p>
          <p className="mt-1 text-lg text-muted-foreground">
            {stopped
              ? reason
                ? plant.name(reason)
                : t("reasonUnknown")
              : t("since", { time: plant.fmt.time(live?.since ?? null) })}
          </p>
        </div>
        <div className="text-right">
          <p className="text-sm text-muted-foreground">{stopped ? t("stoppedFor") : t("runningFor")}</p>
          <p className="text-5xl font-bold tabular-nums" data-testid="status-timer">
            {plant.fmt.clock(sinceMin)}
          </p>
        </div>
      </section>

      {/* output vs plan to now */}
      <section className="grid grid-cols-3 gap-4 rounded-2xl border bg-card px-6 py-4">
        <div className="col-span-2">
          <p className="text-sm text-muted-foreground">{t("output")}</p>
          <p className="tabular-nums">
            <span className="text-5xl font-bold">{plant.fmt.int(gq)}</span>
            <span className="text-2xl text-muted-foreground"> / {plant.fmt.int(plan)}</span>
          </p>
          <div className="mt-2 h-3 rounded-full bg-muted">
            <div
              className={cn("h-3 rounded-full", ratio < 0.95 ? "bg-isa-warning" : "bg-isa-normal")}
              style={{ width: `${Math.min(100, ratio * 100)}%` }}
            />
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{t("planToNow")}</p>
        </div>
        <div className="text-right">
          <p className="text-sm text-muted-foreground">OEE</p>
          <p className="text-4xl font-semibold tabular-nums">{plant.fmt.pct(live?.oee ?? null, 0)}</p>
          <p className="mt-1 text-sm text-muted-foreground tabular-nums">
            {t("defects", { n: Math.max(0, (live?.pq ?? 0) - (live?.gq ?? 0)) })}
          </p>
        </div>
      </section>

      {/* actions */}
      {mode === null ? (
        <section className="grid flex-1 grid-cols-2 gap-4">
          <button
            type="button"
            data-testid="btn-andon"
            disabled={busy}
            onClick={() => run({ kind: "request", method: "POST", path: "/api/v1/operator/andon", body: { line } }, t("andon"), t("andonSent"))}
            className="flex min-h-32 items-center justify-center gap-4 rounded-2xl border-2 border-severity-warning bg-severity-warning/20 text-2xl font-bold hover:bg-severity-warning/30 active:scale-[0.99] disabled:opacity-60"
          >
            <Siren aria-hidden className="size-10" />
            {t("andon")}
          </button>
          <button
            type="button"
            data-testid="btn-reason"
            disabled={!canClassify || busy}
            onClick={() => setMode("reason")}
            className="flex min-h-32 items-center justify-center gap-4 rounded-2xl border-2 bg-card text-2xl font-bold hover:bg-accent active:scale-[0.99] disabled:opacity-40"
          >
            <Tag aria-hidden className="size-10" />
            <span className="text-left">
              {t("reason")}
              {!canClassify ? <span className="block text-sm font-normal text-muted-foreground">{t("reasonDisabled")}</span> : null}
            </span>
          </button>
          <button
            type="button"
            data-testid="btn-defect"
            disabled={busy}
            onClick={() => setMode("defect")}
            className="flex min-h-32 items-center justify-center gap-4 rounded-2xl border-2 bg-card text-2xl font-bold hover:bg-accent active:scale-[0.99]"
          >
            <ShieldAlert aria-hidden className="size-10" />
            {t("defect")}
          </button>
          <button
            type="button"
            data-testid="btn-material"
            disabled={busy}
            onClick={() =>
              run({ kind: "request", method: "POST", path: "/api/v1/operator/material-call", body: { line } }, t("material"), t("materialSent"))
            }
            className="flex min-h-32 items-center justify-center gap-4 rounded-2xl border-2 bg-card text-2xl font-bold hover:bg-accent active:scale-[0.99]"
          >
            <PackageX aria-hidden className="size-10" />
            {t("material")}
          </button>
        </section>
      ) : (
        <section className="flex-1 rounded-2xl border bg-card p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-2xl font-semibold">{mode === "reason" ? t("reasonTitle", { entity: reasonTarget?.entity ?? "" }) : t("defectTitle")}</h2>
            <Button variant="outline" size="touch" onClick={() => setMode(null)}>
              <X aria-hidden />
              {t("cancel")}
            </Button>
          </div>
          {mode === "reason" && reasonTarget ? (
            <ReasonPicker
              touch
              busy={busy}
              onConfirm={(code) =>
                run({ kind: "classify", entity: reasonTarget.entity, start_ts: reasonTarget.start_ts, downtime_id: reasonTarget.downtime_id, reason_code: code }, t("reason"), t("reasonSent"))
              }
            />
          ) : null}
          {mode === "defect" ? (
            <DefectForm
              area={lineAsset?.area ?? ""}
              busy={busy}
              onSubmit={(body) =>
                run({ kind: "request", method: "POST", path: "/api/v1/operator/defects", body: { line, ...body } }, t("defect"), t("defectSent"))
              }
            />
          ) : null}
        </section>
      )}
      {toast ? <Toast {...toast} onClose={() => setToast(null)} /> : null}
    </div>
  );
}

function DefectForm({
  area,
  busy,
  onSubmit,
}: {
  area: string;
  busy: boolean;
  onSubmit: (b: { defect_code: string; qty: number; body_id: string | null }) => void;
}) {
  const t = useTranslations("operator");
  const plant = usePlant();
  const [code, setCode] = useState<string | null>(null);
  const [qty, setQty] = useState(1);
  const [body, setBody] = useState("");
  const codes = plant.defects.filter((d) => d.area === area);
  if (!code) {
    return (
      <div className="grid grid-cols-3 gap-3" data-testid="defect-codes">
        {codes.map((d) => (
          <button
            key={d.code}
            type="button"
            data-testid={`defect-${d.code}`}
            onClick={() => setCode(d.code)}
            className="flex min-h-24 flex-col items-center justify-center gap-1 rounded-xl border bg-card p-3 text-center text-lg font-medium hover:bg-accent"
          >
            {plant.name(d)}
            <code className="text-xs text-muted-foreground">{d.code}</code>
          </button>
        ))}
      </div>
    );
  }
  const d = codes.find((x) => x.code === code);
  return (
    <div className="flex flex-col items-center gap-6">
      <p className="text-2xl font-semibold">
        {plant.name(d)} <code className="text-base text-muted-foreground">{code}</code>
      </p>
      <div className="flex items-center gap-4" role="group" aria-label={t("qty")}>
        <Button variant="outline" size="touch" className="w-20" onClick={() => setQty((q) => Math.max(1, q - 1))} aria-label={t("less")}>
          <Minus aria-hidden />
        </Button>
        <span className="w-24 text-center text-6xl font-bold tabular-nums" data-testid="defect-qty">
          {qty}
        </span>
        <Button variant="outline" size="touch" className="w-20" onClick={() => setQty((q) => Math.min(50, q + 1))} aria-label={t("more")}>
          <Plus aria-hidden />
        </Button>
      </div>
      <label className="flex w-full max-w-md flex-col gap-1.5">
        <span className="text-sm text-muted-foreground">{t("bodyId")}</span>
        <input
          value={body}
          onChange={(e) => setBody(e.target.value.toUpperCase())}
          inputMode="text"
          className="h-16 rounded-xl border bg-background px-4 text-2xl tracking-wider outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="B2610160001"
        />
      </label>
      <div className="flex w-full max-w-xl gap-3">
        <Button variant="outline" size="touch" className="flex-1" onClick={() => setCode(null)}>
          {t("back")}
        </Button>
        <Button size="touch" className="flex-1" disabled={busy} data-testid="defect-submit" onClick={() => onSubmit({ defect_code: code, qty, body_id: body.trim() || null })}>
          {t("save")}
        </Button>
      </div>
    </div>
  );
}
