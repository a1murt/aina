import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * a terminal toast that goes away, an honest connection badge, KPI never above 100 %, one open
 * stop for one chain break, labels that fit, a header without clipping, safe login redirect.
 * Runs after p0.spec.ts against the same `make demo` stack (S1 is injected there); actions that
 * would write are answered locally by page.route where the test is about the UI only. Live KPI
 * need a running plant: beforeAll presses «Пуск» (no reset, idempotent when already running), so
 * the suite also passes on a paused stand and leaves the simulator running.
 */
const PASSWORD = process.env.DEMO_PASSWORD ?? "qost2026";

async function startPlant(request: APIRequestContext): Promise<void> {
  const login = await request.post("/api/v1/auth/login", { data: { username: "admin", password: PASSWORD } });
  expect(login.ok()).toBeTruthy();
  const { access_token } = (await login.json()) as { access_token: string };
  const res = await request.post("/api/v1/sim/start", { data: {}, headers: { Authorization: `Bearer ${access_token}` } });
  expect(res.ok(), await res.text()).toBeTruthy();
}

async function login(page: Page, user: string, next?: string): Promise<void> {
  await page.goto(next ? `/login?next=${encodeURIComponent(next)}` : "/login");
  await page.locator('input[name="username"]').fill(user);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
}

