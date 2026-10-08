"use client";

import { decodeToken, TOKEN_COOKIE, type Claims } from "@/lib/auth";

/** RFC 7807 problem returned by the API (M4: `/problems/<slug>`). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly slug: string,
    readonly title: string,
    readonly detail: string,
    readonly errors: ReadonlyArray<Record<string, unknown>> = [],
  ) {
    super(detail || title);
    this.name = "ApiError";
  }
}

/** The request never reached the API (offline tablet, proxy down) — candidates for a retry. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

export function isRetryable(err: unknown): boolean {
  return err instanceof NetworkError || (err instanceof ApiError && [502, 503, 504].includes(err.status));
}

// ------------------------------------------------------------------ session

function readCookie(name: string): string | undefined {
  if (typeof document === "undefined") return undefined;
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : undefined;
}

export function getToken(): string | undefined {
  return readCookie(TOKEN_COOKIE);
}

export function getClaims(): Claims | null {
  return decodeToken(getToken());
}

export function saveToken(token: string, expiresAt: string): void {
  const maxAge = Math.max(0, Math.floor((Date.parse(expiresAt) - Date.now()) / 1000));
  document.cookie = `${TOKEN_COOKIE}=${encodeURIComponent(token)}; path=/; max-age=${maxAge}; samesite=lax`;
}

export function clearToken(): void {
  document.cookie = `${TOKEN_COOKIE}=; path=/; max-age=0; samesite=lax`;
}

/** Drop the session and go to the login page, keeping the current location as `next`. */
export function logout(keepLocation = true): void {
  clearToken();
  if (typeof window === "undefined") return;
  const next = keepLocation ? `?next=${encodeURIComponent(window.location.pathname + window.location.search)}` : "";
  window.location.assign(`/login${next}`);
}

// ------------------------------------------------------------------ requests

export type Query = Record<string, string | number | boolean | null | undefined | ReadonlyArray<string>>;

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** Do not redirect to /login on 401 (the login form itself). */
  anonymous?: boolean;
}

export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    params.set(k, Array.isArray(v) ? v.join(",") : String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function problemOf(res: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    /* not JSON (proxy error page) */
  }
  const type = typeof body.type === "string" ? body.type : "";
  const slug = type.split("/").pop() || `http-${res.status}`;
  const detailRaw = body.detail;
  const detail = typeof detailRaw === "string" ? detailRaw : detailRaw ? JSON.stringify(detailRaw) : "";
  const errors = Array.isArray(body.errors) ? (body.errors as Record<string, unknown>[]) : [];
  return new ApiError(res.status, slug, typeof body.title === "string" ? body.title : res.statusText, detail, errors);
}

/** Raw authorised fetch against the same-origin API proxy (`/api/v1/...`). */
export async function apiFetch(path: string, opts: RequestOptions & { form?: FormData } = {}): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(withQuery(path, opts.query), {
      method: opts.method ?? (body ? "POST" : "GET"),
      headers,
      body,
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new NetworkError(err instanceof Error ? err.message : String(err));
  }
  if (res.status === 401 && !opts.anonymous) {
    logout();
    throw await problemOf(res);
  }
  if (!res.ok) throw await problemOf(res);
  return res;
}

/** JSON request; errors are thrown as {@link ApiError} / {@link NetworkError}. */
export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const res = await apiFetch(path, opts);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Download an authorised file (import template) through a temporary object URL. */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const res = await apiFetch(path, { method: "GET" });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
