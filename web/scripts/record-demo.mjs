// Records the Demo Day scenario (SPEC §17) as a silent screen video with Russian captions.
// Needs a running `make demo` stack. Usage: node web/scripts/record-demo.mjs [outDir]
// Output: <outDir>/aina-demo.webm (default outDir: var/demo-video).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, request as apiRequest } from "@playwright/test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const BASE = process.env.DEMO_BASE_URL ?? "http://localhost:3000";
const PASSWORD = process.env.DEMO_PASSWORD ?? "qost2026";
const OUT_DIR = path.resolve(process.argv[2] ?? path.join(ROOT, "var/demo-video"));
const DOCX = path.join(ROOT, "data/case/source/case2_data.docx");
const CASE_FILE = fs.existsSync(DOCX) ? DOCX : path.join(ROOT, "data/case/csv/case2_data.xlsx");
const W = 1440;
const H = 900;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api() {
  const ctx = await apiRequest.newContext({ baseURL: BASE });
  const res = await ctx.post("/api/v1/auth/login", { data: { username: "admin", password: PASSWORD } });
  const token = (await res.json()).access_token;
  const sim = async (p, data = {}) => {
    const r = await ctx.post(`/api/v1/sim/${p}`, { data, headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok()) throw new Error(`sim/${p}: ${r.status()} ${await r.text()}`);
  };
  return { ctx, sim };
}

async function caption(page, text) {
  await page
    .evaluate((t) => {
      let el = document.getElementById("__demo_caption");
      if (!el) {
        el = document.createElement("div");
        el.id = "__demo_caption";
        Object.assign(el.style, {
          position: "fixed", left: "50%", bottom: "28px", transform: "translateX(-50%)",
          maxWidth: "1100px", padding: "14px 26px", borderRadius: "14px", zIndex: "2147483647",
          background: "rgba(10,14,22,0.86)", color: "#fff", font: "600 24px/1.35 Inter, system-ui, sans-serif",
          textAlign: "center", boxShadow: "0 8px 30px rgba(0,0,0,.35)", pointerEvents: "none",
        });
        document.body.appendChild(el);
      }
      el.textContent = t;
      el.style.display = t ? "block" : "none";
    }, text)
    .catch(() => {});
}

async function card(page, title, lines, ms) {
  page.__caption = "";
  const html = `<html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(circle at 30% 20%,#1d2b45,#0a0e16 70%);color:#fff;font-family:Inter,system-ui,sans-serif">
    <div style="max-width:1100px;padding:40px"><div style="font-size:84px;font-weight:800;letter-spacing:-2px">${title}</div>
    ${lines.map((l, i) => `<div style="font-size:${i === 0 ? 34 : 26}px;margin-top:${i === 0 ? 18 : 12}px;opacity:${i === 0 ? 0.95 : 0.75}">${l}</div>`).join("")}
    </div></body></html>`;
  await page.setContent(html);
  await sleep(ms);
}

async function login(page, user) {
  await page.context().clearCookies();
  await page.goto(`${BASE}/login`);
  await page.locator('input[name="username"]').fill(user);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 15_000 });
}

async function scroll(page, total, steps = 8, pause = 450) {
  for (let i = 0; i < steps; i++) {
    await page.mouse.wheel(0, total / steps);
    await sleep(pause);
  }
}

