import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "border-border bg-muted text-muted-foreground",
        critical: "border-severity-critical/40 bg-severity-critical/12 text-severity-critical",
        warning: "border-severity-warning/50 bg-severity-warning/15 text-foreground",
        info: "border-severity-info/40 bg-severity-info/12 text-severity-info",
        planned: "border-isa-planned/40 bg-isa-planned/12 text-isa-planned",
        outline: "border-border text-foreground",
        solid: "border-transparent bg-primary text-primary-foreground",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export function Badge({ className, tone, ...props }: ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
