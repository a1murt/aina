"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, FlaskConical, Pause, Play, RotateCcw, Zap } from "lucide-react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import { usePlant } from "@/components/plant-context";
import { QueryState } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api/client";
import type { SimScenario, SimStatus } from "@/lib/api/types";
import { cn } from "@/lib/utils";


/** /demo — demo console (admin, profile demo): sim status, speed, scenarios S1–S5, reset (§13.2). */
export function DemoView() {
  const t = useTranslations("demo");
  const locale = useLocale();
  const plant = usePlant();
  const qc = useQueryClient();
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const status = useQuery({ queryKey: ["sim", "status"], queryFn: () => api<SimStatus>("/api/v1/sim/status"), refetchInterval: 3_000 });
  const scenarios = useQuery({ queryKey: ["sim", "scenarios"], queryFn: () => api<SimScenario[]>("/api/v1/sim/scenarios"), staleTime: Infinity });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown; label: string }) => api(`/api/v1/sim/${path}`, { method: "POST", body: body ?? {} }),
    onSuccess: (_d, v) => {
      setNote({ ok: true, text: v.label });
      void qc.invalidateQueries({ queryKey: ["sim"] });
    },
    onError: (err) => setNote({ ok: false, text: err instanceof ApiError ? `${err.title}: ${err.detail}` : String(err) }),
  });
  const s = status.data;
  const applied = new Map((s?.scenarios ?? []).map((x) => [x.scenario_id, x]));
  // terminal of the line that carries the model plan (plant.yaml), else the first line
  const terminalLine = plant.assets?.plan.find((p) => p.line)?.line ?? plant.assets?.flow[0] ?? "";
  const screens = [
    { href: "/import", key: "import" },
    { href: "/director", key: "director" },
    { href: "/live", key: "live" },
    { href: "/live?tv=1", key: "tv" },
    { href: `/operator/${terminalLine}`, key: "operator" },
  ] as const;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4" data-testid="demo">
      <div>
        <h1 className="text-xl font-semibold">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </div>
      {note ? (
        <p role="status" className={cn("rounded-md border px-3 py-2 text-sm", note.ok ? "bg-muted" : "border-severity-critical/50 bg-severity-critical/10 text-severity-critical")}>
          {note.text}
        </p>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-[1fr_1.4fr]">
        <Card>
          <CardHeader title={t("status")} />
          <CardBody>
            <QueryState loading={status.isLoading} error={status.error} />
            {s ? (
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm" data-testid="sim-status">
                <dt className="text-muted-foreground">{t("state")}</dt>
                <dd>
                  <Badge tone={s.state === "running" && !s.paused ? "outline" : "warning"}>
                    {s.paused ? t("paused") : s.state === "running" ? t("running") : s.state}
                  </Badge>
                </dd>
                <dt className="text-muted-foreground">{t("plantTime")}</dt>
                <dd className="tabular-nums">{plant.fmt.dateTime(s.plant_time)}</dd>
                <dt className="text-muted-foreground">{t("shift")}</dt>
                <dd>{s.shift ? `${plant.fmt.date(s.shift.date)} · ${s.shift.code}` : "—"}</dd>
                <dt className="text-muted-foreground">{t("speed")}</dt>
                <dd className="font-semibold tabular-nums">×{s.speed}</dd>
                <dt className="text-muted-foreground">{t("demoStart")}</dt>
                <dd className="tabular-nums">{plant.fmt.dateTime(s.demo_start)}</dd>
                <dt className="text-muted-foreground">{t("seed")}</dt>
                <dd className="tabular-nums">
                  {s.seed} · {t("epoch", { n: s.epoch })}
                </dd>
                <dt className="text-muted-foreground">{t("lastReset")}</dt>
                <dd className="tabular-nums">{s.last_reset ? plant.fmt.dateTime(s.last_reset) : "—"}</dd>
              </dl>
            ) : null}
            <div className="mt-4">
              <p className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{t("speedTitle")}</p>
              <div className="flex flex-wrap gap-2" role="group" aria-label={t("speedTitle")}>
                {(s?.speed_presets ?? [1, 10, 60, 300]).map((v) => (
                  <Button
                    key={v}
                    variant={s?.speed === v ? "default" : "outline"}
                    aria-pressed={s?.speed === v}
                    data-testid={`speed-${v}`}
                    className="h-10 min-w-16 tabular-nums"
                    onClick={() => act.mutate({ path: "speed", body: { value: v }, label: t("speedSet", { v }) })}
                  >
                    ×{v}
                  </Button>
                ))}
                {s?.paused ? (
                  <Button variant="outline" className="h-10" onClick={() => act.mutate({ path: "start", label: t("started") })}>
                    <Play aria-hidden />
                    {t("start")}
                  </Button>
                ) : (
                  <Button variant="outline" className="h-10" onClick={() => act.mutate({ path: "pause", label: t("pausedNote") })}>
                    <Pause aria-hidden />
                    {t("pause")}
                  </Button>
                )}
              </div>
            </div>
            <div className="mt-5 border-t pt-4">
              {confirmReset ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm">{t("resetConfirm")}</span>
                  <Button
                    variant="destructive"
                    data-testid="reset-confirm"
                    onClick={() => {
                      setConfirmReset(false);
                      act.mutate({ path: "reset", label: t("resetDone") });
                    }}
                  >
                    {t("resetYes")}
                  </Button>
                  <Button variant="outline" onClick={() => setConfirmReset(false)}>
                    {t("cancel")}
                  </Button>
                </div>
              ) : (
                <Button variant="outline" className="h-10" onClick={() => setConfirmReset(true)} data-testid="reset">
                  <RotateCcw aria-hidden />
                  {t("reset")}
                </Button>
              )}
            </div>
          </CardBody>
        </Card>
        <Card>
          <CardHeader title={t("scenarios")} icon={<FlaskConical aria-hidden className="size-4" />} subtitle={t("scenariosHint")} />
          <CardBody>
            <QueryState loading={scenarios.isLoading} error={scenarios.error} />
            <ul className="grid gap-2">
              {(scenarios.data ?? []).map((sc, i) => {
                const st = applied.get(sc.id);
                return (
                  <li key={sc.id} className="flex items-center gap-3 rounded-lg border p-3">
                    <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-sm font-bold">S{i + 1}</span>
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">{(locale === "kk" && sc.name_kk) || sc.name_ru}</p>
                      <p className="truncate text-xs text-muted-foreground">
                        <code>{sc.id}</code>
                        {sc.at_min != null ? ` · ${t("auto")}` : ""}
                        {st ? ` · ${t("appliedAt", { time: plant.fmt.time(st.at) })}` : ""}
                      </p>
                    </div>
                    <Button
                      data-testid={`inject-${sc.id}`}
                      className="h-10"
                      disabled={act.isPending}
                      onClick={() => act.mutate({ path: "inject", body: { scenario_id: sc.id }, label: t("injected", { name: (locale === "kk" && sc.name_kk) || sc.name_ru }) })}
                    >
                      <Zap aria-hidden />
                      {t("inject")}
                    </Button>
                  </li>
                );
              })}
            </ul>
          </CardBody>
        </Card>
      </div>
      <Card>
        <CardHeader title={t("screens")} />
        <CardBody className="flex flex-wrap gap-2">
          {screens.map((x) => (
            <Link key={x.key} href={x.href} target="_blank" className="inline-flex h-10 items-center gap-1.5 rounded-md border px-3 text-sm font-medium hover:bg-accent">
              <ExternalLink aria-hidden className="size-4" />
              {t(`links.${x.key}`)}
            </Link>
          ))}
        </CardBody>
      </Card>
    </div>
  );
}
