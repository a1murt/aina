"""Template shift report without an LLM (SPEC §11.5: mandatory, offline demo).

Russian and Kazakh; the Kazakh wording is a DRAFT for native review (:data:`KK_DRAFT`). Every
number, date and time comes verbatim from the :class:`ShiftInput`, so the text always passes
the number check. Four sections — «Итоги», «Отклонения», «Причины», «Задачи на следующую смену»
(kk: «Қорытынды», «Ауытқулар», «Себептер», «Келесі ауысымға міндеттер») — at most
:data:`MAX_WORDS` words: lists are shortened until the text fits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from twin_core.report.model import Deviation, Lang, ShiftInput, StopRow

MAX_WORDS = 250
KK_DRAFT = True
"""Kazakh texts are a draft translation; check with a native speaker before Demo Day."""

SECTIONS: dict[Lang, tuple[str, str, str, str]] = {
    "ru": ("Итоги", "Отклонения", "Причины", "Задачи на следующую смену"),
    "kk": ("Қорытынды", "Ауытқулар", "Себептер", "Келесі ауысымға міндеттер"),
}


def word_count(text: str) -> int:
    """Words as a reader counts them: whitespace-separated tokens with a letter or digit."""
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


def fmt_num(value: float | int, digits: int = 1) -> str:
    """Report number: integers as is (no-break thousands), decimals with a comma."""
    if isinstance(value, int) or (float(value).is_integer() and digits == 0):
        n = round(value)
        text = f"{abs(n):,}".replace(",", " ")
        return f"−{text}" if n < 0 else text
    return f"{value:.{digits}f}".replace(".", ",")


def _p(value: float | None) -> str:
    return "—" if value is None else f"{fmt_num(value)}%"


def _d(day_iso: str) -> str:
    y, m, d = day_iso.split("-")
    return f"{d}.{m}.{y}"


@dataclass(frozen=True, slots=True)
class _Limits:
    devs: int = 5
    stops: int = 3
    losses: int = 3
    defects: int = 3
    tasks: int = 5
    lines: int = 6


@dataclass(frozen=True, slots=True)
class _Phrases:
    title: Callable[[ShiftInput], str]
    not_closed: str
    no_kpi: str
    output: Callable[[ShiftInput], str]
    oee: str
    target: str
    defects: str
    norm: str
    bottleneck: Callable[[str, str], str]
    forecast: Callable[[ShiftInput], str]
    dev: Callable[[Deviation], str]
    no_devs: str
    stop: Callable[[StopRow], str]
    losses: Callable[[str, str], str]
    defect: Callable[[str, str, str], str]
    microstops: Callable[[str, str], str]
    no_causes: str
    tasks_head: Callable[[ShiftInput], str]
    task_stop: Callable[[StopRow], str]
    task_defect: Callable[[str, str], str]
    task_oee: Callable[[str, str], str]
    task_rate: Callable[[str], str]
    task_alerts: Callable[[str], str]
    task_bottleneck: Callable[[str], str]
    task_default: Callable[[str], str]


def _ru_title(i: ShiftInput) -> str:
    s = i.shift
    return (
        f"Сменный рапорт: {i.site}, {_d(s.date.isoformat())}, смена {s.code} "
        f"({s.start_local}–{s.end_local})"
    )


def _ru_output(i: ShiftInput) -> str:
    t = i.totals
    if t.plan is None:
        return f"Выпуск {fmt_num(t.output or 0)} авто (нерабочая смена)."
    out = fmt_num(t.output or 0)
    return f"Выпуск {out} авто при плане {fmt_num(t.plan)} ({_p(t.attainment_pct)})."


def _ru_forecast(i: ShiftInput) -> str:
    f = i.forecast
    assert f is not None
    text = f"Прогноз месяца: {fmt_num(f.p50)} авто (P10–P90: {fmt_num(f.p10)}–{fmt_num(f.p90)})"
    if f.plan is not None and f.p_plan_pct is not None:
        text += f"; вероятность выполнить план {fmt_num(f.plan)} — {_p(f.p_plan_pct)}"
    return text + "."


def _ru_dev(d: Deviation) -> str:
    if d.kind == "oee":
        return f"{d.name}: OEE {_p(d.value)} при цели {_p(d.limit)}."
    if d.kind == "defect_rate":
        return f"{d.name}: брак {_p(d.value)} при норме {_p(d.limit)}."
    if d.kind == "plan":
        return f"Выпуск ниже плана на {fmt_num(int(d.gap or 0))} авто."
    return f"Риск плана месяца: вероятность {_p(d.value)} (порог {_p(d.limit)})."


def _ru_stop(s: StopRow) -> str:
    kind = "плановый простой" if s.planned else s.reason.lower()
    if s.open:
        return f"{s.name}, {s.line_name}: {kind}, с {s.start_local}, {s.minutes} мин, не устранено."
    return f"{s.name}, {s.line_name}: {kind}, {s.minutes} мин ({s.start_local}–{s.end_local})."


def _ru_tasks_head(i: ShiftInput) -> str:
    n = i.next_shift
    if n is None:
        return SECTIONS["ru"][3]
    when = n.start_local if n.date == i.shift.date else f"{_d(n.date.isoformat())} {n.start_local}"
    return f"{SECTIONS['ru'][3]} (смена {n.code}, {when})"


RU = _Phrases(
    title=_ru_title,
    not_closed="Смена не закрыта: данные предварительные.",
    no_kpi="Данных KPI за смену нет.",
    output=_ru_output,
    oee="OEE",
    target="цель",
    defects="Брак",
    norm="норма",
    bottleneck=lambda name, share: f"Узкое место — {name} ({share} смены).",
    forecast=_ru_forecast,
    dev=_ru_dev,
    no_devs="Отклонений от целей нет.",
    stop=_ru_stop,
    losses=lambda line, items: f"Потери на линии {line}: {items}.",
    defect=lambda name, area, qty: f"Брак ({area}): {name.lower()} — {qty} шт.",
    microstops=lambda n, m: f"Микроостановки: {n}, всего {m} мин.",
    no_causes="Существенных простоев и брака не было.",
    tasks_head=_ru_tasks_head,
    task_stop=lambda s: f"{s.name}: проверить и устранить причину — {s.reason.lower()}.",
    task_defect=lambda area, name: f"Участок «{area}»: разобрать брак — {name.lower()}.",
    task_oee=lambda line, loss: f"{line}: поднять OEE, главный источник потерь — {loss}.",
    task_rate=lambda rate: f"Держать темп не ниже {rate} авто за смену (план месяца).",
    task_alerts=lambda n: f"Разобрать открытые оповещения: {n}.",
    task_bottleneck=lambda name: f"Не допускать простоев узкого места ({name}).",
    task_default=lambda target: f"Работать по плану, держать OEE не ниже {target}.",
)


def _kk_title(i: ShiftInput) -> str:
    s = i.shift
    return (
        f"Ауысым есебі: {i.site}, {_d(s.date.isoformat())}, {s.code} ауысымы "
        f"({s.start_local}–{s.end_local})"
    )


def _kk_output(i: ShiftInput) -> str:
    t = i.totals
    if t.plan is None:
        return f"Шығарылым: {fmt_num(t.output or 0)} автокөлік (жұмыс емес ауысым)."
    return (
        f"Шығарылым: {fmt_num(t.output or 0)} автокөлік, жоспар {fmt_num(t.plan)} "
        f"({_p(t.attainment_pct)})."
    )


def _kk_forecast(i: ShiftInput) -> str:
    f = i.forecast
    assert f is not None
    text = f"Ай болжамы: {fmt_num(f.p50)} автокөлік (P10–P90: {fmt_num(f.p10)}–{fmt_num(f.p90)})"
    if f.plan is not None and f.p_plan_pct is not None:
        text += f"; {fmt_num(f.plan)} жоспарын орындау ықтималдығы — {_p(f.p_plan_pct)}"
    return text + "."


def _kk_dev(d: Deviation) -> str:
    if d.kind == "oee":
        return f"{d.name}: OEE {_p(d.value)}, мақсат {_p(d.limit)}."
    if d.kind == "defect_rate":
        return f"{d.name}: ақау {_p(d.value)}, норма {_p(d.limit)}."
    if d.kind == "plan":
        return f"Шығарылым жоспардан {fmt_num(int(d.gap or 0))} автокөлікке аз."
    return f"Ай жоспарының тәуекелі: ықтималдық {_p(d.value)} (шек {_p(d.limit)})."


def _kk_stop(s: StopRow) -> str:
    kind = "жоспарлы тоқтау" if s.planned else s.reason.lower()
    if s.open:
        return (
            f"{s.name}, {s.line_name}: {kind}, {s.start_local} бастап, {s.minutes} мин, жойылмаған."
        )
    return f"{s.name}, {s.line_name}: {kind}, {s.minutes} мин ({s.start_local}–{s.end_local})."


def _kk_tasks_head(i: ShiftInput) -> str:
    n = i.next_shift
    if n is None:
        return SECTIONS["kk"][3]
    when = n.start_local if n.date == i.shift.date else f"{_d(n.date.isoformat())} {n.start_local}"
    return f"{SECTIONS['kk'][3]} ({n.code} ауысымы, {when})"


KK = _Phrases(
    title=_kk_title,
    not_closed="Ауысым жабылмаған: деректер алдын ала.",
    no_kpi="Ауысым бойынша KPI деректері жоқ.",
    output=_kk_output,
    oee="OEE",
    target="мақсат",
    defects="Ақау",
    norm="норма",
    bottleneck=lambda name, share: f"Тар орын — {name} (ауысымның {share}).",
    forecast=_kk_forecast,
    dev=_kk_dev,
    no_devs="Мақсаттардан ауытқу жоқ.",
    stop=_kk_stop,
    losses=lambda line, items: f"{line} шығындары: {items}.",
    defect=lambda name, area, qty: f"{area} ақауы: {name.lower()} — {qty} дана.",
    microstops=lambda n, m: f"Микротоқтаулар: {n}, барлығы {m} мин.",
    no_causes="Елеулі тоқтаулар мен ақау болған жоқ.",
    tasks_head=_kk_tasks_head,
    task_stop=lambda s: f"{s.name}: себебін тексеру және жою — {s.reason.lower()}.",
    task_defect=lambda area, name: f"«{area}» учаскесі: ақауды талдау — {name.lower()}.",
    task_oee=lambda line, loss: f"{line}: OEE арттыру, негізгі шығын көзі — {loss}.",
    task_rate=lambda rate: (
        f"Қарқынды ауысымына кемінде {rate} автокөлік деңгейінде ұстау (ай жоспары)."
    ),
    task_alerts=lambda n: f"Ашық хабарламаларды талдау: {n}.",
    task_bottleneck=lambda name: f"Тар орынның ({name}) тоқтауына жол бермеу.",
    task_default=lambda target: f"Жоспар бойынша жұмыс, OEE кемінде {target}.",
)

PHRASES: dict[Lang, _Phrases] = {"ru": RU, "kk": KK}


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _render(i: ShiftInput, ph: _Phrases, lim: _Limits) -> str:
    heads = SECTIONS[i.lang]
    out = [ph.title(i), ""]

    # ----- Итоги
    summary: list[str] = []
    if not i.closed and i.source != "none":
        summary.append(ph.not_closed)
    if not i.lines:
        summary.append(ph.no_kpi)
    else:
        summary.append(ph.output(i))
        oee = ", ".join(f"{ln.name} {_p(ln.oee_pct)}" for ln in i.lines[: lim.lines])
        summary.append(f"{ph.oee}: {oee} ({ph.target} {_p(i.thresholds.oee_target_pct)}).")
        rates = ", ".join(f"{q.name} {_p(q.defect_rate_pct)}" for q in i.quality[: lim.lines])
        if rates:
            summary.append(
                f"{ph.defects}: {rates} ({ph.norm} {_p(i.thresholds.defect_rate_limit_pct)})."
            )
    if i.bottleneck:
        top = i.bottleneck[0]
        summary.append(ph.bottleneck(top.name, _p(top.sole_pct)))
    if i.forecast is not None:
        summary.append(ph.forecast(i))
    out += [heads[0], " ".join(summary), ""]

    # ----- Отклонения
    devs = [ph.dev(d) for d in i.deviations[: lim.devs]]
    out += [heads[1], *(_bullets(devs) if devs else [ph.no_devs]), ""]

    # ----- Причины
    causes = [ph.stop(s) for s in i.stops[: lim.stops]]
    worst = _worst_line(i)
    if worst is not None and lim.losses > 0:
        items = [
            f"{loss.name} {loss.minutes} мин (≈ {fmt_num(loss.cars)} авто)"
            if i.lang == "ru"
            else f"{loss.name} {loss.minutes} мин (≈ {fmt_num(loss.cars)} автокөлік)"
            for loss in i.losses
            if loss.line == worst
        ][: lim.losses]
        if items:
            name = next(ln.name for ln in i.lines if ln.code == worst)
            causes.append(ph.losses(name, ", ".join(items)))
    for d in i.defects[: lim.defects]:
        causes.append(ph.defect(d.name, d.area_name, fmt_num(d.qty)))
    if i.totals.microstops and lim.stops > 1:
        causes.append(ph.microstops(fmt_num(i.totals.microstops), fmt_num(i.totals.microstop_min)))
    out += [heads[2], *(_bullets(causes) if causes else [ph.no_causes]), ""]

    # ----- Задачи
    out += [ph.tasks_head(i), *_bullets(_tasks(i, ph)[: lim.tasks])]
    return "\n".join(out).strip() + "\n"


def _worst_line(i: ShiftInput) -> str | None:
    """The line to explain: the lowest OEE among lines below target, else the lowest OEE."""
    rated = [ln for ln in i.lines if ln.oee_pct is not None]
    if not rated:
        return None
    return min(rated, key=lambda ln: (ln.oee_pct or 0.0, ln.code)).code


def _tasks(i: ShiftInput, ph: _Phrases) -> list[str]:
    tasks: list[str] = []
    unplanned = [s for s in i.stops if not s.planned]
    seen: set[str] = set()
    for s in unplanned[:2]:
        if s.equipment not in seen:
            tasks.append(ph.task_stop(s))
            seen.add(s.equipment)
    for d in i.deviations:
        if d.kind == "defect_rate":
            top = next((x for x in i.defects if x.area == d.entity), None)
            if top is not None:
                tasks.append(ph.task_defect(d.name, top.name))
    for d in i.deviations:
        if d.kind == "oee":
            loss = next(
                (x for x in i.losses if x.line == d.entity and x.category != "planned_downtime"),
                None,
            )
            if loss is not None:
                tasks.append(ph.task_oee(d.name, loss.name))
                break
    f = i.forecast
    if (
        f is not None
        and f.required_rate_plan is not None
        and (any(d.kind in ("plan", "forecast") for d in i.deviations))
    ):
        tasks.append(ph.task_rate(fmt_num(f.required_rate_plan)))
    if i.totals.open_alerts:
        tasks.append(ph.task_alerts(fmt_num(i.totals.open_alerts)))
    if i.bottleneck:
        tasks.append(ph.task_bottleneck(i.bottleneck[0].name))
    if not tasks:
        tasks.append(ph.task_default(_p(i.thresholds.oee_target_pct)))
    return tasks


_SHRINK = (
    _Limits(),
    _Limits(devs=4, stops=3, losses=2, defects=2, tasks=4),
    _Limits(devs=3, stops=2, losses=2, defects=2, tasks=3),
    _Limits(devs=2, stops=2, losses=1, defects=1, tasks=3, lines=4),
    _Limits(devs=2, stops=1, losses=0, defects=1, tasks=2, lines=4),
    _Limits(devs=1, stops=1, losses=0, defects=0, tasks=1, lines=3),
)


def render_template(data: ShiftInput) -> str:
    """The template report in ``data.lang`` (≤ :data:`MAX_WORDS` words)."""
    phrases = PHRASES[data.lang]
    text = ""
    for limits in _SHRINK:
        text = _render(data, phrases, limits)
        if word_count(text) <= MAX_WORDS:
            return text
    return text


def has_sections(text: str, lang: Lang) -> bool:
    """All four section headings, each at the start of a line, in order."""
    lines = [line.strip().strip("*#:").strip() for line in text.splitlines()]
    pos = -1
    for head in SECTIONS[lang]:
        idx = next((n for n, line in enumerate(lines) if n > pos and line.startswith(head)), None)
        if idx is None:
            return False
        pos = idx
    return True


__all__ = [
    "KK_DRAFT",
    "MAX_WORDS",
    "SECTIONS",
    "fmt_num",
    "has_sections",
    "render_template",
    "word_count",
]
