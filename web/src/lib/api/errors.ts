"use client";

import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { ApiError, NetworkError } from "./client";

/**
 * Localised error text (FE-5): an API problem slug becomes `errors.<slug>`, unknown slugs fall back
 * by HTTP status, a lost connection becomes `common.networkError`. The raw server detail (English,
 * for support) is returned separately and shown only behind a «Подробнее» toggle.
 */
export interface ErrorText {
  text: string;
  /** Raw server detail, or null when it adds nothing. */
  detail: string | null;
  status: number | null;
  forbidden: boolean;
  offline: boolean;
  /** No specific text for this error (the generic «не удалось» was used). */
  generic: boolean;
}

/** Root next-intl translator (no namespace); typed loosely so slugs can be looked up at run time. */
export interface ErrorTranslator {
  (key: string): string;
  has(key: string): boolean;
}

const BY_STATUS: Record<number, string> = {
  400: "bad-request",
  401: "unauthorized",
  403: "forbidden",
  404: "not-found",
  405: "bad-request",
  409: "conflict",
  413: "upload-too-large",
  422: "validation",
  429: "too-many",
  500: "internal",
  501: "not-implemented",
  502: "backend-unavailable",
  503: "backend-unavailable",
  504: "backend-unavailable",
};

/** Error that carries a problem slug without an HTTP round trip (client-side checks). */
export function clientError(slug: string, detail = ""): ApiError {
  return new ApiError(0, slug, slug, detail);
}

function rawDetail(err: ApiError): string | null {
  const parts = [err.status ? String(err.status) : "", err.detail || err.title].filter(Boolean);
  const fields = err.errors
    .map((e) => {
      const loc = Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body" && x !== "query").join(".") : "";
      const msg = typeof e.msg === "string" ? e.msg : "";
      return loc || msg ? `${loc}${loc && msg ? ": " : ""}${msg}` : "";
    })
    .filter(Boolean);
  const text = [parts.join(" · "), ...fields].filter(Boolean).join("\n");
  return text || null;
}

export function errorMessage(err: unknown, t: ErrorTranslator): ErrorText {
  if (err instanceof NetworkError) {
    return { text: t("common.networkError"), detail: err.message || null, status: null, forbidden: false, offline: true, generic: false };
  }
  if (err instanceof ApiError) {
    const byStatus = BY_STATUS[err.status] ?? (err.status >= 500 ? "internal" : "");
    const key = [err.slug, byStatus].find((k) => k && t.has(`errors.${k}`));
    return {
      text: key ? t(`errors.${key}`) : t("errors.unknown"),
      detail: rawDetail(err),
      status: err.status || null,
      forbidden: err.status === 403,
      offline: false,
      generic: !key,
    };
  }
  const detail = err instanceof Error ? err.message : err == null ? null : String(err);
  return { text: t("errors.unknown"), detail: detail || null, status: null, forbidden: false, offline: false, generic: true };
}

/** `const describe = useErrorMessage(); toast(describe(err).text)` — for screens and toasts. */
export function useErrorMessage(): (err: unknown) => ErrorText {
  const t = useTranslations() as unknown as ErrorTranslator;
  return useCallback((err: unknown) => errorMessage(err, t), [t]);
}
