import { defineConfig, devices } from "@playwright/test";

/**
 * T-E2E (SPEC §16) against a running `make demo` stack: web on E2E_BASE_URL (default
 * http://localhost:3000, the compose `web` service), API through its /api proxy.
 * The scenarios share one plant, so they run serially in one worker.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    locale: "ru-RU",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
