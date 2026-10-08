import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { decodeToken, startPage, TOKEN_COOKIE } from "@/lib/auth";

/** "/" → the start page of the signed-in role (the middleware normally redirects first). */
export default async function RootPage() {
  const claims = decodeToken((await cookies()).get(TOKEN_COOKIE)?.value);
  redirect(claims ? startPage(claims) : "/login");
}
