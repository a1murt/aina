import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

/**
 * /import on stage (DEMO-9, VIS-11): picking the same file twice in an open tab uploads it twice,
 * the report opens with the headline cards, and a stored result never shows the «0,02 с» of a
 * cache hit. Read-only for the plant (a repeated import returns the stored job).
 */
const PASSWORD = process.env.DEMO_PASSWORD ?? "qost2026";
const CASE_XLSX = path.resolve(__dirname, "../../data/case/csv/case2_data.xlsx");

async function login(page: Page, user: string): Promise<void> {
  await page.goto("/login");
  await page.locator('input[name="username"]').fill(user);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
}

test("import: the same file twice → two uploads and the headline cards", async ({ page }) => {
  await login(page, "director");
  await page.goto("/import");
  const isUpload = (url: string, method: string) => method === "POST" && new URL(url).pathname === "/api/v1/import";
  let posts = 0;
  page.on("request", (r) => {
    if (isUpload(r.url(), r.method())) posts++;
  });
  for (let i = 0; i < 2; i++) {
    const done = page.waitForResponse((r) => isUpload(r.url(), r.request().method()));
    await page.getByTestId("file-input").setInputFiles(CASE_XLSX);
    await done;
    await expect(page.getByTestId("import-report")).toBeVisible();
    // the input is cleared after every pick, so the browser fires `change` for the same file again
    await expect(page.getByTestId("file-input")).toHaveValue("");
  }
  expect(posts).toBe(2);
  await expect(page.getByTestId("import-headlines").locator("[data-testid^=import-headline-]")).toHaveCount(4);
  await expect(page.getByTestId("import-report")).not.toContainText(/(^|\D)0,0\d\s?с/);
});
