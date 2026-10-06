import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone", // self-contained server for the Docker image
  poweredByHeader: false,
  reactStrictMode: true,
};

export default withNextIntl(nextConfig);
