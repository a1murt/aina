/**
 * Number and date formats (SPEC §13.1): ru-RU (NBSP = thousands separator, comma = decimal),
 * dates dd.MM.yyyy, time HH:mm, always in the plant time zone (site.timezone of GET /assets).
 * A number and its unit (%, мин, с) are joined by a no-break space so they never wrap apart.
 */
const LOCALE = "ru-RU";
const NBSP = "\u00a0";

/** {@link Fmt.ratioPct}: a ratio shown clamped to 0..100 % with the raw value kept for a tooltip. */
export interface RatioPct {
  /** Display text, clamped to 0..100 % ("100,0 %"), or "—" when the value is missing. */
  text: string;
  /** Unclamped text ("104,2 %"), or "—". */
  rawText: string;
  /** `rawText` when the value was clamped, else undefined — ready for a `title` attribute. */
  title: string | undefined;
  /** The ratio as given (null when missing or not finite). */
  raw: number | null;
  /** True when the raw ratio lies outside 0..1 and the display was clamped. */
  clamped: boolean;
}

export interface Fmt {
  tz: string;
  int: (n: number | null | undefined) => string;
  num: (n: number | null | undefined, digits?: number) => string;
  /** Ratio 0..1 → "91,3 %". */
  pct: (r: number | null | undefined, digits?: number) => string;
  /**
   * Ratio for KPI display (OEE, A/P/Q, E): text clamped to 0..100 %, raw value for the tooltip.
   * Early in a shift E can exceed 1 (WIP at shift start) — show "100 %", explain in `title`.
   */
  ratioPct: (r: number | null | undefined, digits?: number) => RatioPct;
  /**
   * Axis / tick label of a ratio: 0.8 → "80 %", 0.005 → "0,5 %" (up to 2 decimals, trailing
   * zeros dropped). Usable directly as an ECharts `axisLabel.formatter`; for series in percent
   * units pass `(v) => fmt.axisPct(v / 100)`.
   */
  axisPct: (r: number | null | undefined) => string;
  /** Percentage points of a ratio difference → "+2,1 п.п." is composed by callers; this is the number. */
  signed: (n: number | null | undefined, digits?: number) => string;
  /** Tenge, compact: 980 млн, 1,2 млрд. */
  money: (n: number | null | undefined) => string;
  time: (iso: string | number | null | undefined, seconds?: boolean) => string;
  date: (iso: string | number | null | undefined) => string;
  dayMonth: (iso: string | number | null | undefined) => string;
  dateTime: (iso: string | number | null | undefined) => string;
  /** Short date: "14.10 07:30" for an instant (plant zone), "14.10" for a calendar date "2026-10-14". */
  dateShort: (iso: string | number | null | undefined) => string;
  /** Minutes → "mm:ss" (under an hour) or "h:mm:ss" — a running counter, not a duration. */
  clock: (minutes: number | null | undefined) => string;
  /**
   * Minutes → a readable duration: "44 с", "9 мин 44 с", "2 ч 5 мин", "3 ч" (seconds are dropped
   * from an hour on; negative values count as 0). Use for stop lengths so they do not read as
   * a time of day.
   */
  duration: (minutes: number | null | undefined) => string;
}

const DASH = "—";

/** Local-date strings such as "2026-10-17" have no zone: format them as calendar dates. */
function toDate(v: string | number): Date {
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) return new Date(`${v}T12:00:00Z`);
  return new Date(v);
}

/** Unit words of compact money amounts (from next-intl, `units.*`). */
export interface MoneyLabels {
  bn: string;
  mn: string;
  k: string;
  currency: string;
}

/** Unit words of durations (from next-intl, `units.h`, `units.min`, `units.s`). */
export interface DurationLabels {
  h: string;
  min: string;
  s: string;
}

export type FmtLabels = MoneyLabels & DurationLabels;