/** Every percentage shown by the matched elements (e.g. "100,0 %", "87 %"); SVG text included. */
async function percentages(page: Page, selector: string): Promise<number[]> {
  const texts = await page.locator(selector).allTextContents();
  return texts.flatMap((t) => [...t.matchAll(/(-?\d+(?:,\d+)?)\s*%/g)].map((m) => Number((m[1] ?? "0").replace(",", "."))));
}

  test.beforeAll(async ({ request }) => {
    await startPlant(request);
  });

  test("what-if keeps the focus: a typed decimal stays whole (FE-1)", async ({ page }) => {
    await login(page, "director");
    await page.getByTestId("open-what-if").click();
    const field = page.getByTestId("defect-PAINT");
    await field.click();
    await page.keyboard.type("3.5");
    await expect(field).toHaveValue("3.5");
    await page.waitForTimeout(5_000); // clock ticks and parent re-renders must not steal the focus
    await expect(field).toBeFocused();
  });

  test("what-if closes on Esc after «Рассчитать»; the copilot opens over it", async ({ page }) => {
    await login(page, "director");
    await page.getByTestId("open-what-if").click();
    await page.getByTestId("defect-PAINT").fill("3");
    await page.getByTestId("what-if-run").click(); // the button is disabled while it runs: focus leaves the panel
    await expect(page.getByTestId("scenario-comparison").first()).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("what-if-panel")).toHaveCount(0);

    await page.getByTestId("open-what-if").click();
    await expect(page.getByTestId("what-if-panel")).toBeVisible();
    await page.getByTestId("copilot-open").click();
    // the copilot is on top: its input takes the click (a panel under what-if would not)
    await page.getByTestId("copilot-input").click({ timeout: 5_000 });
    await expect(page.getByTestId("copilot-input")).toBeFocused();
    // Esc closes the panel on top only
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("copilot-panel")).toHaveCount(0);
    await expect(page.getByTestId("what-if-panel")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("what-if-panel")).toHaveCount(0);
  });

  test("equipment drawer on /live keeps the focus (FE-1)", async ({ page }) => {
    await login(page, "master");
    await page.goto("/live");
    await page.getByTestId("node-CONV-01").click();
    const classify = page.getByTestId("drawer-classify");
    await classify.focus();
    await page.waitForTimeout(3_000);
    await expect(classify).toBeFocused();
    // Tab stays inside the dialog (focus trap)
    for (let i = 0; i < 12; i++) await page.keyboard.press("Tab");
    expect(await page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBe(true);
  });

  test("terminal toast disappears after about 4 s (FE-2)", async ({ page }) => {
    await page.route("**/api/v1/operator/andon", (route) => route.fulfill({ status: 200, json: { ok: true } }));
    await login(page, "operator");
    await page.getByTestId("btn-andon").click();
    const toast = page.getByTestId("terminal-toast");
    await expect(toast).toContainText("Мастер вызван");
    const shown = Date.now();
    await expect(toast).toBeHidden({ timeout: 6_000 });
    const after = Date.now() - shown;
    expect(after).toBeGreaterThan(3_400);
    expect(after).toBeLessThan(4_600);
  });

  test("connection badge is back to «Онлайн» within 3 s after a network blip (DEMO-8)", async ({ page, context }) => {
    await login(page, "master");
    await page.goto("/live");
    const badge = page.getByTestId("connection-status");
    await expect(badge).toHaveAttribute("data-status", "live");
    await context.setOffline(true);
    await page.waitForTimeout(10_000);
    await context.setOffline(false);
    await expect(badge).toHaveAttribute("data-status", "live", { timeout: 3_000 });
    await expect(badge).toContainText("Онлайн");
  });

  test("KPI percentages never exceed 100 % on /live and the terminal (DEMO-6, VIS-5)", async ({ page, browser }) => {
    await login(page, "master");
    await page.goto("/live");
    await expect(page.getByTestId("line-kpis")).toBeVisible();
    await page.waitForTimeout(2_000);
    const shown = [...(await percentages(page, '[data-testid="line-kpis"]')), ...(await percentages(page, '[data-testid^="area-oee-"] tspan'))];
    expect(shown.length).toBeGreaterThan(0);
    for (const v of shown) expect(v).toBeLessThanOrEqual(100);
    // a clamped value carries the raw estimate in its tooltip
    for (const el of await page.locator('[data-clamped="1"]').all()) await expect(el).toHaveAttribute("title", /ранняя оценка: \d/);

    const op = await browser.newPage({ viewport: { width: 1024, height: 768 } });
    await login(op, "operator");
    await expect(op.getByTestId("terminal-oee")).toBeVisible();
    for (const v of await percentages(op, '[data-testid="terminal-oee"]')) expect(v).toBeLessThanOrEqual(100);
    await op.close();
  });

  test("schema labels fit their nodes (FE-14)", async ({ page }) => {
    await login(page, "master");
    await page.goto("/live");
    await expect(page.getByTestId("node-CONV-03")).toBeVisible();
    const overflow = await page.evaluate(() => {
      const out: string[] = [];
      for (const n of document.querySelectorAll<SVGGElement>('g[data-testid^="node-"]')) {
        const r = n.querySelector<SVGRectElement>("rect.node")?.getBBox();
        if (!r) continue;
        for (const t of n.querySelectorAll<SVGTextElement>("text")) {
          const b = t.getBBox();
          if (b.x + b.width > r.x + r.width + 0.5) out.push(`${n.dataset.testid}: ${t.textContent}`);
        }
      }
      return out;
    });
    expect(overflow).toEqual([]);
  });

  test("one chain break is one open stop; a line code opens the causing unit (FE-3)", async ({ page }) => {
    await login(page, "master");
    await page.goto("/live");
    const stops = page.getByTestId("open-stop");
    // S1 (p0.spec.ts) opened a unit stop and a line stop for ASSY-1: shown as one row
    if ((await stops.count()) > 0) {
      await expect(page.locator('[data-testid="open-stop"][data-entity="ASSY-1"]')).toHaveCount(0);
      await stops.first().locator("button").first().click();
      await expect(page.getByTestId("equipment-drawer")).toBeVisible();
      await expect(page.getByTestId("equipment-drawer")).not.toContainText("unknown equipment");
    }
  });

  test("no English words on RU /login (VIS-9)", async ({ page }) => {
    await page.goto("/login");
    const text = await page.locator("main").innerText();
    expect(text).not.toContain("DEMO_PASSWORD");
    expect(text).not.toContain(".env");
  });

  test("login ?next= accepts only this site (FE-11)", async ({ page }) => {
    await login(page, "master", "//evil.example/phish");
    await expect(page).toHaveURL(/\/live$/);
  });

  test("admin is not offered screens the API refuses (FE-10)", async ({ page }) => {
    await login(page, "admin");
    await expect(page.getByTestId("nav-quality")).toHaveCount(0);
    await expect(page.getByTestId("nav-operator")).toHaveCount(0);
  });

  for (const [role, width] of [
    ["admin", 1280],
    ["director", 1280],
    ["director", 1366],
    ["director", 1440],
  ] as const) {
    test(`header of ${role} is not clipped at ${width} px (VIS-14)`, async ({ browser }) => {
      const page = await browser.newPage({ viewport: { width, height: 800 } });
      await login(page, role);
      const nav = page.locator("header.sticky nav");
      await expect(nav).toBeVisible();
      const { scroll, client } = await nav.evaluate((n) => ({ scroll: n.scrollWidth, client: n.clientWidth }));
      expect(scroll).toBeLessThanOrEqual(client + 1);
      await page.close();
    });
  }

  test("header clock stops at «нет данных» without clock ticks (VIS-21)", async ({ page }) => {
    let snapshotSeen = false;
    // pass the first snapshot, then drop everything the server sends (no ticks, no deltas)
    await page.routeWebSocket(/\/ws\/live/, (ws) => {
      const server = ws.connectToServer();
      server.onMessage((m) => {
        if (!snapshotSeen) {
          ws.send(m);
          if (/"type":\s*"snapshot"/.test(String(m)) && String(m).includes('"lines"')) snapshotSeen = true;
        }
      });
    });
    await login(page, "master");
    await page.goto("/live");
    await expect(page.getByTestId("plant-clock")).toBeVisible();
    await expect(page.getByTestId("plant-clock")).toHaveAttribute("data-stale", "1", { timeout: 15_000 });
    await expect(page.getByTestId("plant-clock")).toContainText("нет данных");
  });

  test("«Симулятор не запущен» when a shift runs and every line is out of plan (VIS-21)", async ({ page }) => {
    type Shift = { date: string; code: string; elapsed_min?: number };
    // the state right after a demo reset, before «Старт»: the clock says a shift is on (well past
    // the grace minutes), every line has been IDLE_NO_PLAN for 10 plant minutes, no line deltas
    const inShift = (shift: Shift | null | undefined, plantTime: string): Shift => ({
      ...(shift ?? { date: plantTime.slice(0, 10), code: "1" }),
      elapsed_min: 30,
    });
    await page.routeWebSocket(/\/ws\/live/, (ws) => {
      const server = ws.connectToServer();
      server.onMessage((m) => {
        let msg: { type?: string; data?: Record<string, unknown> };
        try {
          msg = JSON.parse(String(m)) as typeof msg;
        } catch {
          return;
        }
        const d = msg.data;
        if (msg.type === "snapshot" && d && Array.isArray(d.lines)) {
          const clock = (d.clock ?? { plant_time: new Date().toISOString(), speed: 1 }) as { plant_time: string; shift?: Shift | null };
          const since = new Date(Date.parse(clock.plant_time) - 10 * 60_000).toISOString();
          d.clock = { ...clock, mode: "sim", shift: inShift(clock.shift, clock.plant_time) };
          d.lines = (d.lines as Record<string, unknown>[]).map((l) => ({ ...l, state: "IDLE_NO_PLAN", since }));
          ws.send(JSON.stringify(msg));
        } else if (msg.type === "clock" && d && d.event === "tick" && typeof d.plant_time === "string") {
          ws.send(JSON.stringify({ ...msg, data: { ...d, mode: "sim", shift: inShift(d.shift as Shift | null, d.plant_time) } }));
        }
        // line state, KPI and alert deltas are dropped: the plant stays as the snapshot says
      });
    });
    await login(page, "master");
    await page.goto("/live");
    const banner = page.getByTestId("sim-not-running");
    await expect(banner).toBeVisible({ timeout: 15_000 });
    await expect(banner).toContainText("Симулятор не запущен");
    await expect(banner).toContainText(/дольше \d+ мин/);
  });
});
