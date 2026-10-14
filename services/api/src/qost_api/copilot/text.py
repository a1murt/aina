"""Russian wording of the offline copilot: numbers, dates, durations and labels of API keys.

Numbers use a decimal comma and no-break spaces between thousands and before units
(``2 525``, ``88,8 %``); dates are ``dd.MM``; times are plant-local ``HH:MM``. Labels map the
keys of API payloads (loss categories, targets, severities, unit states, lever kinds) to the
words the UI uses, so an answer never shows a raw key.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from twin_core.clock import to_plant_tz
from twin_core.config import TwinConfig

NBSP = " "
MINUS = "−"

LOSS_RU: dict[str, str] = {
    "planned_downtime": "плановый простой",
    "unplanned_downtime": "внеплановый простой",
    "starved": "голодание (нет кузовов на входе)",
    "blocked": "блокировка (выход занят)",
    "changeover": "переналадка",
    "microstops": "микроостановки",
    "speed": "потеря скорости",
    "quality": "брак",
}
TARGET_RU: dict[str, str] = {"plant_target": "цель завода", "line_plan": "план линии"}
SEVERITY_RU: dict[str, str] = {
    "critical": "критично",
    "warning": "предупреждение",
    "info": "информация",
}
STATE_RU: dict[str, str] = {
    "RUNNING": "работает",
    "DEGRADED": "работает в деградированном режиме",
    "STARVED": "голодание — нет входа",
    "BLOCKED": "блокировка — выход занят",
    "DOWN_UNPLANNED": "внеплановый простой",
    "DOWN_PLANNED": "плановое ТО",
    "CHANGEOVER": "переналадка",
    "IDLE_NO_PLAN": "вне смены",
}
KPI_PART_RU: dict[str, str] = {
    "availability": "доступность",
    "effectiveness": "эффективность",
    "quality_ratio": "качество",
}


def label(table: dict[str, str], key: Any) -> str:
    """Russian label of an API key; an unknown key becomes a neutral word, never the raw key."""
    return table.get(str(key), "прочее")


def num(value: float | int | None, digits: int = 0) -> str:
    """``2525`` -> ``2 525``, ``1387.24`` (1) -> ``1 387,2``; no-break thousands, comma."""
    if value is None:
        return "—"
    v = float(value)
    text = f"{abs(v):,.{digits}f}".replace(",", NBSP).replace(".", ",")
    if digits and "," in text:
        text = text.rstrip("0").rstrip(",")
    if text in ("0", ""):
        return "0"
    return f"{MINUS}{text}" if v < 0 else text


def pct(ratio: float | None, digits: int = 1) -> str:
    """Ratio as a percent: ``0.8883`` -> ``88,8 %``."""
    if ratio is None:
        return "—"
    return f"{num(float(ratio) * 100, digits)}{NBSP}%"


def pp(delta: float) -> str:
    """Difference of two ratios in percentage points: ``-0.063`` -> ``6,3 п.п.``."""
    return f"{num(abs(delta) * 100, 1)}{NBSP}п.п."


def cars(units: float | None) -> str:
    if units is None:
        return "—"
    digits = 0 if abs(units) >= 10 else 1
    return f"≈{NBSP}{num(units, digits)}{NBSP}авто"


def money(kzt: float | None) -> str:
    """Tenge in words of scale: ``69 691 673`` -> ``≈ 69,7 млн ₸``."""
    if kzt is None:
        return "—"
    v = float(kzt)
    for scale, word in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if abs(v) >= scale:
            return f"≈{NBSP}{num(v / scale, 1)}{NBSP}{word}{NBSP}₸"
    return f"≈{NBSP}{num(v)}{NBSP}₸"


def minutes(value: float | None) -> str:
    """``9.7`` -> ``10 мин``, ``125`` -> ``2 ч 5 мин``, ``0.4`` -> ``менее 1 мин``."""
    if value is None:
        return "—"
    m = round(float(value))
    if m < 1:
        return f"менее{NBSP}1{NBSP}мин"
    if m < 60:
        return f"{m}{NBSP}мин"
    h, rest = divmod(m, 60)
    return f"{num(h)}{NBSP}ч" + (f" {rest}{NBSP}мин" if rest else "")


def ddmm(value: date | str) -> str:
    """``2026-10-15`` -> ``15.10``."""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return d.strftime("%d.%m")


def period(start: date | str, end: date | str) -> str:
    """``15.10`` or ``01.10–16.10``."""
    a, b = ddmm(start), ddmm(end)
    return a if a == b else f"{a}–{b}"


def month_word(month: str) -> str:
    """``2026-10`` -> ``октябрь 2026``."""
    names = (
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    )
    year, mon = month.split("-")[:2]
    return f"{names[int(mon) - 1]} {year}"


def _moment(ts: datetime | str | None) -> datetime | None:
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return ts


def hhmm(ts: datetime | str | None, cfg: TwinConfig) -> str:
    """Plant-local ``HH:MM`` of a moment (``—`` when unknown)."""
    moment = _moment(ts)
    return "—" if moment is None else to_plant_tz(moment, cfg.timezone).strftime("%H:%M")


def when(ts: datetime | str | None, cfg: TwinConfig) -> str:
    """Plant-local ``dd.MM HH:MM`` of a moment (``—`` when unknown)."""
    moment = _moment(ts)
    return "—" if moment is None else to_plant_tz(moment, cfg.timezone).strftime("%d.%m %H:%M")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 отказ, 3 отказа, 5 отказов."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
