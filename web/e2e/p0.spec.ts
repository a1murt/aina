import fs from "node:fs";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import path from "node:path";

/**
 * T-E2E (SPEC §16): login of every role → start page; import docx → 10 DQ and 6 alerts;
 * S1 → CONV-03 red on /live + alert; reason classified on the terminal → visible on /live;
 * what-if changes P50; alert ack. Needs `make demo` (seeded users, password DEMO_PASSWORD).
 */
const PASSWORD = process.env.DEMO_PASSWORD ?? "qost2026";
// The organisers' docx is not published in the repository; fall back to the xlsx copy (same golden).
const CASE_DOCX_PATH = path.resolve(__dirname, "../../data/case/source/case2_data.docx");
const CASE_DOCX = fs.existsSync(CASE_DOCX_PATH)
  ? CASE_DOCX_PATH
  : path.resolve(__dirname, "../../data/case/csv/case2_data.xlsx");

async function token(request: APIRequestContext, user: string): Promise<string> {
  const res = await request.post("/api/v1/auth/login", { data: { username: user, password: PASSWORD } });
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { access_token: string }).access_token;
}

async function login(page: Page, user: string): Promise<void> {
  await page.goto("/login");
  await page.locator('input[name="username"]').fill(user);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
}

async function sim(request: APIRequestContext, admin: string, pathName: string, data: unknown = {}) {
  const res = await request.post(`/api/v1/sim/${pathName}`, { data, headers: { Authorization: `Bearer ${admin}` } });
  expect(res.ok(), await res.text()).toBeTruthy();
}

test.describe.serial("P0 scenarios", () => {
  test.beforeAll(async ({ request }) => {
    // a clean live tail at demo_start, slow enough (×10) for the S1 stop to stay open
    const admin = await token(request, "admin");
    await sim(request, admin, "reset");
    await sim(request, admin, "speed", { value: 10 });
  });

  const START: Record<string, RegExp> = {
    director: /\/director$/,
    master: /\/live$/,
    operator: /\/operator\/[A-Z0-9-]+$/,
    maintenance: /\/maintenance$/,
    quality: /\/quality$/,
    admin: /\/demo$/,
  };
  for (const [role, url] of Object.entries(START)) {
    test(`login as ${role} → start page`, async ({ page }) => {
      await login(page, role);
      await expect(page).toHaveURL(url);
      await expect(page.getByTestId("connection-status")).toHaveAttribute("data-status", "live");
    });
  }

  test("hidden sections: operator gets 403 for /director", async ({ page }) => {
    await login(page, "operator");
    await expect(page.getByTestId("nav-director")).toHaveCount(0);
    const res = await page.goto("/director");
    expect(res?.status()).toBe(403);
    await expect(page.getByTestId("forbidden")).toBeVisible();
  });

  test("import docx → 10 DQ findings and 6 alerts (US-1)", async ({ page }) => {
    await login(page, "director");
    await page.goto("/import");
    await page.getByTestId("file-input").setInputFiles(CASE_DOCX);
    await expect(page.getByTestId("import-report")).toBeVisible();
    await expect(page.getByTestId("import-dq-item")).toHaveCount(10);
    await expect(page.getByTestId("import-alert")).toHaveCount(6);
    await expect(page.getByTestId("import-kpi").locator("tbody tr")).toHaveCount(6);
  });

  test("S1 → CONV-03 red on /live and an alert (US-2)", async ({ page, request }) => {
    await login(page, "master");
    await page.goto("/live");
    await expect(page.getByTestId("connection-status")).toHaveAttribute("data-status", "live");
    const admin = await token(request, "admin");
    await sim(request, admin, "inject", { scenario_id: "S1-CHAIN-BREAK" });
    await expect(page.getByTestId("node-CONV-03")).toHaveAttribute("data-state", "DOWN_UNPLANNED", { timeout: 5_000 });
    await expect(page.locator('[data-testid="alert-row"][data-entity="CONV-03"][data-rule="AL-S1"]').first()).toBeVisible();
    await expect(page.locator('[data-testid="open-stop"][data-entity="CONV-03"]')).toBeVisible();
  });

  test("reason classified on the terminal is visible on /live (US-3)", async ({ browser }) => {
    const op = await browser.newPage({ viewport: { width: 1024, height: 768 } });
    await login(op, "operator");
    await expect(op.getByTestId("line-status")).toHaveAttribute("data-state", /DOWN/);
    await op.getByTestId("btn-reason").click();
    await op.getByTestId("category-ME").click();
    await op.getByTestId("reason-ME-BEARING").click();
    await op.getByTestId("reason-submit").click();
    await expect(op.getByTestId("terminal-toast")).toContainText("Причина записана");

    const master = await browser.newPage();
    await login(master, "master");
    await expect(master.locator('[data-testid="open-stop"][data-entity="CONV-03"] [data-testid="stop-reason"]')).toHaveText(
      "Подшипник / редуктор",
    );
    await op.close();
    await master.close();
  });

  test("what-if changes P50 (US-4)", async ({ page }) => {
    await login(page, "director");
    await expect(page.getByTestId("forecast-p50")).toBeVisible();
    await page.getByTestId("open-what-if").click();
    await page.getByTestId("defect-PAINT").fill("3");
    await page.getByTestId("extra-shifts").locator("button").first().click();
    const started = Date.now();
    await page.getByTestId("what-if-run").click();
    const scen = page.getByTestId("scenario-comparison").first().getByTestId("scenario-p50");
    await expect(scen).toBeVisible();
    expect(Date.now() - started).toBeLessThan(5_000);
    const base = await page.getByTestId("scenario-comparison").first().getByTestId("base-p50").innerText();
    expect(await scen.innerText()).not.toBe(base);
  });

  test("ack an alert", async ({ page }) => {
    await login(page, "director");
    const row = page.locator('[data-testid="alerts-feed"] [data-testid="alert-row"]', { has: page.getByTestId("ack-alert") }).first();
    await expect(row).toBeVisible();
    const id = await row.getAttribute("data-id");
    const ack = page.waitForResponse((r) => r.url().includes(`/api/v1/alerts/${id}/ack`) && r.ok());
    await row.getByTestId("ack-alert").click();
    await ack;
    await expect(page.locator(`[data-testid="alerts-feed"] [data-testid="alert-row"][data-id="${id}"]`)).toHaveCount(0);
  });
});
