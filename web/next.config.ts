import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

// The UI is served same-origin with the API: /api/* and the WebSocket /ws/* are proxied to the
// api service, so the browser never needs CORS. Rewrites are resolved when the server starts
// (dev) or at build time (standalone image), hence API_URL is a build argument in the Dockerfile.
const API_URL = (process.env.API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  output: "standalone", // self-contained server for the Docker image
  poweredByHeader: false,
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_URL}/api/:path*` },
      { source: "/ws/:path*", destination: `${API_URL}/ws/:path*` },
    ];
  },
};

export default withNextIntl(nextConfig);