async function step(name, fn) {
  try {
    await fn();
  } catch (err) {
    console.warn(`step "${name}" failed: ${err.message.split("\n")[0]}`);
  }
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const { ctx, sim } = await api();
  await sim("reset");
  await sim("speed", { value: 60 });

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    recordVideo: { dir: OUT_DIR, size: { width: W, height: H } },
    locale: "ru-RU",
  });
  const page = await context.newPage();
  page.on("load", () => caption(page, page.__caption ?? ""));
  const say = async (t) => {
    page.__caption = t;
    await caption(page, t);
  };

  await card(page, "Aina", [
    "Цифровой двойник автомобильного завода",
    "Кейс 2 · Qostanai Industry Hackathon · АО «Группа компаний АЛЛЮР»",
    "Запись демо: виртуальный завод отдаёт данные по OPC UA / MQTT, скорость 60×",
  ], 6000);

  await step("import", async () => {
    await login(page, "director");
    await page.goto(`${BASE}/import`);
    await say("1. Загружаем файл кейса как есть — система за секунды проверяет данные завода");
    await sleep(2500);
    await page.getByTestId("file-input").setInputFiles(CASE_FILE);
    await page.getByTestId("import-report").waitFor({ timeout: 20_000 });
    await sleep(2500);
    await say("OEE по ISO 22400, брак окраски 5,17% — критично, Конвейер-03 стоял 55 из 60 мин лимита");
    await scroll(page, 900);
    await sleep(2000);
    await say("Журнал простоев не сходится с часами в 5 случаях, буфер перед сборкой проедается, план 4 800 против цели 5 500");
    await scroll(page, 1400, 10);
    await sleep(3000);
  });

  await step("live", async () => {
    await login(page, "master");
    await page.goto(`${BASE}/live`);
    await say("2. Цех онлайн: данные идут от виртуального завода по OPC UA — так же, как от реальных контроллеров");
    await sleep(7000);
    await say("Кузова движутся по линиям, буферы наполняются, узкое место определяется методом активных периодов");
    await sleep(7000);
  });

  await step("S1", async () => {
    await say("3. Сценарий S1: обрыв цепи Конвейера-03 на сборке");
    await sim("inject", { scenario_id: "S1-CHAIN-BREAK" });
    await page.getByTestId("node-CONV-03").waitFor({ timeout: 10_000 });
    await page.locator('[data-testid="node-CONV-03"][data-state="DOWN_UNPLANNED"]').waitFor({ timeout: 10_000 });
    await sleep(2500);
    await say("Схема краснеет за секунду, мастер получает оповещение с оценкой влияния на выпуск");
    await sleep(3500);
    await page.getByTestId("node-CONV-03").click().catch(() => {});
    await sleep(4000);
    await page.keyboard.press("Escape").catch(() => {});
    await scroll(page, 700, 6);
    await sleep(3000);
  });

  await step("operator", async () => {
    await login(page, "operator");
    await say("4. Терминал оператора на планшете: оператор указывает причину остановки в три касания");
    await sleep(3500);
    await page.getByTestId("btn-reason").click();
    await sleep(1500);
    await page.getByTestId("category-ME").click();
    await sleep(1500);
    await page.getByTestId("reason-ME-CHAIN").click();
    await sleep(1200);
    await page.getByTestId("reason-submit").click();
    await sleep(3500);
  });

  await step("maintenance", async () => {
    await login(page, "maintenance");
    await page.goto(`${BASE}/maintenance`);
    await say("5. ТОиР: модель предсказывает отказ робота ABB-04 в ближайшие 8 ч и объясняет почему (SHAP)");
    await sleep(4000);
    await page.getByTestId("eq-row-ABB-04").click().catch(() => {});
    await sleep(5000);
    await say("Камера-02: фильтры упрутся в предел через ~10 ч — система советует заменить их в пересменку 15:00");
    await page.getByTestId("pdm-alerts").scrollIntoViewIfNeeded().catch(() => {});
    await sleep(6000);
  });

  await step("director", async () => {
    await login(page, "director");
    await say("6. Панель директора: прогноз месяца Монте-Карло — P(4 800) и P(5 500), веер P10–P90");
    await sleep(6000);
    await say("Цель 5 500 недостижима без доп. смен: потолок линии ≈ 5 191 за 42 смены");
    await scroll(page, 700, 6);
    await sleep(3000);
    await page.mouse.wheel(0, -2000);
    await say("What-if: брак окраски 3% + субботняя смена 17.10 → пересчёт за секунду");
    await page.getByTestId("open-what-if").click();
    await sleep(1500);
    await page.getByTestId("defect-PAINT").fill("3");
    await sleep(800);
    await page.getByTestId("extra-shifts").locator("button").first().click();
    await sleep(1200);
    await page.getByTestId("what-if-run").click();
    await page.getByTestId("scenario-comparison").first().waitFor({ timeout: 15_000 });
    await sleep(6500);
    await page.keyboard.press("Escape").catch(() => {});
    await say("Рычаги с эффектом в машинах и тенге; эффект «с системой» ≈ 67 млн ₸ в месяц (допущения помечены)");
    await scroll(page, 1200, 8);
    await sleep(4000);
  });

  await card(page, "Готово к пилоту", [
    "ISO 23247 · ISA-95 · ISO 22400 · OPC UA только на чтение · on-prem · офлайн",
    "Пилот на одной линии за 4–6 недель: доступ к OPC UA/SCADA на чтение, выгрузки MES, один сервер",
    "github.com/a1murt/aina",
  ], 7000);

  const video = page.video();
  await context.close();
  await browser.close();
  await ctx.dispose();
  const src = await video.path();
  const dst = path.join(OUT_DIR, "aina-demo.webm");
  fs.renameSync(src, dst);
  console.log(dst);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
