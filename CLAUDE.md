# Aina — правила для Claude Code

## Что это
Цифровой двойник автомобильного завода (кейс 2 Qostanai Industry Hackathon, Demo Day 16.10.2026), позиционирование — «готово к пилоту», а не MVP. Полное ТЗ — `docs/SPEC.md`. Перед любой задачей прочитай нужные разделы ТЗ и ссылайся на требования по ID (`FR-*`, `NFR-*`, `AC` этапа).

## Источники правды
- `docs/SPEC.md` — требования, формулы, API, экраны, этапы и промпты (§18).
- `config/*.yaml` — модель завода (ISA-95), пороги и правила, причины и дефекты, параметры симуляции, бизнес-допущения, контракт тегов.
- `data/case/source/` — исходные файлы кейса; `data/case/csv/` — их CSV/XLSX-копии.
- `data/case/expected/import_expected.json` — эталон для golden-тестов; генерируется `compute_expected.py`. **Вручную не править.**
- `tools/sim_sanity_check.py` — грубая проверка параметров симуляции (не замена DES).

## Как работаем
- Один этап (M0–M9) за сессию; промпты — `docs/SPEC.md` §18.2. Для M2, M3, M6 сначала план, потом код.
- Готовность этапа: код + тесты + `make check` зелёный + запись в `docs/PROGRESS.md` + коммит `Mn: <кратко>`.
- Если ТЗ неоднозначно — выбери вариант, который проще поменять конфигом, и запиши решение в `docs/PROGRESS.md` («Решения»). Если решение меняет формулы, контракт API или golden — остановись и спроси.

## Жёсткие правила
1. Никаких констант завода в коде (такты, пороги, коды, смены) — только через `twin_core.config`.
2. Время: в БД только `timestamptz` UTC; «сейчас» — только `twin_core.clock.Clock` (никаких `datetime.now()`/`time.time()` в бизнес-логике); отображение — `Asia/Qostanay`; `tzdata >= 2024a`.
3. KPI считаются только функциями `twin_core.kpi` по ISO 22400 (SPEC §5.4). Изменение формулы = правка `compute_expected.py` + регенерация golden в том же коммите.
4. Коллектор и всё, что ходит к OPC UA/MQTT заводской стороны, — **только чтение**. Никаких `write_value`/`call_method` вне `services/sim` (тест T-RO).
5. Аналитика, движок и ML не используют тег `Degradation` (это оракул симулятора).
6. `OFFLINE=true` → ноль внешних HTTP-вызовов (LLM API, Telegram, CDN). Шрифты и иконки — локально.
7. Каждый эндпоинт проверяет роль; каждое изменение данных пишется в `audit_log`.
8. Строки UI — только через `next-intl` (ru по умолчанию, kk); цвета состояний — токены ISA-101 (SPEC §13.1); состояние всегда дублируется иконкой/подписью.
9. P2 из SPEC §1.4 не реализуем. Новые зависимости вне стека SPEC §4.3 — только с записью причины в `docs/PROGRESS.md`.
10. Тесты пишутся вместе с кодом. T-GOLD зелёный всегда; красный golden блокирует коммит.

## Команды (создаются в M0)
`make up` · `make down` · `make check` (ruff, mypy, eslint, pytest без интеграционных) · `make test` (всё, включая интеграционные) · `make seed` · `make demo` (миграции → seed → backfill → импорт кейса → live) · `make demo-reset` · `make tagmap` · `make ml-dataset` · `make ml-train`

## Стиль
- Python 3.12, `uv`, типы везде (mypy strict в `twin_core`), `ruff`, async I/O, pydantic v2, `structlog`, SQLAlchemy 2 async + Alembic. Комментарии и docstrings — на английском.
- Next.js 15 App Router, TypeScript strict, Tailwind + shadcn/ui, ECharts, TanStack Query (запросы), Zustand (live-состояние из WS), Playwright.
- Коды сущностей — как в `config/plant.yaml` (`WELD-1`, `CONV-03`, …); русские названия — только в UI.

## Карта репозитория
`packages/twin_core` (конфиг, домен, KPI, DQ, события, часы) · `services/sim` (виртуальный завод, OPC UA, MQTT, пульт) · `services/collector` · `services/engine` · `services/api` · `services/notifier` · `ml/` · `web/` · `infra/` · `tests/` · `docs/`
