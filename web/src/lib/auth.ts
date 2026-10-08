/**
 * Session and role routing (FR-UI-03). The JWT issued by POST /api/v1/auth/login is kept in a
 * cookie readable by the page (it is sent as `Authorization: Bearer` and as `?token=` of the
 * WebSocket). The payload is decoded here only to route the UI — the API verifies every request.
 * Edge-safe: used by the middleware as well as by client components.
 */

export const TOKEN_COOKIE = "qost_token";

export const ROLES = ["director", "master", "operator", "maintenance", "quality", "admin"] as const;
export type Role = (typeof ROLES)[number];

export interface Claims {
  sub: string;
  usr: string;
  name?: string | null;
  role: Role;
  lang?: string;
  lines: string[];
  exp: number;
}

function base64UrlDecode(part: string): string {
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/** Claims of a token that is well-formed and not expired (wall clock, as the API checks it). */
export function decodeToken(token: string | undefined | null, nowMs: number = Date.now()): Claims | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3 || !parts[1]) return null;
  try {
    const raw = JSON.parse(base64UrlDecode(parts[1])) as Partial<Claims>;
    if (typeof raw.role !== "string" || !(ROLES as readonly string[]).includes(raw.role)) return null;
    if (typeof raw.exp !== "number" || raw.exp * 1000 <= nowMs) return null;
    return {
      sub: String(raw.sub ?? ""),
      usr: String(raw.usr ?? ""),
      name: raw.name ?? null,
      role: raw.role,
      lang: raw.lang,
      lines: Array.isArray(raw.lines) ? raw.lines.map(String) : [],
      exp: raw.exp,
    };
  } catch {
    return null;
  }
}

/** Screens of the web UI and the roles that see them (SPEC §3, §13.2). */
export const SCREENS = [
  { href: "/director", key: "director", roles: ["director", "admin"] },
  { href: "/live", key: "live", roles: ["director", "master", "maintenance", "quality", "admin"] },
  { href: "/operator", key: "operator", roles: ["operator", "master", "admin"] },
  { href: "/maintenance", key: "maintenance", roles: ["maintenance", "director", "master", "admin"] },
  { href: "/quality", key: "quality", roles: ["quality", "director", "master", "admin"] },
  { href: "/reports", key: "reports", roles: ["master", "director"] },
  { href: "/import", key: "import", roles: ["director", "admin"] },
  { href: "/demo", key: "demo", roles: ["admin"] },
] as const satisfies ReadonlyArray<{ href: string; key: string; roles: readonly Role[] }>;

export type ScreenKey = (typeof SCREENS)[number]["key"];

/** Start page of a role (SPEC §3 «главный экран»); admin starts at the demo console (P0). */
export function startPage(claims: Claims): string {
  switch (claims.role) {
    case "director":
      return "/director";
    case "master":
      return "/live";
    case "operator": {
      const line = claims.lines[0];
      return line ? `/operator/${encodeURIComponent(line)}` : "/operator";
    }
    case "maintenance":
      return "/maintenance";
    case "quality":
      return "/quality";
    case "admin":
      return "/demo";
  }
}

export function screenOf(pathname: string): (typeof SCREENS)[number] | undefined {
  return SCREENS.find((s) => pathname === s.href || pathname.startsWith(`${s.href}/`));
}

/** Whether the role may open the path; operators only their own line of the terminal. */
export function canAccess(claims: Claims, pathname: string): boolean {
  const screen = screenOf(pathname);
  if (!screen) return true;
  if (!(screen.roles as readonly Role[]).includes(claims.role)) return false;
  if (claims.role === "operator" && screen.key === "operator") {
    const line = decodeURIComponent(pathname.split("/")[2] ?? "");
    return line === "" || claims.lines.length === 0 || claims.lines.includes(line);
  }
  return true;
}

export function visibleScreens(role: Role) {
  return SCREENS.filter((s) => (s.roles as readonly Role[]).includes(role));
}
