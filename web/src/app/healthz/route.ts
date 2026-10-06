// Liveness probe for Docker Compose (SPEC §12.2: /healthz on every service).
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok", service: "web" });
}