export function makeFmt(tz: string, labels: FmtLabels): Fmt {
  const cache = new Map<string, Intl.NumberFormat>();
  const nf = (min: number, max: number, extra: Intl.NumberFormatOptions = {}) => {
    const key = `${min}|${max}|${JSON.stringify(extra)}`;
    let f = cache.get(key);
    if (!f) {
      f = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: min, maximumFractionDigits: max, ...extra });
      cache.set(key, f);
    }
    return f;
  };
  const dateOnly = (v: string | number) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);
  const dtf = (opts: Intl.DateTimeFormatOptions, utc = false) =>
    new Intl.DateTimeFormat(LOCALE, { ...opts, timeZone: utc ? "UTC" : tz, hourCycle: "h23" });
  const fTime = dtf({ hour: "2-digit", minute: "2-digit" });
  const fTimeS = dtf({ hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fDate = dtf({ day: "2-digit", month: "2-digit", year: "numeric" });
  const fDateUtc = dtf({ day: "2-digit", month: "2-digit", year: "numeric" }, true);
  const fDm = dtf({ day: "2-digit", month: "2-digit" });
  const fDmUtc = dtf({ day: "2-digit", month: "2-digit" }, true);
  const fDmTime = dtf({ day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const ok = (n: number | null | undefined): n is number => typeof n === "number" && Number.isFinite(n);
  const bad = (v: string | number | null | undefined) => v == null || v === "" || Number.isNaN(toDate(v).getTime());

  const pct = (r: number | null | undefined, d = 1) => (ok(r) ? `${nf(d, d).format(r * 100)}${NBSP}%` : DASH);
  const part = (n: number, unit: string) => `${n}${NBSP}${unit}`;

  return {
    tz,
    int: (n) => (ok(n) ? nf(0, 0).format(Math.round(n)) : DASH),
    num: (n, d = 1) => (ok(n) ? nf(0, d).format(n) : DASH),
    pct,
    ratioPct: (r, d = 1) => {
      if (!ok(r)) return { text: DASH, rawText: DASH, title: undefined, raw: null, clamped: false };
      const shown = Math.min(1, Math.max(0, r));
      const rawText = pct(r, d);
      const clamped = shown !== r;
      return { text: clamped ? pct(shown, d) : rawText, rawText, title: clamped ? rawText : undefined, raw: r, clamped };
    },
    axisPct: (r) => (ok(r) ? `${nf(0, 2).format(r * 100)}${NBSP}%` : ""),
    signed: (n, d = 0) => (ok(n) ? nf(0, d, { signDisplay: "exceptZero" }).format(n) : DASH),
    money: (n) => {
      if (!ok(n)) return DASH;
      const abs = Math.abs(n);
      const c = labels.currency;
      if (abs >= 1e9) return `${nf(0, 2).format(n / 1e9)} ${labels.bn} ${c}`;
      if (abs >= 1e6) return `${nf(0, 1).format(n / 1e6)} ${labels.mn} ${c}`;
      if (abs >= 1e3) return `${nf(0, 0).format(n / 1e3)} ${labels.k} ${c}`;
      return `${nf(0, 0).format(n)} ${c}`;
    },
    time: (v, seconds = false) => (bad(v) ? DASH : (seconds ? fTimeS : fTime).format(toDate(v as string | number))),
    date: (v) => (bad(v) ? DASH : (dateOnly(v as string | number) ? fDateUtc : fDate).format(toDate(v as string | number))),
    dayMonth: (v) => (bad(v) ? DASH : (dateOnly(v as string | number) ? fDmUtc : fDm).format(toDate(v as string | number))),
    dateTime: (v) => (bad(v) ? DASH : `${fDate.format(toDate(v as string | number))} ${fTime.format(toDate(v as string | number))}`),
    dateShort: (v) => {
      if (bad(v)) return DASH;
      const x = v as string | number;
      if (dateOnly(x)) return fDmUtc.format(toDate(x));
      return fDmTime.format(toDate(x)).replace(",", "");
    },
    clock: (minutes) => {
      if (!ok(minutes)) return DASH;
      const total = Math.max(0, Math.floor(minutes * 60));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      const pad = (x: number) => String(x).padStart(2, "0");
      return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
    },
    duration: (minutes) => {
      if (!ok(minutes)) return DASH;
      const total = Math.max(0, Math.floor(minutes * 60));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      if (h > 0) return m > 0 ? `${part(h, labels.h)} ${part(m, labels.min)}` : part(h, labels.h);
      if (m > 0) return s > 0 ? `${part(m, labels.min)} ${part(s, labels.s)}` : part(m, labels.min);
      return part(s, labels.s);
    },
  };
}
