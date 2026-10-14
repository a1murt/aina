/**
 * Text measurement for the SVG schema: labels are truncated with "…" to the room they have
 * (FE-14, VIS-4) instead of running into neighbouring nodes — Kazakh labels are much longer.
 * Measured with a canvas in the page font; a rough estimate is used where there is no DOM.
 */
let ctx: CanvasRenderingContext2D | null | undefined;
let family = "sans-serif";
const cache = new Map<string, number>();

if (typeof document !== "undefined" && "fonts" in document) {
  // widths measured with a fallback font are wrong once the web font arrives
  document.fonts.addEventListener("loadingdone", () => cache.clear());
}

const estimate = (text: string, px: number) => text.length * px * 0.6;

/** Width of `text` in user units at `px` font size and CSS `weight`. */
export function textWidth(text: string, px: number, weight = 400): number {
  const key = `${weight}|${px}|${text}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  if (typeof document === "undefined") return estimate(text, px);
  if (ctx === undefined) {
    ctx = document.createElement("canvas").getContext("2d");
    family = getComputedStyle(document.body).fontFamily || family;
  }
  if (!ctx) return estimate(text, px);
  ctx.font = `${weight} ${px}px ${family}`;
  const w = ctx.measureText(text).width;
  cache.set(key, w);
  return w;
}

/** `text` cut to fit `maxWidth` (with "…"); `truncated` tells to add a <title> with the full text. */
export function fitText(text: string, maxWidth: number, px: number, weight = 400): { text: string; truncated: boolean } {
  if (textWidth(text, px, weight) <= maxWidth) return { text, truncated: false };
  let lo = 0;
  let hi = text.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (textWidth(`${text.slice(0, mid).trimEnd()}…`, px, weight) <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return { text: `${text.slice(0, lo).trimEnd()}…`, truncated: true };
}
