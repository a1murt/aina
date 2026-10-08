"""Telegram message texts (SPEC §14), pure functions.

Format: ``[КРИТИЧНО] Конвейер-03 (финальная) — внеплановая остановка оборудования (обрыв цепи) ·
09:31 · Сборка-1 · влияние: −14,2 авто, отыгрывается за ~0,5 смены``; the rule's Russian message
follows on the next line for rules other than AL-S1. Times are plant-local.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from qost_notifier import texts
from twin_core.alert_text import num
from twin_core.clock import to_plant_tz
from twin_core.config import TwinConfig
from twin_core.report import entity_name
from twin_core.rules import AL_EQUIPMENT_STOP


@dataclass(frozen=True, slots=True)
class AlertInfo:
    """An alert as the notifier sees it: stream data merged with its ``alert`` row."""

    dedup_key: str
    rule_id: str
    severity: str
    entity_type: str
    entity: str
    ts: datetime
    message_ru: str
    value: Any
    status: str
    escalation_level: int = 0
    id: int | None = None


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _n1(value: float) -> str:
    return num(round(value, 1))


def impact_short(impact: dict[str, Any]) -> str:
    """FR-ENG-06 impact in the §14 wording, from the alert value ``impact``."""
    lost = float(impact.get("lost_units") or 0.0)
    if lost <= 0:
        return "влияние на выпуск: нет"
    head = f"влияние: −{_n1(lost)} авто"
    if impact.get("bottleneck"):
        return f"{head}, безвозвратно (узкое место)"
    shifts = impact.get("recover_shifts")
    if shifts is None:
        return f"{head}, не отыгрывается (нет запаса мощности)"
    text = f"{head}, отыгрывается за ~{_n1(float(shifts))} смены"
    irrecoverable = float(impact.get("irrecoverable_units") or 0.0)
    if irrecoverable > 0:
        text += f" (~{_n1(irrecoverable)} безвозвратно)"
    return text


def _line_of(cfg: TwinConfig, alert: AlertInfo, value: dict[str, Any]) -> str | None:
    line = value.get("line")
    if isinstance(line, str) and line in cfg.lines:
        return line
    if alert.entity in cfg.equipment:
        return cfg.line_of_equipment(alert.entity).code
    return None


def headline(cfg: TwinConfig, alert: AlertInfo, *, tag: bool = True) -> str:
    value = alert.value if isinstance(alert.value, dict) else {}
    rule = cfg.alert_rules.get(alert.rule_id)
    what = _lower_first(rule.name_ru) if rule is not None else alert.rule_id
    if alert.rule_id == AL_EQUIPMENT_STOP:
        reason = cfg.reasons.get(str(value.get("reason_code")))
        if reason is not None:
            what = f"{what} ({_lower_first(reason.name_ru)})"
    started = parse_ts(value.get("started")) or alert.ts
    parts = [f"{entity_name(cfg, alert.entity)} — {what}", _hhmm(cfg, started)]
    if tag:
        parts[0] = f"{texts.SEVERITY_TAGS.get(alert.severity, '[?]')} {parts[0]}"
    line = _line_of(cfg, alert, value)
    if line is not None and line != alert.entity:
        parts.append(entity_name(cfg, line))
    impact = value.get("impact")
    if isinstance(impact, dict):
        parts.append(impact_short(impact))
    return " · ".join(parts)


def _hhmm(cfg: TwinConfig, moment: datetime) -> str:
    return to_plant_tz(moment, cfg.timezone).strftime("%H:%M")


def open_url(base: str, path: str, alert_id: int | None) -> str | None:
    if alert_id is None:
        return None
    return base.rstrip("/") + path.format(id=alert_id)


def button_url_allowed(url: str) -> bool:
    """Telegram rejects inline URL buttons to localhost; such links go into the text."""
    host = (urlsplit(url).hostname or "").lower()
    return (
        bool(host)
        and host not in ("localhost", "0.0.0.0", "::1")
        and not host.startswith("127.")
        and not host.endswith(".localhost")
    )


def alert_message(
    cfg: TwinConfig,
    alert: AlertInfo,
    *,
    escalation_level: int | None = None,
    link_in_text: str | None = None,
) -> str:
    lines: list[str] = []
    if escalation_level:
        esc = cfg.rules.escalation.get(alert.severity)  # type: ignore[call-overload]
        minutes = esc.timeout_min * escalation_level if esc is not None else 0
        lines.append(texts.ESCALATION.format(level=escalation_level, minutes=num(minutes)))
    lines.append(headline(cfg, alert))
    value = alert.value if isinstance(alert.value, dict) else {}
    if alert.rule_id == AL_EQUIPMENT_STOP:
        elapsed = value.get("elapsed_min")
        if isinstance(elapsed, int | float):
            lines.append(f"Простой: {num(float(elapsed), 0)} мин")
    elif alert.message_ru:
        lines.append(alert.message_ru)
    if link_in_text:
        lines.append(texts.OPEN_LINK.format(url=link_in_text))
    return "\n".join(lines)


def digest_message(cfg: TwinConfig, shift_label: str, alerts: Sequence[AlertInfo]) -> str:
    head = texts.DIGEST_HEAD.format(shift=shift_label, n=len(alerts))
    body = [f"- {headline(cfg, a, tag=False)}" for a in sorted(alerts, key=lambda a: a.ts)]
    return "\n".join([head, *body])


def ack_mark(role: str, moment: datetime, cfg: TwinConfig) -> str:
    return texts.ACK_MARK.format(role=texts.ROLE_NAMES.get(role, role), time=_hhmm(cfg, moment))


__all__ = [
    "AlertInfo",
    "ack_mark",
    "alert_message",
    "button_url_allowed",
    "digest_message",
    "headline",
    "impact_short",
    "open_url",
    "parse_ts",
]
