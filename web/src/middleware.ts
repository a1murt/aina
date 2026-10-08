import { NextResponse, type NextRequest } from "next/server";

import { canAccess, decodeToken, startPage, TOKEN_COOKIE } from "@/lib/auth";

/**
 * Role routing (FR-UI-03): no session → /login; "/" → the role's start page; a screen the role
 * may not open → /forbidden (HTTP 403). The pathname is forwarded to the root layout so the
 * first render already has the right theme.
 */
export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const headers = new Headers(req.headers);
  headers.set("x-pathname", pathname);
  const pass = () => NextResponse.next({ request: { headers } });

  if (pathname === "/login" || pathname === "/forbidden") return pass();

  const claims = decodeToken(req.cookies.get(TOKEN_COOKIE)?.value);
  if (!claims) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname + search)}`;
    return NextResponse.redirect(url);
  }
  // "/" → start page; the bare terminal → the operator's own line (others pick a line there)
  if (pathname === "/" || (pathname === "/operator" && claims.role === "operator")) {
    const url = req.nextUrl.clone();
    url.pathname = startPage(claims);
    url.search = "";
    if (url.pathname !== pathname) return NextResponse.redirect(url);
  }
  if (!canAccess(claims, pathname)) {
    const url = req.nextUrl.clone();
    url.pathname = "/forbidden";
    url.search = `?from=${encodeURIComponent(pathname)}`;
    return NextResponse.rewrite(url, { status: 403, request: { headers } });
  }
  return pass();
}

export const config = {
  // Pages only: API/WS proxy, Next assets, health probe and files with an extension pass through.
  matcher: ["/((?!api|ws|_next|healthz|favicon.ico|.*\\..*).*)"],
};
