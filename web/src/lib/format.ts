/**
 * Number and date formats (SPEC §13.1): ru-RU (space = thousands separator, comma = decimal),
 * dates dd.MM.yyyy, time HH:mm, always in the plant time zone (site.timezone of GET /assets).
 */
const LOCALE = "ru-RU";

export interface Fmt {
  tz: string;
  int: (n: number | null | undefined) => string;
  num: (n: number | null | undefined, digits?: number) => string;
  /** Ratio 0..1 → "91,3 %". */
  pct: (r: number | null | undefined, digits?: number) => string;
  /** Percentage points of a ratio difference → "+2,1 п.п." is composed by callers; this is the number. */
  signed: (n: number | null | undefined, digits?: number) => string;
  /** Tenge, compact: 980 млн, 1,2 млрд. */
  money: (n: number | null | undefined) => string;
  time: (iso: string | number | null | undefined, seconds?: boolean) => string;
  date: (iso: string | number | null | undefined) => string;
  dayMonth: (iso: string | number | null | undefined) => string;
  dateTime: (iso: string | number | null | undefined) => string;
  /** Minutes → "mm:ss" (under an hour) or "h:mm:ss". */
  clock: (minutes: number | null | undefined) => string;
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

export function makeFmt(tz: string, labels: MoneyLabels): Fmt {
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
  const ok = (n: number | null | undefined): n is number => typeof n === "number" && Number.isFinite(n);
  const bad = (v: string | number | null | undefined) => v == null || v === "" || Number.isNaN(toDate(v).getTime());

  return {
    tz,
    int: (n) => (ok(n) ? nf(0, 0).format(Math.round(n)) : DASH),
    num: (n, d = 1) => (ok(n) ? nf(0, d).format(n) : DASH),
    pct: (r, d = 1) => (ok(r) ? `${nf(d, d).format(r * 100)} %` : DASH),
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
    clock: (minutes) => {
      if (!ok(minutes)) return DASH;
      const total = Math.max(0, Math.floor(minutes * 60));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      const pad = (x: number) => String(x).padStart(2, "0");
      return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
    },
  };
}
