"""Deterministic copilot without an LLM (``LLM_PROVIDER=none``, ``OFFLINE=true`` or a provider
failure): a keyword router picks the same read-only tools and a template writes the answer from
their results.

The router maps a question to at most :data:`MAX_BLOCKS` answer blocks. Compound questions of a
plant manager have their own blocks: "why did the month plan slip" (forecast + bottleneck +
losses), "what should we do" (levers), "why did X drop yesterday" (KPI of the day against the
month, losses and stops of X), "what hinders output" (bottleneck + its losses), "which units fail
most" (unit KPI), "why is X stopped" (state + stops), "what is at risk" (failure-risk alerts).
Plain blocks answer KPI/OEE and output, losses, downtime, defects, alerts and unit health for the
entities and the period named in the question. Anything else is refused politely.

Every answer starts with a verdict of one or two sentences, then bullet points; numbers use
no-break thousands and a decimal comma, dates are ``dd.MM``, API keys are shown in Russian
(:mod:`qost_api.copilot.text`). Plant-level losses are the losses at the bottleneck, as on the
director tile (:func:`qost_api.queries.losses.bottleneck_loss_view`): each line's losses weighted by
its share of shifts as the bottleneck, plus quality of the output line; starvation and blocking of
the other lines follow from the bottleneck and are not added up.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from qost_api.copilot import text as tx
from qost_api.copilot.tools import TOOLS_BY_NAME, Toolbox, ToolError
from qost_api.plan_ceiling import line_ceiling
from qost_api.queries.losses import bottleneck_loss_view
from twin_core.config import TwinConfig
from twin_core.forecast.calibration import targets_from_config
from twin_core.kpi import minutes_to_units

REFUSAL = (
    "Я отвечаю только на вопросы о работе завода: показатели смены и месяца (OEE, выпуск, "
    "доступность), потери, простои, брак, оповещения, прогноз выпуска и что сделать для плана, "
    "состояние оборудования и узкое место. Переформулируйте вопрос в этих рамках."
)
NO_ACCESS = "Для вашей роли этот вопрос недоступен (нет доступа к нужным данным)."
MAX_BLOCKS = 3
MAX_BULLETS = 8
MIN_LOSS_MIN = 1.0
"""Loss categories below a minute are noise in a spoken answer."""
B = "• "

TOOL_RU = {
    "get_kpi": "показатели",
    "get_losses": "потери",
    "get_downtime": "простои",
    "get_defects": "брак",
    "get_alerts": "оповещения",
    "get_forecast": "прогноз",
    "get_levers": "рычаги",
    "get_equipment_health": "состояние оборудования",
    "get_bottleneck": "узкое место",
}

# --------------------------------------------------------------------------- text and entities

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_NOT_WORD = re.compile(r"[^\w\-]+")


def normalize_text(text: str) -> str:
    """Lower case, ``ё`` -> ``е``, one dash, punctuation -> spaces, single spaces."""
    t = unicodedata.normalize("NFKC", text.replace("№", " ")).translate(_DASHES).casefold()
    t = t.replace("ё", "е")
    return " ".join(_NOT_WORD.sub(" ", t).split())


@dataclass
class Entities:
    areas: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    ordered: list[tuple[str, str]] = field(default_factory=list)
    """``(kind, code)`` in the order of the question."""

    @property
    def any(self) -> bool:
        return bool(self.areas or self.lines or self.equipment)


_NUM_TOKEN = re.compile(r"^([^\W\d_]+)-?(\d+)$")
_LETTERS = r"[^\W\d_]"


def _stem(word: str) -> str:
    """A word of 5+ letters also matches its inflections (``сварки`` for ``сварка``)."""
    if len(word) >= 5:
        return re.escape(word[:-1]) + _LETTERS + "{0,3}"
    return re.escape(word)


def _token_pattern(token: str) -> str:
    m = _NUM_TOKEN.match(token)
    if m:  # "конвейер-03" also as "конвейер 3", "конвейера №3"
        return _stem(m.group(1)) + r"[\s\-]*0*" + str(int(m.group(2))) + r"(?!\d)"
    if token.isalpha():
        return _stem(token)
    return re.escape(token)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    """Whole-word pattern of a name: a short name (``ОТК``) never matches inside a longer word
    (``отказ``)."""
    norm = normalize_text(name)
    if len(norm.replace(" ", "").replace("-", "")) < 3:
        return None
    body = r"[\s\-]+".join(_token_pattern(t) for t in norm.split())
    return re.compile(r"(?<![^\W_])" + body + r"(?![^\W_])")


def find_entities(cfg: TwinConfig, question: str) -> Entities:
    """Codes and names of areas, lines and units mentioned in the question (longest first)."""
    text = normalize_text(question)
    names: dict[str, tuple[str, str]] = {}
    for area in cfg.plant.areas:
        for n in filter(None, (area.code, area.name_ru, area.name_kk, *area.aliases)):
            names.setdefault(normalize_name_key(n), ("area", area.code))
        for line in area.lines:
            for n in filter(None, (line.code, line.name_ru, line.name_kk, *line.aliases)):
                names.setdefault(normalize_name_key(n), ("line", line.code))
            for eq in line.equipment:
                for n in filter(None, (eq.code, eq.name_ru, eq.name_kk, *eq.aliases)):
                    names.setdefault(normalize_name_key(n), ("equipment", eq.code))
    found = Entities()
    hits: list[tuple[int, str, str]] = []
    for name in sorted(names, key=len, reverse=True):
        pattern = _name_pattern(name)
        if pattern is None:
            continue
        while (match := pattern.search(text)) is not None:
            kind, code = names[name]
            hits.append((match.start(), kind, code))
            text = text[: match.start()] + " " * (match.end() - match.start()) + text[match.end() :]
    for _pos, kind, code in sorted(hits):
        bucket = {"area": found.areas, "line": found.lines, "equipment": found.equipment}[kind]
        if code not in bucket:
            bucket.append(code)
            found.ordered.append((kind, code))
    return found


def normalize_name_key(name: str) -> str:
    return normalize_text(name)


# --------------------------------------------------------------------------- period

_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")
_MONTHS = (
    "январ",
    "феврал",
    "март",
    "апрел",
    "ма[йяе]",
    "июн",
    "июл",
    "август",
    "сентябр",
    "октябр",
    "ноябр",
    "декабр",
)


def _period(question: str, today: date) -> tuple[date, date, str, bool]:
    """``(from, to, label, explicit)``; default: the month so far (not explicit)."""
    q = normalize_text(question)
    m = _DATE_RE.search(question)
    if m:
        year = int(m.group(3) or today.year)
        try:
            day = date(year, int(m.group(2)), int(m.group(1)))
            return day, day, day.strftime("%d.%m"), True
        except ValueError:
            pass
    if re.search(r"\bпозавчера", q):
        d = today - timedelta(days=2)
        return d, d, f"позавчера, {tx.ddmm(d)}", True
    if re.search(r"\bвчера", q):
        d = today - timedelta(days=1)
        return d, d, f"вчера, {tx.ddmm(d)}", True
    if re.search(r"\b(сегодня|сейчас|текущ\w* смен|эт\w* смен|за смену|смена)\b", q):
        return today, today, f"сегодня, {tx.ddmm(today)}", True
    if re.search(r"\bнедел", q):
        d0 = today - timedelta(days=6)
        return d0, today, f"последние 7 дней, {tx.period(d0, today)}", True
    for i, stem in enumerate(_MONTHS, start=1):
        if re.search(r"\b" + stem, q) and not (stem.startswith("ма") and "мая" not in q):
            year = today.year if i <= today.month else today.year - 1
            first = date(year, i, 1)
            nxt = date(year + (i == 12), i % 12 + 1, 1)
            last = min(nxt - timedelta(days=1), today)
            if first > today:
                break
            label = "с начала месяца" if (year, i) == (today.year, today.month) else ""
            return first, last, label or tx.month_word(f"{year}-{i:02d}"), True
    explicit = bool(re.search(r"\bмесяц", q))
    return today.replace(day=1), today, "с начала месяца", explicit


def find_period(question: str, today: date) -> tuple[date, date, str]:
    """``(from, to, label)`` named in the question; default: the month so far."""
    start, end, label, _ = _period(question, today)
    return start, end, label


# --------------------------------------------------------------------------- router


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(parts) + ")")


ADVICE = _rx(
    r"посовету",
    r"совет",
    r"рекоменд",
    r"рычаг",
    r"что (?:\w+ )?(?:делать|сделать|предпринять|изменить|улучшить)",
    r"как (?:\w+ )?(?:выполнить|достичь|добиться|догнать|увеличить|поднять|нарастить|успеть)",
    r"какие меры",
    r"чем помочь",
)
COST = _rx(
    r"сколько стоит",
    r"стоит час",
    r"стоимост",
    r"во сколько обход",
    r"цена (?:часа|минуты|простоя)",
    r"сколько теря\w* (?:за|в) час",
)
PLAN = _rx(
    r"выполн",
    r"план(?:а|у|ом|е)?\b",
    r"успеем",
    r"достигн",
    r"дотян",
    r"прогноз(?! отказ)",
    r"цел[ьи] завода",
    r"сколько (?:\w+ )?(?:выпустим|сделаем|соберем)",
    r"к концу месяца",
)


def _qty_pattern(qty: int) -> str:
    """``5500`` -> ``5 ?500``: a quantity written with or without a thousands space."""
    digits = str(qty)
    head = len(digits) % 3 or 3
    groups = [digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)]
    return " ?".join(groups)


def qty_rx(values: Iterable[int | None]) -> re.Pattern[str] | None:
    """Whole-number match of any of ``values`` in a normalized question (None: no values)."""
    pats = sorted({_qty_pattern(int(v)) for v in values if v}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(pats) + r")\b") if pats else None


def target_quantities(cfg: TwinConfig) -> dict[str, set[int]]:
    """Configured plan quantities by target (``rules.yaml`` plant target, ``plant.yaml`` plan)."""
    out: dict[str, set[int]] = {
        "plant_target": {cfg.rules.thresholds.plant_target_per_month},
        "line_plan": set(),
    }
    for month in sorted({entry.month for entry in cfg.plant.plan}):
        targets = targets_from_config(cfg, month)
        if targets.plant_target:
            out["plant_target"].add(targets.plant_target)
        if targets.line_plan:
            out["line_plan"].add(targets.line_plan)
    return out


RISK = _rx(r"зон\w* риска", r"в риске", r"под угрозой", r"риск отказ", r"может (?:\w+ )?отказ")
STOPPED = _rx(r"стоит\b", r"стоят\b", r"остановил", r"остановлен", r"встал", r"не работает")
WHY = _rx(r"почему", r"из-за чего", r"отчего", r"в чем причин", r"причин")
DROP = _rx(
    r"просел",
    r"просад",
    r"упал",
    r"упало",
    r"снизил",
    r"снижен",
    r"хуже",
    r"ухудш",
    r"провал",
    r"падени",
    r"просел",
)
HINDERS = _rx(
    r"меша",
    r"огранич",
    r"тормоз",
    r"сдержив",
    r"узк\w* мест",
    r"бутылочн",
    r"слаб\w* мест",
    r"главн\w* (?:проблем|причин)",
)
EQ_RANK = _rx(
    r"ломает",
    r"ломают",
    r"ломалось",
    r"ломался",
    r"поломк",
    r"отказыва",
    r"чаще всего",
    r"простаива",
    r"ненадежн",
    r"наименее надежн",
    r"самое проблемн",
    r"больше всего (?:\w+ )?(?:стоял|прост|отказ)",
)
EQ_BY_FAILURES = _rx(r"ломает", r"ломают", r"ломал", r"поломк", r"отказ", r"ненадежн", r"надежн")
BROKE = _rx(r"сломал", r"авари", r"что случил", r"что произошл")
LOSSES = _rx(r"потер", r"потеря", r"убыт", r"недобор", r"недодел", r"недовыпуск")
DOWNTIME = _rx(r"просто[йяеию]", r"остановк", r"стоял", r"останов")
DEFECTS = _rx(r"брак", r"дефект", r"отбраков", r"рекламац", r"несоответств")
ALERTS = _rx(r"оповещ", r"тревог", r"алерт", r"инцидент", r"уведомлен")
HEALTH = _rx(
    r"здоров",
    r"износ",
    r"предел",
    r"фильтр",
    r"отказ",
    r"риск",
    r"состояни",
    r"вероятност\w* отказ",
    r"обслужива",
    r"ремонт",
    r"что с\b",
    r"как (?:\w+ )?себя чувству",
)
KPI = _rx(
    r"oee",
    r"оее",
    r"ооэ",
    r"kpi",
    r"кпэ",
    r"выпуск",
    r"выпустил",
    r"произвел",
    r"сделали",
    r"собрали",
    r"доступност",
    r"эффективн",
    r"качеств",
    r"mtbf",
    r"mttr",
    r"производительн",
    r"показател",
    r"сколько (?:\w+ )?(?:машин|авто|автомобил|кузов)",
    r"сравни",
    r"как (?:\w+ )?(?:работает|работала|работал|отработал)",
)
OVERVIEW = _rx(
    r"как дела",
    r"как обстановка",
    r"обстановк",
    r"что происходит",
    r"как идут дела",
    r"сводк",
    r"как (?:\w+ )?завод",
    r"кратко",
    r"в целом",
)
SHIFT_WORD = _rx(r"смен")
LIMITS = _rx(
    r"фильтр", r"предел", r"вытяжк", r"вибрац", r"цеп[ьи]\b", r"когда (?:\w+ )?(?:менять|обслуж)"
)
KPI_STRONG = _rx(
    r"oee", r"оее", r"ооэ", r"kpi", r"кпэ", r"доступност", r"эффективн", r"mtbf", r"mttr"
)
METRIC_WORDS = {
    "availability": _rx(r"доступност"),
    "effectiveness": _rx(r"эффективн", r"скорост", r"производительн"),
    "quality_ratio": _rx(r"качеств"),
}
NOW = _rx(r"сейчас", r"в данный момент", r"текущ")
DOWNTIME_FOCUS = _rx(r"просто[йяеию]", r"остановк", r"стоял")


@dataclass
class OfflineResult:
    text: str
    refused: bool
    calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Block:
    verdict: str
    bullets: list[str] = field(default_factory=list)


@dataclass
class _State:
    """Tool calls of one answer, shared by every block and sub-period."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    memo: dict[str, Any] = field(default_factory=dict)
    denied: int = 0
    failed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Ctx:
    question: str
    q: str
    e: Entities
    start: date
    end: date
    label: str
    explicit: bool
    today: date
    toolbox: Toolbox
    st: _State

    @property
    def period(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat()}

    def at(self, start: date, end: date, label: str, *, q: str | None = None) -> _Ctx:
        """The same question over another period (the tool calls stay shared)."""
        return replace(self, start=start, end=end, label=label, explicit=True, q=q or self.q)

    def allowed(self, name: str) -> bool:
        return self.toolbox.principal.role in TOOLS_BY_NAME[name].roles

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        """The tool's result, or ``None`` when the role may not call it or it failed."""
        st = self.st
        if not self.allowed(name):
            st.denied += 1
            return None
        key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
        if key in st.memo:
            return st.memo[key]
        try:
            result = await self.toolbox.run(name, args)
        except ToolError as exc:
            st.calls.append({"name": name, "arguments": args, "ok": False, "error": str(exc)})
            st.failed.append(TOOL_RU.get(name, "данные"))
            st.memo[key] = None
            return None
        st.calls.append({"name": name, "arguments": args, "ok": True, "result": result})
        st.memo[key] = result
        return result


