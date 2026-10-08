"use client";

import { useMutation } from "@tanstack/react-query";
import { Bot, CheckCircle2, Send, Wrench, XCircle } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api/client";
import type { CopilotAnswer } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export const COPILOT_ROLES = ["director", "master", "maintenance", "quality"];

interface Turn {
  q: string;
  a?: CopilotAnswer;
  error?: string;
}

/** Copilot slide-out (SPEC §11.4): questions in plain words, visible tool calls and the basis line. */
export function CopilotButton() {
  const t = useTranslations("copilot");
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)} data-testid="copilot-open">
        <Bot aria-hidden />
        <span className="hidden lg:inline">{t("open")}</span>
      </Button>
      <CopilotPanel open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function CopilotPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useTranslations("copilot");
  const locale = useLocale();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState("");
  const ask = useMutation({
    mutationFn: (question: string) =>
      api<CopilotAnswer>("/api/v1/copilot/ask", { method: "POST", body: { question, lang: locale === "kk" ? "kk" : "ru" } }),
    onSuccess: (a, question) => setTurns((ts) => ts.map((x) => (x.q === question && !x.a && !x.error ? { ...x, a } : x))),
    onError: (err, question) =>
      setTurns((ts) =>
        ts.map((x) => (x.q === question && !x.a && !x.error ? { ...x, error: err instanceof ApiError ? err.detail || err.title : String(err) } : x)),
      ),
  });
  const send = (question: string) => {
    const s = question.trim();
    if (!s || ask.isPending) return;
    setTurns((ts) => [...ts, { q: s }]);
    setQ("");
    ask.mutate(s);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(q);
  };
  const examples = [t("ex1"), t("ex2"), t("ex3")];
  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={
        <span className="flex items-center gap-2">
          <Bot aria-hidden className="size-5" />
          {t("title")}
        </span>
      }
      footer={
        <form onSubmit={submit} className="flex w-full gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t("placeholder")}
            aria-label={t("placeholder")}
            maxLength={1000}
            className="h-9 flex-1 rounded-md border bg-background px-3 text-sm"
            data-testid="copilot-input"
          />
          <Button type="submit" size="sm" disabled={ask.isPending || !q.trim()}>
            <Send aria-hidden />
            {t("send")}
          </Button>
        </form>
      }
    >
      <div className="flex flex-col gap-4 p-5" data-testid="copilot-panel">
        {turns.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            <p className="mb-2">{t("hint")}</p>
            <ul className="flex flex-col gap-1.5">
              {examples.map((ex) => (
                <li key={ex}>
                  <button type="button" onClick={() => send(ex)} className="rounded-md border px-2.5 py-1.5 text-left text-foreground hover:bg-accent">
                    {ex}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {turns.map((turn, i) => (
          <div key={i} className="flex flex-col gap-2">
            <p className="self-end rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">{turn.q}</p>
            {turn.error ? (
              <p className="flex items-start gap-1.5 text-sm text-severity-critical">
                <XCircle aria-hidden className="mt-0.5 size-4 shrink-0" />
                {turn.error}
              </p>
            ) : turn.a ? (
              <div className="rounded-lg border bg-card p-3 text-sm">
                {turn.a.tool_calls.length ? (
                  <ul className="mb-2 flex flex-wrap gap-1.5" aria-label={t("tools")}>
                    {turn.a.tool_calls.map((c, j) => (
                      <li key={j}>
                        <Badge tone={c.ok ? "outline" : "critical"} title={JSON.stringify(c.arguments)}>
                          <Wrench aria-hidden />
                          {c.name}
                          {c.ok ? <CheckCircle2 aria-hidden /> : <XCircle aria-hidden />}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                ) : null}
                <p className={cn("whitespace-pre-line", turn.a.refused && "text-muted-foreground")}>{turn.a.answer}</p>
                <p className="mt-2 text-[11px] text-muted-foreground">
                  {t("meta", { mode: turn.a.mode, n: turn.a.tool_calls.length, limit: turn.a.tool_limit, ms: turn.a.duration_ms })}
                </p>
              </div>
            ) : (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <Bot aria-hidden className="size-4 animate-pulse" />
                {t("thinking")}
              </p>
            )}
          </div>
        ))}
      </div>
    </Sheet>
  );
}
