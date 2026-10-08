"""Russian alert texts (titles and messages) shared by the import path (api) and the engine.

Kazakh texts arrive with M8. Numbers use a decimal comma; times are shown in the plant zone.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from twin_core.clock import to_plant_tz
from twin_core.config import TwinConfig
from twin_core.rules import (
    AL_ANDON,
    AL_BUFFER,
    AL_CKD_COVERAGE,
    AL_DEFECT_RATE,
    AL_DOWNTIME_LIMIT,
    AL_EQUIPMENT_STOP,
    AL_MATERIAL_CALL,
    AL_OEE_BELOW,
    AL_OEE_NEAR,
    AL_PLAN_RISK,
    AL_SYSTEMIC_DEFECTS,
    Alert,
)


def pct(value: float) -> str:
    """Fraction as a percent with up to 2 decimals and a decimal comma: 0.0517 -> ``5,17``."""
    return f"{value * 100:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def num(value: float, digits: int | None = None) -> str:
    """Number with a decimal comma (``g`` format, or ``digits`` decimals)."""
    text = f"{value:g}" if digits is None else f"{value:.{digits}f}"
    return text.replace(".", ",")


def day(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def local_time(value: datetime | str, cfg: TwinConfig) -> str:
    moment = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    return to_plant_tz(moment, cfg.timezone).strftime("%d.%m.%Y %H:%M")


def alert_title_ru(alert: Alert, cfg: TwinConfig) -> str:
    rule = cfg.alert_rules.get(alert.rule_id)
    return rule.name_ru if rule is not None else alert.rule_id


def impact_text(impact: dict[str, Any]) -> str:
    """FR-ENG-06 wording of :func:`twin_core.kpi.stop_impact` (as stored in the alert value)."""
    lost = float(impact.get("lost_units") or 0.0)
    if lost <= 0:
        return "влияния на выпуск нет"
    head = f"потеря ≈ {num(lost, 1)} авто"
    if impact.get("bottleneck"):
        return f"{head}, безвозвратно (узкое место)"
    shifts = impact.get("recover_shifts")
    irrecoverable = float(impact.get("irrecoverable_units") or 0.0)
    if shifts is None:
        return f"{head}, не отыгрывается (нет запаса мощности)"
    tail = f"отыгрывается за ≈ {num(float(shifts), 1)} смены"
    if irrecoverable > 0:
        tail += f", из них ≈ {num(irrecoverable, 1)} безвозвратно (буфер полон)"
    return f"{head}; {tail}"


def alert_message_ru(alert: Alert, cfg: TwinConfig) -> str:
    """Russian alert text for feeds and Telegram."""
    t = cfg.rules.thresholds
    when = day(alert.period_date) + (f", смена {alert.shift}" if alert.shift else "")
    value = alert.value
    if alert.rule_id == AL_DEFECT_RATE and isinstance(value, float):
        name = cfg.areas[alert.entity].name_ru
        return f"{name}: брак {pct(value)}% ({when}), норма {pct(t.defect_rate_limit)}%"
    if alert.rule_id in (AL_OEE_BELOW, AL_OEE_NEAR) and isinstance(value, float):
        name = cfg.lines[alert.entity].name_ru
        return f"{name}: OEE {pct(value)}% ({when}), цель {pct(t.oee_target)}%"
    if alert.rule_id == AL_DOWNTIME_LIMIT and isinstance(value, float):
        name = cfg.equipment[alert.entity].name_ru
        return (
            f"{name}: внеплановый простой {num(value)} мин ({when}), "
            f"лимит {num(t.critical_downtime_limit_min_per_day)} мин в сутки"
        )
    if alert.rule_id == AL_SYSTEMIC_DEFECTS and isinstance(value, dict):
        parts = ", ".join(f"{cfg.areas[area].name_ru} {pct(rate)}%" for area, rate in value.items())
        return f"Брак вырос на всех участках ({when}): {parts}"
    if alert.rule_id == AL_EQUIPMENT_STOP and isinstance(value, dict):
        name = cfg.equipment[alert.entity].name_ru
        reason = cfg.reasons.get(str(value.get("reason_code")))
        cause = reason.name_ru.lower() if reason is not None else "причина не указана"
        started = local_time(alert.period, cfg) if alert.period_key else when
        minutes = num(float(value.get("elapsed_min", 0)), 0)
        text = f"{name}: {cause}, остановка с {started}, {minutes} мин"
        if value.get("microstop"):
            return text + " — микроостановка, закрыто автоматически"
        impact = value.get("impact")
        return text + (f"; {impact_text(impact)}" if isinstance(impact, dict) else "")
    if alert.rule_id == AL_BUFFER and isinstance(value, dict):
        buffer = cfg.buffers[alert.entity]
        level, capacity = value.get("level"), value.get("capacity")
        if value.get("direction") == "high":
            risk = f"риск блокировки {cfg.lines[buffer.from_line].name_ru}"
        else:
            risk = f"риск голодания {cfg.lines[buffer.to_line].name_ru}"
        return f"{buffer.name_ru}: уровень {level} из {capacity} — {risk}"
    if alert.rule_id == AL_CKD_COVERAGE and isinstance(value, dict):
        product = cfg.products[alert.entity].name
        return (
            f"{product}: комплектов {value.get('kits')}, запас "
            f"{num(float(value.get('coverage_days', 0)), 1)} сут. плана "
            f"(норма ≥ {num(t.ckd_coverage_min_days)})"
        )
    if alert.rule_id == AL_PLAN_RISK and isinstance(value, dict):
        p = float(value.get("p", 0.0))
        text = (
            f"Вероятность выполнить план месяца {pct(p)}% "
            f"(цель {num(float(value.get('target_qty', 0)), 0)} авто, {when})"
        )
        if "p50" in value:
            text += f", медианный прогноз {num(float(value['p50']), 0)} авто"
        return text
    if alert.rule_id in (AL_ANDON, AL_MATERIAL_CALL) and isinstance(value, dict):
        line = cfg.lines.get(alert.entity)
        name = line.name_ru if line is not None else alert.entity
        who = value.get("user")
        if alert.rule_id == AL_ANDON:
            text = f"{name}: андон — оператор вызывает мастера"
            reason = cfg.reasons.get(str(value.get("reason_code")))
            if reason is not None:
                text += f" ({reason.name_ru.lower()})"
            if value.get("equipment"):
                text += f", {cfg.equipment[str(value['equipment'])].name_ru}"
        else:
            kit = cfg.products.get(str(value.get("product")))
            what = f" ({kit.name})" if kit is not None else ""
            text = f"{name}: нет комплектующих{what} — вызов материалов"
        if int(value.get("count", 1) or 1) > 1:
            text += f", вызовов: {value['count']}"
        if value.get("comment"):
            text += f". «{value['comment']}»"
        return text + (f" [{who}]" if who else "")
    return f"{alert_title_ru(alert, cfg)}: {alert.entity} ({when})"


__all__ = ["alert_message_ru", "alert_title_ru", "day", "impact_text", "local_time", "num", "pct"]