def _bullet(text: str) -> str:
    """A bullet line with a capital letter; a heading (``…:``) without the bullet."""
    text = text[:1].upper() + text[1:]
    return text if text.endswith(":") else B + text


def _finish(text: str) -> str:
    """Final touches: no doubled full stop after an abbreviation (``п.п..``)."""
    return re.sub(r"(?<=\.)\.(?!\.)", "", text)


class OfflineCopilot:
    def __init__(self, cfg: TwinConfig) -> None:
        self.cfg = cfg
        self._targets = target_quantities(cfg)
        self._target_rx = qty_rx(q for qs in self._targets.values() for q in qs)

    # ------------------------------------------------------------------ routing

    def intents(self, question: str) -> list[str]:
        """Answer blocks for the question, in order (empty: refuse)."""
        q = normalize_text(question)
        e = find_entities(self.cfg, question)
        found: list[str] = []

        def add(*names: str) -> None:
            for n in names:
                if n not in found:
                    found.append(n)

        if ADVICE.search(q):
            add("advice")
        elif COST.search(q):
            add("cost")
        elif (
            PLAN.search(q) or (self._target_rx is not None and self._target_rx.search(q))
        ) and not (RISK.search(q) or EQ_RANK.search(q)):
            add("plan")
        elif RISK.search(q) and not e.equipment:
            add("risk")
        elif STOPPED.search(q) and not DROP.search(q) and (e.any or WHY.search(q) or "сейчас" in q):
            add("stopped")
        elif DROP.search(q) or (WHY.search(q) and e.any):
            add("why_drop")
        elif EQ_RANK.search(q):
            add("eq_rank")
        elif HINDERS.search(q):
            add("hinders")
        if found:
            if DEFECTS.search(q) and found[0] in ("plan", "hinders"):
                add("defects")
            return found[:MAX_BLOCKS]
        if OVERVIEW.search(q) and not e.any:
            return ["kpi", "alerts"]
        if BROKE.search(q):
            add("downtime")
        if LIMITS.search(q) and not e.equipment:
            add("limits")
        elif HEALTH.search(q):
            add("health" if e.equipment else "risk")
        if LOSSES.search(q):
            add("losses")
        if DOWNTIME.search(q):
            add("downtime")
        if DEFECTS.search(q):
            add("defects")
        if ALERTS.search(q):
            add("alerts")
        if KPI.search(q) or OVERVIEW.search(q):
            add("kpi")
        if "losses" in found:  # "сколько машин потеряли из-за простоев" is one question
            found = [f for f in found if f != "downtime" and (f != "kpi" or KPI_STRONG.search(q))]
        if not found and e.any:  # "Что с окраской?", "ABB-03?"
            add("health" if e.equipment and not (e.lines or e.areas) else "kpi")
        return found[:MAX_BLOCKS]

    async def answer(self, question: str, toolbox: Toolbox, today: date) -> OfflineResult:
        found = self.intents(question)
        if not found:
            return OfflineResult(REFUSAL, True)
        start, end, label, explicit = _period(question, today)
        ctx = _Ctx(
            question=question,
            q=normalize_text(question),
            e=find_entities(self.cfg, question),
            start=start,
            end=end,
            label=label,
            explicit=explicit,
            today=today,
            toolbox=toolbox,
            st=_State(),
        )
        blocks: list[_Block] = []
        for name in found:
            handler: Callable[[_Ctx], Awaitable[_Block | None]] = getattr(self, f"_b_{name}")
            block = await handler(ctx)
            if block is not None:
                blocks.append(block)
        st = ctx.st
        if not blocks:
            if st.failed:
                return OfflineResult(
                    f"Не удалось получить данные ({', '.join(dict.fromkeys(st.failed))}). "
                    "Попробуйте ещё раз через минуту.",
                    False,
                    st.calls,
                )
            return OfflineResult(NO_ACCESS if st.denied else REFUSAL, not st.denied, st.calls)
        lead = " ".join(b.verdict for b in blocks[:2] if b.verdict)
        bullets = [x for b in blocks for x in b.bullets][:MAX_BULLETS]
        if st.failed:
            bullets.append(f"Не удалось получить: {', '.join(dict.fromkeys(st.failed))}.")
        lines = [lead] + [_bullet(x) for x in bullets]
        return OfflineResult(_finish("\n".join(lines)), False, st.calls)

    # ------------------------------------------------------------------ names

    def _name(self, code: str) -> str:
        for table in (self.cfg.lines, self.cfg.equipment, self.cfg.areas):
            item = table.get(code)
            if item is not None:
                return str(item.name_ru)
        if code == self.cfg.plant.site.code:
            return "завод"
        return code

    def _full(self, code: str) -> str:
        """``Камера базы и лака (BOOTH-02)``; lines and areas by name only."""
        if code in self.cfg.equipment:
            name = self._name(code)
            if code in name:
                return name
            if name.endswith(
                ")"
            ):  # "Конвейер-03 (финальная)" -> "Конвейер-03 (финальная, CONV-03)"
                return f"{name[:-1]}, {code})"
            return f"{name} ({code})"
        return self._name(code)

    def _on(self, code: str) -> str:
        """Locative phrase: ``на участке «Окраска»``, ``на линии Окраска-1``."""
        if code in self.cfg.areas:
            return f"на участке «{self._name(code)}»"
        if code in self.cfg.lines:
            return f"на {self._line_gen(code)}"
        return f"на {self._full(code)}"

    def _of(self, code: str | None) -> str:
        """Genitive phrase: ``участка «Сварка»``, ``линии Окраска-1``, a unit by name."""
        if not code:
            return "завода"
        if code in self.cfg.areas:
            return f"участка «{self._name(code)}»"
        if code in self.cfg.lines:
            return self._line_gen(code)
        return self._full(code)

    def _line_gen(self, code: str) -> str:
        """``линии Окраска-1``; a line named «Линия контроля» -> ``линии контроля``."""
        name = self._name(code)
        head, _, rest = name.partition(" ")
        if head.casefold() == "линия" and rest:
            return f"линии {rest}"
        return f"линии {name}"

    def _line_of(self, kind: str, code: str) -> str | None:
        if kind == "line":
            return code
        if kind == "equipment":
            return self.cfg.line_of_equipment(code).code
        if kind == "area":
            lines = [ln.code for ln in self.cfg.areas[code].lines]
            return lines[0] if lines else None
        return None

    def _shift_name(self, code: str) -> str:
        for s in self.cfg.plant.calendar.shifts:
            if s.code == code:
                return str(s.name_ru)
        return f"смена {code}"

    def _dedup_stops(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One record per stop: a line-level and an equipment-level record of the same stop
        (same start, reason and duration) collapse into the equipment one."""
        kept: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        for item in items:
            key = (item.get("start_ts"), item.get("reason_code"), item.get("duration_s"))
            prev = kept.get(key)
            if prev is None or (
                item.get("entity") in self.cfg.equipment
                and prev.get("entity") not in self.cfg.equipment
            ):
                kept[key] = item
        return list(kept.values())

    def _why(self, item: dict[str, Any]) -> str:
        """Reason of a downtime record in words."""
        return str(item.get("reason_name_ru") or self._reason(item.get("reason_code")))

    @staticmethod
    def _dur(item: dict[str, Any]) -> str:
        return tx.minutes(float(item.get("duration_s") or 0) / 60)

    def _reason(self, code: str | None) -> str:
        if not code:
            return "причина не указана"
        reason = self.cfg.reasons.get(code)
        return str(reason.name_ru) if reason is not None else "причина не указана"

    def _month(self, c: _Ctx) -> str:
        return f"{c.end.year:04d}-{c.end.month:02d}"

    # ------------------------------------------------------------------ helpers on payloads

    @staticmethod
    def _rows(r: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        """Newest row of every entity of a KPI answer."""
        rows: dict[str, dict[str, Any]] = {}
        for item in (r or {}).get("items") or []:
            rows[item["code"]] = item
        return rows

    @staticmethod
    def _weakest(k: dict[str, Any]) -> str | None:
        parts = {p: float(k[p]) for p in tx.KPI_PART_RU if k.get(p) is not None}
        if not parts:
            return None
        return tx.KPI_PART_RU[min(parts, key=lambda p: parts[p])]

    @staticmethod
    def _loss_cats(r: dict[str, Any]) -> list[dict[str, Any]]:
        cats = [
            c
            for c in r.get("categories") or []
            if c.get("loss") and float(c.get("minutes") or 0) >= MIN_LOSS_MIN
        ]
        return sorted(cats, key=lambda c: -float(c.get("units") or c.get("minutes") or 0))

    def _cat_text(self, c: dict[str, Any]) -> str:
        return (
            f"{tx.label(tx.LOSS_RU, c['category'])} — {tx.minutes(c.get('minutes'))}, "
            f"{tx.cars(c.get('units'))}"
        )

    async def _bottleneck_losses(
        self, c: _Ctx
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None, _Ctx]:
        """(bottleneck answer, losses answer, main bottleneck line) for the period: the losses
        at the bottleneck (:func:`bottleneck_loss_view`, the director tile's figure); the plant
        tree only when the period has no bottleneck."""
        bn = await c.call("get_bottleneck", c.period)
        line = (bn or {}).get("overall")
        if not line and c.start == c.end == c.today:
            # the shift has just started: the last full day ("за смену") or the month so far
            if SHIFT_WORD.search(c.q):
                d = c.today - timedelta(days=1)
                return await self._bottleneck_losses(c.at(d, d, f"вчера, {tx.ddmm(d)}"))
            if c.today.day > 1:
                return await self._bottleneck_losses(
                    c.at(c.today.replace(day=1), c.today, "с начала месяца")
                )
        losses = await c.call("get_losses", {**c.period, "level": "plant"})
        if line:
            view = bottleneck_loss_view(losses, bn, self.cfg.flow_lines[-1]) if losses else None
            return bn, view, line, c
        return bn, losses, None, c

    def _bn_where(self, losses: dict[str, Any], line: str | None) -> str:
        """Where the losses were counted: one bottleneck line is named, several are not."""
        if not line:
            return "по заводу"
        lines = losses.get("lines") or [{"line": line}]
        if len(lines) == 1:
            return f"на узком месте — линии {self._name(lines[0]['line'])}"
        return "на узком месте"

    def _weights_text(self, losses: dict[str, Any], *, shares_shown: bool = False) -> str:
        """How the bottleneck losses were weighted, for a bullet; empty for a single line
        without quality losses of another (output) line."""
        lines = losses.get("lines") or []
        if len(lines) < 2:
            named = {x["line"] for x in lines}
            other = any(i.get("line") not in named for i in losses.get("items") or [])
            return "Плюс брак выходной линии — целиком." if other else ""
        if shares_shown:
            return "Потери линий взяты по этим долям, брак выходной линии — целиком."
        parts = ", ".join(f"{self._name(x['line'])} — {tx.pct(x['share'], 0)}" for x in lines)
        return (
            f"Потери линий взяты по доле смен, когда линия была узким местом ({parts}), "
            "брак выходной линии — целиком."
        )

    def _shares_text(self, bn: dict[str, Any], limit: int = 3) -> str:
        shares = bn.get("shares") or {}
        top = sorted(shares.items(), key=lambda kv: -(kv[1]["sole"] + kv[1]["shifting"]))
        return ", ".join(
            f"{self._name(code)} — {tx.pct(v['sole'] + v['shifting'], 0)}"
            for code, v in top[:limit]
            if v["sole"] + v["shifting"] > 0
        )

    # ------------------------------------------------------------------ blocks

    async def _b_advice(self, c: _Ctx) -> _Block | None:
        r = await c.call("get_levers", {"month": self._month(c)})
        if r is None:
            if c.st.denied and not c.st.failed:
                return await self._b_hinders(c)
            return None
        base = r.get("base") or {}
        needed: dict[str, dict[str, Any]] = r.get("shifts_needed_for_target") or {}
        p_reach: dict[str, float] = base.get("p_reach") or {}
        plant_rx = self._named_target_rx("plant_target", needed)
        line_rx = self._named_target_rx("line_plan", needed)
        if (re.search(r"\bцел", c.q) or plant_rx.search(c.q)) and "plant_target" in p_reach:
            order = ["plant_target"]
        elif (re.search(r"план\w* лини", c.q) or line_rx.search(c.q)) and "line_plan" in p_reach:
            order = ["line_plan"]
        else:  # "выполнить план": the line plan first, then the plant target
            order = sorted(p_reach, key=lambda t: t != "line_plan")
        sentences = [self._target_verdict(t, base, needed) for t in order]
        verdict = " ".join(x for x in sentences if x)
        target = order[0] if order else "plant_target"
        bullets = []
        levers = sorted(r.get("levers") or [], key=lambda lv: lv.get("rank", 99))
        for lv in [lv for lv in levers if lv.get("top")][:4] or levers[:3]:
            dp = float((lv.get("delta_p_reach") or {}).get(target) or 0.0)
            effect = (lv.get("effect_kzt") or {}).get("p50")
            bullets.append(
                f"{self._lever_text(lv)}: +{tx.num(lv.get('delta_mean'), 0)} авто"
                + (f", вероятность +{tx.pp(dp)}" if dp >= 0.005 else "")
                + (f", эффект {tx.money(effect)}" if effect else "")
            )
        if bullets:
            bullets.insert(0, "Лучшие рычаги (прогноз до конца месяца):")
        return _Block(verdict, bullets)

    def _named_target_rx(self, target: str, needed: dict[str, dict[str, Any]]) -> re.Pattern[str]:
        """Quantities that name ``target`` in a question: configured ones and the payload's
        (``shifts_needed_for_target[target][*].qty``, the database plan when imported)."""
        sets = needed.get(target) or {}
        qtys = self._targets.get(target, set()) | {
            int(x["qty"]) for x in sets.values() if isinstance(x, dict) and x.get("qty")
        }
        return qty_rx(qtys) or re.compile(r"(?!)")

    def _target_verdict(
        self, target: str, base: dict[str, Any], needed: dict[str, dict[str, Any]]
    ) -> str:
        """One sentence per target: the probability, then the extra shifts that reach it."""
        p = float((base.get("p_reach") or {}).get(target, 0.0))
        sets = needed.get(target) or {}
        first: dict[str, Any] = next(iter(sets.values()), {})
        qty = first.get("qty")
        tname = f"{tx.label(tx.TARGET_RU, target).capitalize()} {tx.num(qty)}" if qty else "План"
        fem = target == "plant_target"  # «цель» is feminine, «план» masculine
        if p >= 0.9:
            sure = "гарантирована" if fem else "гарантирован"
            return f"{tname} почти {sure}: вероятность {tx.pct(p, 0)}."
        hard = ("недостижима" if fem else "недостижим") if p < 0.05 else "под угрозой"
        shortfall = (base.get("expected_shortfall") or {}).get(target)
        text = f"{tname} при текущем темпе {hard}: вероятность {tx.pct(p, 0)}" + (
            f", недобор {tx.cars(shortfall)}" if shortfall and float(shortfall) >= 1 else ""
        )
        options = [s for s in sets.values() if s.get("shifts") is not None and s["shifts"] > 0]
        if options:
            best = min(options, key=lambda s: s["shifts"])
            days = ", ".join(dict.fromkeys(tx.ddmm(d["date"]) for d in best.get("dates") or []))
            n = int(best["shifts"])
            return (
                text
                + (
                    "; хватит 1 доп. смены"
                    if n == 1
                    else f"; нужно {n} доп. {tx.plural(n, 'смена', 'смены', 'смен')}"
                )
                + (f" ({days})" if days else "")
                + f" — тогда {tx.pct(best.get('p_reach') or 0.0, 0)}."
            )
        if sets:
            p_max = max(float(s.get("p_max") or 0.0) for s in sets.values())
            return text + f"; даже все доп. смены месяца дают не больше {tx.pct(p_max, 0)}."
        return text + "."

    def _lever_text(self, lv: dict[str, Any]) -> str:
        kind, p = lv.get("kind"), lv.get("params") or {}
        if kind == "eliminate_failures":
            return f"без отказов: {self._full(str(p.get('equipment')))}"
        if kind == "defect_norm":
            return (
                f"брак участка «{self._name(str(p.get('area')))}» с {tx.pct(p.get('from'))} "
                f"до {tx.pct(p.get('to'))}"
            )
        if kind == "filter_policy":
            return "менять фильтры окрасочных камер по прогнозу, в пересменку"
        if kind == "extra_shift":
            shifts = ", ".join(self._shift_name(s) for s in p.get("shifts") or [])
            return f"доп. смена {tx.ddmm(str(p.get('date')))} ({shifts})"
        if kind == "buffer_capacity":
            return (
                f"буфер «{self._buffer_name(str(p.get('buffer')))}»: "
                f"{tx.num(p.get('from'))} → {tx.num(p.get('to'))} мест"
            )
        return "прочий рычаг"

    def _buffer_name(self, code: str) -> str:
        buf = self.cfg.buffers.get(code)
        return str(buf.name_ru) if buf is not None else code

    async def _b_cost(self, c: _Ctx) -> _Block | None:
        line: str | None = None
        for kind, code in c.e.ordered:
            line = self._line_of(kind, code)
            if line:
                break
        bn = None
        if line is None:
            bn = await c.call("get_bottleneck", c.period)
            line = (bn or {}).get("overall") or self.cfg.flow_lines[-1]
        losses = await c.call("get_losses", {**c.period, "level": "line", "code": line})
        if losses is None:
            return None
        ict = float(self.cfg.lines[line].ict_seconds)
        per_hour = minutes_to_units(60.0, ict) or 0.0
        kzt_per_car = float(losses.get("kzt_per_car") or 0.0)
        verdict = (
            f"Час простоя линии {self._name(line)} стоит {tx.money(per_hour * kzt_per_car)} "
            f"(минута — {tx.money(per_hour * kzt_per_car / 60)}): за час она выпускает "
            f"≈ {tx.num(per_hour, 1)} авто (такт {tx.num(ict)} с), "
            f"маржа {tx.num(kzt_per_car)} ₸ на авто — допущение."
        )
        t = losses.get("totals") or {}
        bullets = [
            f"Потери линии {self._name(line)} ({c.label}): {tx.minutes(t.get('minutes'))}, "
            f"{tx.cars(t.get('units'))}, {tx.money(t.get('kzt'))}."
        ]
        if bn is None:
            bn = await c.call("get_bottleneck", c.period)
        overall = (bn or {}).get("overall")
        if overall and overall != line:
            bullets.append(
                f"{self._name(line)} — не узкое место (узкое — {self._name(overall)}): "
                "короткий простой частично гасит буфер, реальная потеря выпуска меньше."
            )
        return _Block(verdict, bullets)

    async def _b_plan(self, c: _Ctx) -> _Block | None:
        month = self._month(c)
        fc = await c.call("get_forecast", {"month": month})
        mtd = c.at(c.end.replace(day=1), c.end, "с начала месяца")
        bn, losses, line, mtd = await self._bottleneck_losses(mtd)
        if fc is None:
            if bn is None and losses is None:
                return None
            return self._hinders_block(mtd, bn, losses, line)
        s, p = fc["summary"], fc.get("p_reach") or {}
        parts = []
        for key, qty in (fc.get("targets") or {}).items():
            part = (
                f"{tx.label(tx.TARGET_RU, key)} {tx.num(qty)} — "
                f"вероятность {tx.pct(p.get(key, 0.0), 0)}"
            )
            short = (fc.get("expected_shortfall") or {}).get(key)
            if short and float(short) >= 1 and float(p.get(key, 0.0)) < 0.5:
                part += f", недобор {tx.cars(short)}"
            parts.append(part)
        verdict = (
            f"Прогноз на {tx.month_word(fc['month'])}: {tx.cars(s['p50'])} "
            f"(P10–P90 {tx.num(s['p10'])}–{tx.num(s['p90'])})"
            + (f"; {'; '.join(parts)}" if parts else "")
            + "."
        )
        ceiling = line_ceiling(self.cfg, fc["month"])
        plant_target = (fc.get("targets") or {}).get("plant_target")
        reasons = []
        if ceiling is not None and plant_target and plant_target > ceiling.qty:
            reasons.append(
                f"цель выше потолка линии ≈ {tx.num(ceiling.qty)} авто за месяц "
                f"(такт {tx.num(ceiling.ict_seconds)} с × {ceiling.shifts} "
                f"{tx.plural(ceiling.shifts, 'смена', 'смены', 'смен')})"
            )
        shares = (bn or {}).get("shares") or {}
        if line and line in shares:
            share = shares[line]["sole"] + shares[line]["shifting"]
            reasons.append(f"узкое место — {self._name(line)} ({tx.pct(share, 0)} смен)")
        if reasons:
            verdict += " Причина: " + "; ".join(reasons) + "."
        bullets = [f"Выпущено {tx.num(fc.get('mtd'))} авто с начала месяца."]
        rates = fc.get("required_rate") or {}
        remaining = fc.get("remaining_shifts")
        rate_parts = [
            f"{tx.num(v, 1)} для «{tx.label(tx.TARGET_RU, k)}»"
            for k, v in rates.items()
            if v is not None
        ]
        if rate_parts:
            bullets.append(
                "Нужный темп, авто за смену: "
                + ", ".join(rate_parts)
                + (f" (осталось {tx.num(remaining, 1)} смен)" if remaining else "")
                + (
                    f"; потолок линии — {tx.num(ceiling.rate_per_shift, 1)}"
                    if ceiling is not None
                    else ""
                )
                + "."
            )
        if losses is not None:
            cats = self._loss_cats(losses)[:3]
            if cats:
                where = self._bn_where(losses, line)
                bullets.append(
                    f"Главные потери {where}: " + "; ".join(self._cat_text(x) for x in cats) + "."
                )
        if c.allowed("get_levers"):
            bullets.append("Что делать — спросите: «Что посоветуешь, чтобы выполнить план?»")
        return _Block(verdict, bullets)

    def _hinders_block(
        self,
        c: _Ctx,
        bn: dict[str, Any] | None,
        losses: dict[str, Any] | None,
        line: str | None,
    ) -> _Block | None:
        if bn is None and losses is None:
            return None
        bullets: list[str] = []
        if line and bn is not None:
            share = bn["shares"][line]["sole"] + bn["shares"][line]["shifting"]
            verdict = (
                f"Больше всего выпуск ограничивает линия {self._name(line)}: она узкое место "
                f"в {tx.pct(share, 0)} смен ({c.label})."
            )
            shares = self._shares_text(bn)
            if shares:
                bullets.append(f"Доля смен, когда линия была узким местом: {shares}.")
            live = (bn.get("live") or {}).get("current")
            if live and NOW.search(c.q):
                verdict = f"Сейчас узкое место — линия {self._name(live)}. " + verdict
            elif live:
                bullets.append(f"Сейчас узкое место — {self._name(live)}.")
        else:
            verdict = f"Узкое место за период ({c.label}) не определено — мало данных."
        if losses is not None:
            cats = self._loss_cats(losses)
            t = losses.get("totals") or {}
            if cats:
                where = self._bn_where(losses, line)
                verdict += (
                    f" Главная потеря {where} — {tx.label(tx.LOSS_RU, cats[0]['category'])}, "
                    f"{tx.cars(cats[0].get('units'))}."
                )
                bullets.append(
                    "Потери: "
                    + "; ".join(self._cat_text(x) for x in cats[:4])
                    + f"; всего {tx.cars(t.get('units'))}, {tx.money(t.get('kzt'))}."
                )
            if line:
                if weights := self._weights_text(
                    losses, shares_shown=bool(self._shares_text(bn or {}))
                ):
                    bullets.append(weights)
                bullets.append(
                    "Голодание и блокировка остальных линий — следствие узкого места, "
                    "в потери выпуска не добавляются."
                )
        return _Block(verdict, bullets)

    async def _b_hinders(self, c: _Ctx) -> _Block | None:
        bn, losses, line, c = await self._bottleneck_losses(c)
        return self._hinders_block(c, bn, losses, line)

    async def _b_bottleneck(self, c: _Ctx) -> _Block | None:
        return await self._b_hinders(c)

    async def _b_why_drop(self, c: _Ctx) -> _Block | None:
        if not c.explicit:  # "почему упала сварка?" — about yesterday
            d = c.today - timedelta(days=1)
            c = c.at(d, d, f"вчера, {tx.ddmm(d)}")
        kind, code = c.e.ordered[0] if c.e.ordered else ("plant", None)
        level = {"area": "area", "line": "line", "equipment": "equipment"}.get(kind, "plant")
        args: dict[str, Any] = {"level": level, **c.period, "granularity": "day"}
        if code:
            args["code"] = code
        day = await c.call("get_kpi", args)
        if day is None:
            return None
        m0 = c.end.replace(day=1)
        month_args = {**args, "from": m0.isoformat(), "granularity": "month"}
        month = await c.call("get_kpi", month_args) if m0 < c.start else None
        key = code or self.cfg.plant.site.code
        d_row = self._rows(day).get(key) or next(iter(self._rows(day).values()), None)
        m_row = self._rows(month).get(key) if month else None
        if d_row is None:
            return _Block(f"Данных {self._of(code)} за период ({c.label}) нет.")
        bullets: list[str] = []
        metric = "availability" if level == "equipment" else "oee"
        for part, words in METRIC_WORDS.items():
            if level != "equipment" and words.search(c.q):
                metric = part
        v, ref = d_row.get(metric), (m_row or {}).get(metric)
        name = tx.KPI_PART_RU.get(metric, "OEE")
        name = name[:1].upper() + name[1:]
        if v is None:
            verdict = f"Показатели {self._of(code)} за период ({c.label}) не рассчитаны."
        elif ref is None:
            verdict = f"{name} {self._of(code)} ({c.label}) — {tx.pct(v)}."
        elif float(v) < float(ref) - 0.005:
            verdict = (
                f"{name} {self._of(code)} ({c.label}) — {tx.pct(v)}: на "
                f"{tx.pp(float(v) - float(ref))} ниже среднего с начала месяца ({tx.pct(ref)})."
            )
        else:
            verdict = (
                f"Просадки нет: {name} {self._of(code)} ({c.label}) — {tx.pct(v)}, "
                f"{'выше' if float(v) > float(ref) + 0.005 else 'на уровне'} среднего "
                f"с начала месяца ({tx.pct(ref)})."
            )
        if level != "equipment":
            drops = []
            for part, ru in tx.KPI_PART_RU.items():
                dv, mv = d_row.get(part), (m_row or {}).get(part)
                if dv is None:
                    continue
                text = f"{ru} {tx.pct(dv)}"
                if mv is not None:
                    text += f" (месяц {tx.pct(mv)})"
                    drops.append((float(dv) - float(mv), ru))
                bullets.append(text)
            if bullets:
                bullets = ["Составляющие: " + ", ".join(bullets) + "."]
            worst = min(drops, default=None)
            if metric == "oee" and worst is not None and worst[0] < -0.005:
                verdict += (
                    f" Сильнее всего просела составляющая «{worst[1]}» — на {tx.pp(worst[0])}."
                )
            if d_row.get("pq") is not None:
                bullets.append(
                    f"Выпуск {tx.num(d_row.get('pq'))} авто, годных {tx.num(d_row.get('gq'))}"
                    + (
                        f", брак {tx.pct(d_row.get('defect_rate'))}"
                        if d_row.get("defect_rate") is not None
                        else ""
                    )
                    + "."
                )
        else:
            bullets.append(
                f"Отказов {tx.num(d_row.get('failures') or 0)}, простой "
                f"{tx.minutes(d_row.get('adot'))}."
            )
        line = self._line_of(kind, code) if code else None
        if kind in ("area", "line") and code:
            losses = await c.call(
                "get_losses",
                {**c.period, "level": "area" if kind == "area" else "line", "code": code},
            )
        elif line:
            losses = await c.call("get_losses", {**c.period, "level": "line", "code": line})
        else:
            _bn, losses, line, _c = await self._bottleneck_losses(c)
        if losses is not None:
            cats = self._loss_cats(losses)
            if cats:
                if worst_cat := cats[0]:
                    verdict += (
                        f" Главная потеря — {tx.label(tx.LOSS_RU, worst_cat['category'])}, "
                        f"{tx.minutes(worst_cat.get('minutes'))} "
                        f"({tx.cars(worst_cat.get('units'))})."
                    )
                bullets.append("Потери: " + "; ".join(self._cat_text(x) for x in cats[:4]) + ".")
        dt_args: dict[str, Any] = {**c.period, "planned": False}
        if kind == "equipment" and code:
            dt_args["entity"] = code
        elif line:
            dt_args["line"] = line
        elif kind == "area" and code:
            dt_args["line"] = self._line_of("area", code)
        stops = await c.call("get_downtime", dt_args) if (code or line) else None
        if stops is not None:
            items = stops.get("items") or []
            if items:
                top = sorted(
                    self._dedup_stops(items), key=lambda i: -float(i.get("duration_s") or 0)
                )[:3]
                bullets.append(
                    "Простои: "
                    + "; ".join(
                        f"{self._full(i['entity'])} — «{self._why(i)}», "
                        f"{self._dur(i)}"
                        + (f" ({tx.hhmm(i['start_ts'], self.cfg)})" if i.get("start_ts") else "")
                        for i in top
                    )
                    + "."
                )
            else:
                bullets.append("Внеплановых простоев не было.")
        return _Block(verdict, bullets)

    async def _b_eq_rank(self, c: _Ctx) -> _Block | None:
        r = await c.call("get_kpi", {"level": "equipment", **c.period, "granularity": "month"})
        if r is None:
            return None
        lines = {self._line_of(k, code) for k, code in c.e.ordered if k in ("line", "area")}
        areas = set(c.e.areas)
        agg: dict[str, dict[str, float]] = {}
        for item in r.get("items") or []:
            code = item["code"]
            if (
                lines
                and self.cfg.line_of_equipment(code).code not in lines
                and self.cfg.area_of_equipment(code).code not in areas
            ):
                continue
            a = agg.setdefault(code, {"failures": 0.0, "down": 0.0, "mtbf": 0.0})
            a["failures"] += float(item.get("failures") or 0)
            a["down"] += float(item.get("adot") or 0)
            if item.get("mtbf_h") is not None:
                a["mtbf"] = float(item["mtbf_h"])
        by_failures = bool(EQ_BY_FAILURES.search(c.q))
        ranked = sorted(
            agg.items(),
            key=lambda kv: (
                (-kv[1]["failures"], -kv[1]["down"])
                if by_failures
                else (-kv[1]["down"], -kv[1]["failures"])
            ),
        )
        ranked = [kv for kv in ranked if kv[1]["failures"] > 0 or kv[1]["down"] >= 1]
        if not ranked:
            return _Block(f"За период ({c.label}) отказов и простоев оборудования не было.")

        def desc(code: str, a: dict[str, float]) -> str:
            n = int(a["failures"])
            text = (
                f"{n} {tx.plural(n, 'отказ', 'отказа', 'отказов')}, простой {tx.minutes(a['down'])}"
            )
            if a["mtbf"]:
                text += f", MTBF {tx.num(a['mtbf'], 1)} ч"
            return text

        code, a = ranked[0]
        if by_failures:
            verdict = f"Чаще всего ({c.label}) отказывает {self._full(code)}: {desc(code, a)}."
        else:
            verdict = f"Больше всего ({c.label}) простаивало {self._full(code)}: {desc(code, a)}."
        bullets = [f"{self._full(cd)} — {desc(cd, x)}" for cd, x in ranked[1:5]]
        if bullets:
            bullets.insert(0, "Дальше по списку:")
        return _Block(verdict, bullets)

    async def _b_stopped(self, c: _Ctx) -> _Block | None:
        kind, code = c.e.ordered[0] if c.e.ordered else ("plant", None)
        if kind == "equipment" and code:
            health = await c.call("get_equipment_health", {"code": code})
            recent = await c.call(
                "get_downtime",
                {
                    "entity": code,
                    "from": c.today.replace(day=1).isoformat(),
                    "to": c.today.isoformat(),
                },
            )
            return self._stopped_unit(code, health, recent)
        line = self._line_of(kind, code) if code else None
        args: dict[str, Any] = {"open_only": True}
        if line:
            args["line"] = line
        now = await c.call("get_downtime", args)
        if now is None:
            return None
        items = now.get("items") or []
        where = f"на линии {self._name(line)}" if line else "на заводе"
        if not items:
            verdict = f"Сейчас {where} открытых простоев нет — оборудование работает."
            if line:
                recent = await c.call(
                    "get_downtime",
                    {
                        "line": line,
                        "from": c.today.replace(day=1).isoformat(),
                        "to": c.today.isoformat(),
                    },
                )
                last = ((recent or {}).get("items") or [None])[0]
                if last:
                    verdict += " " + self._last_stop(last)
            return _Block(verdict)
        n = len(items)
        word = tx.plural(n, "открытый простой", "открытых простоя", "открытых простоев")
        verdict = f"Сейчас {where} {n} {word}."
        bullets = [
            f"{self._full(i['entity'])} — «{self._why(i)}», "
            f"с {tx.hhmm(i['start_ts'], self.cfg)}, идёт {self._dur(i)}"
            for i in items[:5]
        ]
        return _Block(verdict, bullets)

    def _last_stop(self, i: dict[str, Any]) -> str:
        return (
            f"Последний простой — {tx.when(i['start_ts'], self.cfg)}, "
            f"«{self._why(i)}», "
            f"{self._dur(i)}."
        )

    def _stopped_unit(
        self, code: str, health: dict[str, Any] | None, recent: dict[str, Any] | None
    ) -> _Block | None:
        if health is None and recent is None:
            return None
        items = (recent or {}).get("items") or []
        open_items = [i for i in items if i.get("open")]
        state = (health or {}).get("state")
        name = self._full(code)
        if open_items:
            i = open_items[0]
            verdict = (
                f"{name} стоит с {tx.hhmm(i['start_ts'], self.cfg)} "
                f"(идёт {self._dur(i)}): "
                f"«{self._why(i)}»."
            )
        elif state and str(state).startswith("DOWN"):
            verdict = (
                f"{name}: {tx.label(tx.STATE_RU, state)}"
                + (
                    f" с {tx.hhmm(health['since'], self.cfg)}"
                    if health and health.get("since")
                    else ""
                )
                + f", причина — «{self._reason((health or {}).get('reason_code'))}»."
            )
        else:
            verdict = (
                f"{name} сейчас не стоит: "
                f"{tx.label(tx.STATE_RU, state) if state else 'состояние неизвестно'}, "
                "открытых простоев нет."
            )
            closed = [i for i in items if not i.get("open")]
            if closed:
                verdict += " " + self._last_stop(closed[0])
        bullets = []
        rel = (health or {}).get("reliability_30d") or {}
        if rel:
            n = int(rel.get("failures") or 0)
            bullets.append(
                f"За 30 дней: {n} {tx.plural(n, 'отказ', 'отказа', 'отказов')}"
                + (f", MTBF {tx.num(rel['mtbf_h'], 1)} ч" if rel.get("mtbf_h") else "")
                + (f", MTTR {tx.minutes(rel['mttr_min'])}" if rel.get("mttr_min") else "")
                + "."
            )
        return _Block(verdict, bullets)

    async def _b_risk(self, c: _Ctx) -> _Block | None:
        pdm = await c.call("get_alerts", {"status": "open", "rule_id": "AL-M1"})
        lim = await c.call("get_alerts", {"status": "open", "rule_id": "AL-M2"})
        if pdm is None and lim is None:
            return None
        units = list(dict.fromkeys(i["entity"] for i in (pdm or {}).get("items") or []))
        limits = (lim or {}).get("items") or []
        bullets: list[str] = []
        risky: list[str] = []
        for code in units[:2]:
            h = (
                await c.call("get_equipment_health", {"code": code})
                if code in self.cfg.equipment
                else None
            )
            pred = (h or {}).get("prediction")
            if pred:
                risky.append(
                    f"{self._full(code)} — {tx.pct(pred['p_failure'], 0)} "
                    f"за {tx.num(pred['horizon_h'])} ч"
                )
                text = (
                    f"{self._full(code)}: вероятность отказа {tx.pct(pred['p_failure'], 0)} "
                    f"за {tx.num(pred['horizon_h'])} ч, "
                    f"индекс здоровья {tx.num(pred.get('health_index'))}"
                )
                if pred.get("factors"):
                    text += "; причины: " + "; ".join(pred["factors"][:2])
                bullets.append(text + ".")
            else:
                risky.append(self._full(code))
        for code in units[2:5]:
            risky.append(self._full(code))
        for item in limits[:3]:
            bullets.append(
                f"{tx.label(tx.SEVERITY_RU, item['severity']).capitalize()}: {item['message_ru']}"
            )
        if risky:
            n = len(units)
            verdict = (
                f"В зоне риска {n} {tx.plural(n, 'единица', 'единицы', 'единиц')} оборудования: "
                + ", ".join(risky)
                + ". Рекомендация — обслужить в ближайшую пересменку."
            )
        elif limits:
            verdict = (
                "Прогнозов отказа нет, но есть сигналы, которые скоро дойдут до предела "
                f"({len(limits)})."
            )
        else:
            verdict = (
                "Сейчас оборудования в зоне риска нет: открытых прогнозов отказа "
                "и предупреждений о пределах нет."
            )
            mtd = c.at(c.today.replace(day=1), c.today, "с начала месяца", q="отказ")
            rank = await self._b_eq_rank(mtd)
            if rank is not None and rank.verdict.startswith("Чаще"):
                bullets.append(rank.verdict)
        return _Block(verdict, bullets)

    def _limited_units(self, c: _Ctx) -> list[str]:
        """Units with a signal that has a hard limit (time-to-limit forecast), in plant order;
        a question about filters keeps the units whose limited signal is about filters."""
        filters = bool(re.search(r"\bфильтр", c.q))
        units = []
        for code, eq in self.cfg.equipment.items():
            kind = self.cfg.equipment_types.get(eq.type)
            signals = [sg for sg in (kind.signals if kind else []) if sg.limit_hi is not None]
            if filters:
                signals = [sg for sg in signals if "фильтр" in normalize_text(sg.name_ru)]
            if signals:
                units.append(code)
        lines = {self._line_of(k, cd) for k, cd in c.e.ordered if k in ("line", "area")}
        if lines:
            units = [u for u in units if self.cfg.line_of_equipment(u).code in lines]
        return units

    async def _b_limits(self, c: _Ctx) -> _Block | None:
        found: list[tuple[float, str, dict[str, Any]]] = []
        for code in self._limited_units(c)[:5]:
            h = await c.call("get_equipment_health", {"code": code})
            for lim in (h or {}).get("limits") or []:
                hours = lim.get("hours_to_limit")
                found.append((float(hours) if hours is not None else 1e9, code, lim))
        if not c.st.calls:
            return None
        found.sort(key=lambda x: x[0])
        soon = [x for x in found if x[0] < 1e9]
        if not soon:
            return _Block("Ни один контролируемый сигнал не идёт к пределу: обслуживание по плану.")
        hours, code, lim = soon[0]
        verdict = (
            f"Раньше всего предела достигнет {self._full(code)}: "
            f"{str(lim.get('signal_name_ru') or 'сигнал').lower()} — через ≈ {tx.num(hours, 1)} ч"
            + (f" (около {tx.hhmm(lim['limit_at'], self.cfg)})" if lim.get("limit_at") else "")
            + "."
        )
        if lim.get("alert"):
            verdict += " Рекомендация — обслужить в ближайшую пересменку или сейчас."
        bullets = [str(x[2]["text_ru"]) for x in soon[:3] if x[2].get("text_ru")]
        return _Block(verdict, bullets)

    async def _b_health(self, c: _Ctx) -> _Block | None:
        if not c.e.equipment:
            return await self._b_risk(c)
        blocks = []
        for code in c.e.equipment[:2]:
            r = await c.call("get_equipment_health", {"code": code})
            if r is not None:
                blocks.append(self._health_block(code, r))
        if not blocks:
            return None
        return _Block(" ".join(b.verdict for b in blocks), [x for b in blocks for x in b.bullets])

    def _health_block(self, code: str, r: dict[str, Any]) -> _Block:
        name = self._full(code)
        pred = r.get("prediction")
        state = tx.label(tx.STATE_RU, r.get("state")) if r.get("state") else "состояние неизвестно"
        th = self.cfg.rules.thresholds
        if pred:
            p = float(pred["p_failure"])
            level = (
                "высокий риск отказа"
                if p >= th.pdm_crit_p
                else "повышенный риск отказа"
                if p >= th.pdm_warn_p
                else "риск отказа низкий"
            )
            verdict = (
                f"{name}: {level} — {tx.pct(p, 0)} в ближайшие {tx.num(pred['horizon_h'])} ч "
                f"(индекс здоровья {tx.num(pred.get('health_index'))}), сейчас {state}."
            )
            if p >= th.pdm_warn_p:
                verdict += " Рекомендация — обслужить в ближайшую пересменку."
        else:
            verdict = f"{name} сейчас {state}; прогноз отказа пока не рассчитан."
        bullets = []
        if pred and pred.get("factors"):
            bullets.append("Причины: " + "; ".join(pred["factors"][:3]) + ".")
        for lim in r.get("limits") or []:
            if lim.get("alert") and lim.get("text_ru"):
                bullets.append(str(lim["text_ru"]))
        rel = r.get("reliability_30d") or {}
        if rel:
            n = int(rel.get("failures") or 0)
            bullets.append(
                f"За 30 дней: {n} {tx.plural(n, 'отказ', 'отказа', 'отказов')}"
                + (f", MTBF {tx.num(rel['mtbf_h'], 1)} ч" if rel.get("mtbf_h") else "")
                + "."
            )
        return _Block(verdict, bullets)

    async def _b_losses(self, c: _Ctx) -> _Block | None:
        line: str | None = None
        if c.e.lines or c.e.areas:
            kind, code = next((k, cd) for k, cd in c.e.ordered if k in ("line", "area"))
            r = await c.call("get_losses", {**c.period, "level": kind, "code": code})
            where = f"«{self._name(code)}»"
        elif c.e.equipment:
            line = self.cfg.line_of_equipment(c.e.equipment[0]).code
            r = await c.call("get_losses", {**c.period, "level": "line", "code": line})
            where = f"линии {self._name(line)}"
        else:
            _bn, r, line, c = await self._bottleneck_losses(c)
            where = self._bn_where(r or {}, line)
        if r is None:
            return None
        t = r.get("totals") or {}
        cats = self._loss_cats(r)
        if not cats:
            return _Block(f"Потерь {where} за период ({c.label}) нет.")
        verdict = (
            f"Потери {where} ({c.label}): {tx.cars(t.get('units'))}, {tx.money(t.get('kzt'))} "
            f"({tx.minutes(t.get('minutes'))})."
        )
        if DOWNTIME_FOCUS.search(c.q):
            stop_cats = [x for x in cats if x["category"] not in ("speed", "quality")]
            units = sum(float(x.get("units") or 0) for x in stop_cats)
            mins = sum(float(x.get("minutes") or 0) for x in stop_cats)
            verdict = (
                f"Из-за простоев ({c.label}) {where} потеряно {tx.cars(units)} "
                f"({tx.minutes(mins)}); всего потерь — {tx.cars(t.get('units'))}, "
                f"{tx.money(t.get('kzt'))}."
            )
        else:
            verdict += f" Больше всего — {tx.label(tx.LOSS_RU, cats[0]['category'])}."
        bullets = [self._cat_text(x) for x in cats[:4]]
        if line and not (c.e.lines or c.e.areas or c.e.equipment):
            if weights := self._weights_text(r):
                bullets.append(weights)
            bullets.append(
                "Считаем на узком месте: голодание и блокировка остальных линий — его следствие."
            )
        bullets.append("Перевод в ₸ — по марже на авто (допущение).")
        return _Block(verdict, bullets)

    async def _b_downtime(self, c: _Ctx) -> _Block | None:
        if not c.explicit and BROKE.search(c.q):
            c = c.at(c.today, c.today, f"сегодня, {tx.ddmm(c.today)}")
        args: dict[str, Any] = dict(c.period)
        if BROKE.search(c.q):
            args["planned"] = False
        kind, code = c.e.ordered[0] if c.e.ordered else ("plant", None)
        if kind == "equipment" and code:
            args["entity"] = code
        elif code:
            line = self._line_of(kind, code)
            if line:
                args["line"] = line
        r = await c.call("get_downtime", args)
        if r is None:
            return None
        items = r.get("items") or []
        where = f" — {self._full(code)}" if code else ""
        if not items:
            return _Block(f"Простоев за период ({c.label}){where} не было.")
        total = sum(float(i.get("duration_s") or 0) for i in items) / 60.0
        by_reason: dict[str, float] = {}
        for i in items:
            key = i.get("reason_name_ru") or self._reason(i.get("reason_code"))
            by_reason[key] = by_reason.get(key, 0.0) + float(i.get("duration_s") or 0) / 60.0
        top_reason, top_min = max(by_reason.items(), key=lambda kv: kv[1])
        n = len(items)
        more = " (показаны последние)" if r.get("more") else ""
        verdict = (
            f"Простоев за период ({c.label}){where}: {n}{more}, всего {tx.minutes(total)}; "
            f"больше всего — «{top_reason}» ({tx.minutes(top_min)})."
        )
        bullets = []
        for i in items[:5]:
            state = ", идёт" if i.get("open") else ""
            bullets.append(
                f"{tx.when(i['start_ts'], self.cfg)} {self._full(i['entity'])} — "
                f"«{self._why(i)}», "
                f"{self._dur(i)}{state}"
            )
        return _Block(verdict, bullets)

    async def _b_defects(self, c: _Ctx) -> _Block | None:
        args: dict[str, Any] = dict(c.period)
        lines, areas = c.e.lines, c.e.areas
        if lines:
            args["line"] = lines[0]
        elif areas:
            args["area"] = areas[0]
        r = await c.call("get_defects", args)
        if r is None:
            return None
        p = r["pareto"]
        where = f" {self._on(lines[0] if lines else areas[0])}" if lines or areas else ""
        if not p.get("total"):
            return _Block(f"Брака{where} за период ({c.label}) не было.")
        items = p.get("items") or []
        names = {i["defect_code"]: i.get("name_ru") or "дефект" for i in items}
        top = items[0]
        verdict = (
            f"Брак{where} ({c.label}): {tx.num(p['total'])} шт.; чаще всего — "
            f"«{top.get('name_ru') or 'дефект'}» ({tx.num(top['qty'])}, {tx.pct(top['share'], 0)})."
        )
        bullets = [
            f"{i.get('name_ru') or 'дефект'} — {tx.num(i['qty'])} ({tx.pct(i['share'], 0)})"
            for i in items[1:4]
        ]
        vital = [names.get(code, "дефект") for code in p.get("vital_few") or []]
        if vital:
            bullets.append("80 % брака дают: " + ", ".join(vital) + ".")
        return _Block(verdict, bullets)

    async def _b_alerts(self, c: _Ctx) -> _Block | None:
        args: dict[str, Any] = {"status": "open"}
        if c.e.equipment:
            args["entity"] = c.e.equipment[0]
        r = await c.call("get_alerts", args)
        if r is None:
            return None
        items = r.get("items") or []
        counts = r.get("open_counts") or {}
        if not items:
            return _Block("Открытых оповещений нет.")
        total = sum(int(v) for v in counts.values()) or len(items)
        crit, warn, info = (int(counts.get(k, 0)) for k in ("critical", "warning", "info"))
        verdict = (
            f"Открыто {total} {tx.plural(total, 'оповещение', 'оповещения', 'оповещений')}: "
            f"критичных {crit}, предупреждений {warn}, информационных {info}."
        )
        order = {"critical": 0, "warning": 1, "info": 2}
        ranked = sorted(items, key=lambda i: order.get(i.get("severity", "info"), 3))
        bullets = [
            f"{tx.label(tx.SEVERITY_RU, i['severity']).capitalize()}: {i['message_ru']}"
            for i in ranked[:5]
        ]
        return _Block(verdict, bullets)

    async def _b_kpi(self, c: _Ctx) -> _Block | None:
        granularity = "day" if c.start == c.end else "month"
        groups: dict[str, list[str]] = {}
        for kind, code in c.e.ordered:
            groups.setdefault(kind, []).append(code)
        if not groups:
            r = await c.call("get_kpi", {"level": "line", **c.period, "granularity": granularity})
            if r is None:
                return None
            return self._kpi_plant(c, self._rows(r))
        rows: list[tuple[str, str, dict[str, Any]]] = []
        for kind, codes in list(groups.items())[:3]:
            args: dict[str, Any] = {"level": kind, **c.period, "granularity": granularity}
            if len(codes) == 1:
                args["code"] = codes[0]
            r = await c.call("get_kpi", args)
            got = self._rows(r)
            rows += [(kind, code, got[code]) for code in codes if code in got]
        if not rows:
            if c.st.calls:
                return _Block(f"Данных за период ({c.label}) нет.")
            return None
        oee_rows = [(k, cd, row) for k, cd, row in rows if row.get("oee") is not None]
        bullets = [self._kpi_line(k, cd, row) for k, cd, row in rows]
        if len(oee_rows) >= 2:
            ranked = sorted(oee_rows, key=lambda x: -float(x[2]["oee"]))
            best, worst = ranked[0], ranked[-1]
            diff = float(best[2]["oee"]) - float(worst[2]["oee"])
            verdict = (
                f"OEE ({c.label}) выше у {self._of(best[1])} — {tx.pct(best[2]['oee'])}, "
                f"ниже у {self._of(worst[1])} — {tx.pct(worst[2]['oee'])}; "
                f"разница {tx.pp(diff)}."
            )
            weak = self._weakest(worst[2])
            if weak:
                verdict += f" У {self._of(worst[1])} слабее всего {weak}."
            return _Block(verdict, bullets)
        kind, code, row = rows[0]
        if kind == "equipment":
            n = int(row.get("failures") or 0)
            verdict = (
                f"{self._full(code)}, {c.label}: доступность {tx.pct(row.get('availability'))}, "
                f"{n} {tx.plural(n, 'отказ', 'отказа', 'отказов')}, "
                f"простой {tx.minutes(row.get('adot'))}."
            )
            return _Block(verdict, bullets[1:])
        verdict = f"OEE {self._of(code)} ({c.label}) — {tx.pct(row.get('oee'))}"
        weak = self._weakest(row)
        if weak:
            verdict += f"; слабее всего {weak}"
        if row.get("pq") is not None:
            verdict += f"; выпуск {tx.num(row.get('pq'))} авто"
        return _Block(verdict + ".", bullets)

    def _kpi_line(self, kind: str, code: str, k: dict[str, Any]) -> str:
        if kind == "equipment":
            n = int(k.get("failures") or 0)
            return (
                f"{self._full(code)}: доступность {tx.pct(k.get('availability'))}, "
                f"{n} {tx.plural(n, 'отказ', 'отказа', 'отказов')}"
                + (f", MTBF {tx.num(k['mtbf_h'], 1)} ч" if k.get("mtbf_h") else "")
            )
        text = (
            f"{self._name(code)}: OEE {tx.pct(k.get('oee'))} (доступность "
            f"{tx.pct(k.get('availability'))}, эффективность {tx.pct(k.get('effectiveness'))}, "
            f"качество {tx.pct(k.get('quality_ratio'))})"
        )
        if k.get("pq") is not None:
            text += f"; выпуск {tx.num(k.get('pq'))}, годных {tx.num(k.get('gq'))}"
        return text

    def _kpi_plant(self, c: _Ctx, rows: dict[str, dict[str, Any]]) -> _Block | None:
        if not rows:
            return _Block(f"Данных за период ({c.label}) нет.")
        out_line = self.cfg.flow_lines[-1]
        out = rows.get(out_line) or {}
        with_oee = [(code, r) for code, r in rows.items() if r.get("oee") is not None]
        verdict = f"Выпуск завода ({c.label}) — {tx.num(out.get('pq') or 0)} авто"
        if with_oee:
            code, low = min(with_oee, key=lambda x: float(x[1]["oee"]))
            verdict += f"; самый низкий OEE у {self._line_gen(code)} — {tx.pct(low['oee'])}"
        verdict += "."
        bullets = [self._kpi_line("line", code, r) for code, r in rows.items()]
        return _Block(verdict, bullets)
