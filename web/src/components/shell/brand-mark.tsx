import { cn } from "@/lib/utils";

/** Product mark: the letter A in a rounded square (header, login). */
export function BrandMark({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground", className)}>
      <svg viewBox="0 0 24 24" aria-hidden className="size-[62%]">
        <path fillRule="evenodd" fill="currentColor" d="M10.2 3h3.6L21 21h-3.9l-1.5-4H8.4l-1.5 4H3L10.2 3Zm-.5 10.6h4.6L12 7.4l-2.3 6.2Z" />
      </svg>
    </span>
  );
}
