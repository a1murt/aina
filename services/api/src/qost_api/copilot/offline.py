"""Deterministic copilot without an LLM (``LLM_PROVIDER=none``, ``OFFLINE=true`` or a provider
failure): a keyword router picks the same read-only tools and a template writes the answer from
their results. It answers the canned question types of the demo (KPI/OEE and output, losses,
downtime, defects, alerts, forecast of the month, unit health/failure risk, bottleneck) for the
entities and the period named in the question, and refuses politely everything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from qost_api.copilot.tools import TOOLS_BY_NAME, Toolbox, ToolError
from twin_core.alert_text import num, pct
from twin_core.aliases import normalize_name
from twin_core.config import TwinConfig

REFUSAL = (
    "Я отвечаю только на вопросы о работе завода: показатели смены и месяца (OEE, выпуск, "
    "доступность), потери, простои, брак, оповещения, прогноз выпуска, состояние оборудования и "
    "узкое место. Переформулируйте вопрос в этих рамках."
)
NO_ACCESS = "Для вашей роли этот вопрос недоступен (нет доступа к нужным данным)."
MAX_INTENTS = 3

# (intent, keywords) in priority order; keywords are normalized (lower case, ё -> е, no spaces).
INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "health",
        ("откаж", "отказ", "здоров", "износ", "риск", "предел", "фильтр", "состояниеоборуд"),
    ),
    ("forecast", ("прогноз", "выполним", "выполнен", "вероятн", "p50", "p10", "p90", "планмесяц")),
    ("bottleneck", ("узкоемест", "узкогомест", "узкоместо", "bottleneck")),
    ("losses", ("потер", "потеря", "убыт")),
    ("downtime", ("простой", "простои", "простоя", "остановк", "остановил", "стоит")),
    ("defects", ("брак", "дефект", "defect")),
    ("alerts", ("оповещ", "тревог", "алерт", "alert", "инцидент")),
    (
        "kpi",
        (
            "oee",
            "оее",
            "ооэ",
            "kpi",
            "кпэ",
            "выпуск",
            "произвед",
            "доступн",
            "эффективн",
            "качеств",
            "mtbf",
            "mttr",
            "производительн",
            "смен",
            "сколько",
            "показател",
        ),
    ),
)


@dataclass
class Entities:
    areas: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.areas or self.lines or self.equipment)


def find_entities(cfg: TwinConfig, question: str) -> Entities:
    """Codes and names of areas, lines and units mentioned in the question (longest first)."""
    text = normalize_name(question)
    names: dict[str, tuple[str, str]] = {}
    for area in cfg.plant.areas:
        for n in filter(None, (area.code, area.name_ru, area.name_kk, *area.aliases)):
            names.setdefault(normalize_name(n), ("area", area.code))
        for line in area.lines:
            for n in filter(None, (line.code, line.name_ru, line.name_kk, *line.aliases)):
                names.setdefault(normalize_name(n), ("line", line.code))
            for eq in line.equipment:
                for n in filter(None, (eq.code, eq.name_ru, eq.name_kk, *eq.aliases)):
                    names.setdefault(normalize_name(n), ("equipment", eq.code))
    found = Entities()
    for name in sorted(names, key=len, reverse=True):
        pattern = _name_pattern(name)
        if pattern is None:
            continue
        match = pattern.search(text)
        if match is None:
            continue
        kind, code = names[name]
        bucket = {"area": found.areas, "line": found.lines, "equipment": found.equipment}[kind]
        if code not in bucket:
            bucket.append(code)
        text = text[: match.start()] + " " + text[match.end() :]
    return found


_NUMBERED = re.compile(r"^([^\W\d_]{4,})(-?\d+)$")


def _name_pattern(name: str) -> re.Pattern[str] | None:
    """Pattern of a normalized name that also matches its inflections: "окраске-1" for
    "окраска-1", "сварки" for "сварка" (the last letter of a word of 5+ letters is optional)."""
    if len(name) < 3:
        return None
    m = _NUMBERED.match(name)
    if m:
        return re.compile(re.escape(m.group(1)[:-1]) + r"[^\W\d_]?" + re.escape(m.group(2)))
    if name.isalpha() and len(name) >= 5:
        return re.compile(re.escape(name[:-1]) + r"[^\W\d_]?")
    return re.compile(re.escape(name))


_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")


def find_period(question: str, today: date) -> tuple[date, date, str]:
    """``(from, to, label)`` named in the question; default: the month so far."""
    q = normalize_name(question)
    m = _DATE_RE.search(question)
    if m:
        year = int(m.group(3) or today.year)
        try:
            day = date(year, int(m.group(2)), int(m.group(1)))
            return day, day, day.strftime("%d.%m.%Y")
        except ValueError:
            pass
    if "вчера" in q:
        d = today - timedelta(days=1)
        return d, d, "вчера"
    if "сегодня" in q or "текущ" in q or "сейчас" in q:
        return today, today, "сегодня"
    if "недел" in q:
        return today - timedelta(days=6), today, "последние 7 дней"
    return today.replace(day=1), today, "с начала месяца"


@dataclass
class OfflineResult:
    text: str
    refused: bool
    calls: list[dict[str, Any]] = field(default_factory=list)


class OfflineCopilot:
    def __init__(self, cfg: TwinConfig) -> None:
        self.cfg = cfg

    def intents(self, question: str) -> list[str]:
        q = normalize_name(question)
        found = [name for name, words in INTENTS if any(w in q for w in words)]
        if (
            "health" in found
            and "forecast" in found
            and not any(w in q for w in ("прогнозвыпуск", "прогнозмесяц", "выполним"))
        ):
            found.remove("forecast")
        return found[:MAX_INTENTS]

    async def answer(self, question: str, toolbox: Toolbox, today: date) -> OfflineResult:
        found = self.intents(question)
        if not found:
            return OfflineResult(REFUSAL, True)
        entities = find_entities(self.cfg, question)
        start, end, label = find_period(question, today)
        period = {"from": start.isoformat(), "to": end.isoformat()}
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        denied = 0
        for intent in found:
            plan = self._plan(intent, entities, period, question, start, end)
            name, args = plan
            tool = TOOLS_BY_NAME[name]
            if toolbox.principal.role not in tool.roles:
                denied += 1
                continue
            try:
                result = await toolbox.run(name, args)
                calls.append({"name": name, "arguments": args, "ok": True, "result": result})
            except ToolError as exc:
                calls.append({"name": name, "arguments": args, "ok": False, "error": str(exc)})
                parts.append(f"Не удалось получить данные ({name}): {exc}.")
                continue
            parts.append(getattr(self, f"_fmt_{intent}")(result, label, entities))
        if not parts:
            return OfflineResult(NO_ACCESS if denied else REFUSAL, not denied, calls)
        return OfflineResult("\n".join(parts), False, calls)

    # ------------------------------------------------------------------ planning

    def _plan(
        self,
        intent: str,
        e: Entities,
        period: dict[str, str],
        question: str,
        start: date,
        end: date,
    ) -> tuple[str, dict[str, Any]]:
        if intent == "health":
            if e.equipment:
                return "get_equipment_health", {"code": e.equipment[0]}
            return "get_alerts", {"status": "open", "rule_id": "AL-M1"}
        if intent == "forecast":
            return "get_forecast", {"month": f"{end.year:04d}-{end.month:02d}"}
        if intent == "bottleneck":
            return "get_bottleneck", dict(period)
        if intent == "losses":
            args: dict[str, Any] = dict(period)
            if e.lines:
                args.update(level="line", code=e.lines[0])
            elif e.areas:
                args.update(level="area", code=e.areas[0])
            return "get_losses", args
        if intent == "downtime":
            args = dict(period)
            q = normalize_name(question)
            if any(w in q for w in ("сейчас", "текущ", "стоит", "идет")):
                args = {"open_only": True}
            if e.equipment:
                args["entity"] = e.equipment[0]
            elif e.lines:
                args["line"] = e.lines[0]
            return "get_downtime", args
        if intent == "defects":
            args = dict(period)
            if e.lines:
                args["line"] = e.lines[0]
            elif e.areas:
                args["area"] = e.areas[0]
            return "get_defects", args
        if intent == "alerts":
            args = {"status": "open"}
            if e.equipment:
                args["entity"] = e.equipment[0]
            return "get_alerts", args
        args = dict(period)
        if e.equipment:
            args.update(level="equipment", code=e.equipment[0])
        elif e.lines:
            args.update(level="line", code=e.lines[0])
        elif e.areas:
            args.update(level="area", code=e.areas[0])
        else:
            args["level"] = "line"
        args["granularity"] = "day" if start == end else "month"
        return "get_kpi", args

    # ------------------------------------------------------------------ templates

    def _name(self, code: str) -> str:
        for table in (self.cfg.lines, self.cfg.equipment, self.cfg.areas):
            item = table.get(code)
            if item is not None:
                return str(item.name_ru)
        return code

    def _fmt_kpi(self, r: dict[str, Any], label: str, e: Entities) -> str:
        rows: dict[str, dict[str, Any]] = {}
        for item in r["items"]:
            rows[item["code"]] = item  # the newest period of each entity
        if not rows:
            return f"KPI ({label}, {r['from']}…{r['to']}): данных за период нет."
        out = [f"KPI, период {r['from']} … {r['to']} ({label}):"]
        for code, k in rows.items():
            if r["level"] == "equipment":
                mtbf = f", MTBF {num(k['mtbf_h'], 1)} ч" if k.get("mtbf_h") is not None else ""
                out.append(
                    f"- {self._name(code)} ({code}): доступность {_p(k.get('availability'))}, "
                    f"отказов {k.get('failures') or 0}{mtbf}"
                )
                continue
            out.append(
                f"- {self._name(code)} ({code}): OEE {_p(k.get('oee'))}, доступность "
                f"{_p(k.get('availability'))}, эффективность {_p(k.get('effectiveness'))}, "
                f"качество {_p(k.get('quality_ratio'))}; выпуск {k.get('pq')}, годных {k.get('gq')}"
                f" (брак {_p(k.get('defect_rate'))})"
            )
        return "\n".join(out)

    def _fmt_losses(self, r: dict[str, Any], label: str, e: Entities) -> str:
        t = r["totals"]
        out = [
            f"Потери, период {r['from']} … {r['to']} ({label}): {num(t['minutes'], 0)} мин, "
            f"≈ {num(t['units'], 1)} авто, ≈ {num(t['kzt'] / 1e6, 1)} млн ₸ (маржа — допущение)."
        ]
        cats = sorted((c for c in r["categories"] if c.get("loss")), key=lambda c: -c["minutes"])[
            :4
        ]
        out += [
            f"- {c['category']}: {num(c['minutes'], 0)} мин, ≈ {num(c['units'], 1)} авто"
            for c in cats
        ]
        return "\n".join(out)

    def _fmt_downtime(self, r: dict[str, Any], label: str, e: Entities) -> str:
        items = r["items"]
        if not items:
            return "Простоев по заданным условиям нет."
        by_reason: dict[str, float] = {}
        for i in items:
            key = i.get("reason_name_ru") or i["reason_code"]
            by_reason[key] = by_reason.get(key, 0.0) + float(i.get("duration_s") or 0) / 60.0
        top = sorted(by_reason.items(), key=lambda kv: -kv[1])[:3]
        out = [
            f"Простои ({label}): показано {len(items)}"
            + (" (есть ещё)" if r["more"] else "")
            + ". "
            "Больше всего минут: " + ", ".join(f"{k} — {num(v, 0)} мин" for k, v in top) + "."
        ]
        for i in items[:5]:
            state = "идёт" if i["open"] else "закрыт"
            out.append(
                f"- {self._name(i['entity'])} ({i['entity']}), {i.get('reason_name_ru')}: "
                f"{num(float(i.get('duration_s') or 0) / 60.0, 0)} мин, {state}, с {i['start_ts']}"
            )
        return "\n".join(out)

    def _fmt_defects(self, r: dict[str, Any], label: str, e: Entities) -> str:
        p = r["pareto"]
        if not p["total"]:
            return f"Брак за период {p['from']} … {p['to']}: записей нет."
        top = ", ".join(
            f"{i['name_ru'] or i['defect_code']} — {i['qty']} ({pct(i['share'])}%)"
            for i in p["items"][:4]
        )
        where = (
            f" ({', '.join(x for x in (p['area'], p['line']) if x)})"
            if p["area"] or p["line"]
            else ""
        )
        return (
            f"Брак{where}, период {p['from']} … {p['to']}: {p['total']} шт. Основные типы: {top}. "
            f"80% брака дают коды: {', '.join(p['vital_few'])}."
        )

    def _fmt_alerts(self, r: dict[str, Any], label: str, e: Entities) -> str:
        items = r["items"]
        counts = r.get("open_counts") or {}
        if not items:
            return "Открытых оповещений нет."
        head = (
            f"Открытые оповещения: критических {counts.get('critical', 0)}, предупреждений "
            f"{counts.get('warning', 0)}, информационных {counts.get('info', 0)}. Новейшие:"
        )
        return "\n".join([head] + [f"- [{i['severity']}] {i['message_ru']}" for i in items[:6]])

    def _fmt_forecast(self, r: dict[str, Any], label: str, e: Entities) -> str:
        s, p = r["summary"], r["p_reach"]
        t = r["targets"]
        lines = [
            f"Прогноз месяца {r['month']} на {r['as_of']}: выпущено {r['mtd']}, медиана итога "
            f"{num(s['p50'], 0)} (P10 {num(s['p10'], 0)} … P90 {num(s['p90'], 0)})."
        ]
        for key, qty in t.items():
            lines.append(f"- вероятность выполнить {key} ({qty}): {pct(p.get(key, 0.0))}%")
        return "\n".join(lines)

    def _fmt_health(self, r: dict[str, Any], label: str, e: Entities) -> str:
        if "signals" not in r:  # the alert list (no unit named)
            return self._fmt_alerts(r, label, e)
        pred = r.get("prediction")
        head = f"{r['name_ru']} ({r['code']}): состояние {r.get('state') or 'нет данных'}"
        if pred:
            head += (
                f"; вероятность отказа в ближайшие {num(pred['horizon_h'], 0)} ч — "
                f"{pct(pred['p_failure'])}%, индекс здоровья {num(pred['health_index'], 0)}"
            )
            if pred.get("factors"):
                head += ". Причины: " + "; ".join(pred["factors"])
        else:
            head += "; прогноз отказа пока не рассчитан"
        out = [head + "."]
        for lim in r.get("limits") or []:
            if lim.get("alert") and lim.get("text_ru"):
                out.append("- " + lim["text_ru"])
        return "\n".join(out)

    def _fmt_bottleneck(self, r: dict[str, Any], label: str, e: Entities) -> str:
        shares = r.get("shares") or {}
        if not shares:
            return "По узкому месту за период данных нет."
        top = sorted(shares.items(), key=lambda kv: -(kv[1]["sole"] + kv[1]["shifting"]))[:3]
        text = ", ".join(
            f"{self._name(c)} — единственное {pct(v['sole'])}%, плавающее {pct(v['shifting'])}%"
            for c, v in top
        )
        live = (r.get("live") or {}).get("current")
        return (
            f"Узкое место, период {r['from']} … {r['to']} ({r['shifts']} смен): основное — "
            f"{self._name(r['overall'])} ({r['overall']}). {text}."
            + (f" Сейчас: {self._name(live)}." if live else "")
        )


def _p(value: Any) -> str:
    return "—" if value is None else f"{pct(float(value))}%"
