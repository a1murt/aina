import {
  Ban,
  CircleDashed,
  Moon,
  OctagonX,
  Play,
  RefreshCw,
  TriangleAlert,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { getTranslations } from "next-intl/server";

import { cn } from "@/lib/utils";

/** Equipment / line states (SPEC §5.3) with their ISA-101 token, icon and label. */
const STATES: ReadonlyArray<{
  code:
    | "RUNNING"
    | "DEGRADED"
    | "STARVED"
    | "BLOCKED"
    | "DOWN_UNPLANNED"
    | "DOWN_PLANNED"
    | "CHANGEOVER"
    | "IDLE_NO_PLAN";
  Icon: LucideIcon;
  swatch: string;
  tint: string;
}> = [
  { code: "RUNNING", Icon: Play, swatch: "bg-state-running", tint: "text-state-running" },
  { code: "DEGRADED", Icon: TriangleAlert, swatch: "bg-state-degraded", tint: "text-state-degraded" },
  { code: "STARVED", Icon: CircleDashed, swatch: "bg-state-starved", tint: "text-state-starved" },
  { code: "BLOCKED", Icon: Ban, swatch: "bg-state-blocked", tint: "text-state-blocked" },
  {
    code: "DOWN_UNPLANNED",
    Icon: OctagonX,
    swatch: "bg-state-down-unplanned",
    tint: "text-state-down-unplanned",
  },
  {
    code: "DOWN_PLANNED",
    Icon: Wrench,
    swatch: "bg-state-down-planned",
    tint: "text-state-down-planned",
  },
  { code: "CHANGEOVER", Icon: RefreshCw, swatch: "bg-state-changeover", tint: "text-state-changeover" },
  { code: "IDLE_NO_PLAN", Icon: Moon, swatch: "bg-state-idle-no-plan", tint: "text-state-idle-no-plan" },
];

export async function StateLegend() {
  const t = await getTranslations("states");
  return (
    <ul className="grid gap-3 sm:grid-cols-2">
      {STATES.map(({ code, Icon, swatch, tint }) => (
        <li key={code} className="flex items-center gap-3 rounded-lg border bg-card p-3">
          <span aria-hidden className={cn("h-8 w-1.5 rounded-full", swatch)} />
          <Icon aria-hidden className={cn("size-5", tint)} />
          <span className="font-medium">{t(code)}</span>
          <code className="ml-auto text-xs text-muted-foreground">{code}</code>
        </li>
      ))}
    </ul>
  );
}
