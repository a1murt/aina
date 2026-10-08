"""Russian texts of the Telegram bot (SPEC §14). Codes stay as in the config; names come from
``plant.yaml``/``rules.yaml``."""

from __future__ import annotations

ROLE_NAMES: dict[str, str] = {
    "director": "Директор",
    "master": "Мастер смены",
    "operator": "Оператор",
    "maintenance": "Механик (ТОиР)",
    "quality": "Качество (ОТК)",
    "admin": "Администратор",
}

SEVERITY_TAGS: dict[str, str] = {
    "critical": "[КРИТИЧНО]",
    "warning": "[ВНИМАНИЕ]",
    "info": "[ИНФО]",
}

START = "Оповещения Aina. Выберите роль, затем введите PIN роли."
NO_ROLES = "Подписка недоступна: роли и PIN не настроены (TELEGRAM_ROLE_PINS)."
ASK_PIN = "Введите PIN роли «{role}»."
UNKNOWN_ROLE = "Неизвестная роль. Нажмите /start."
WRONG_PIN = "Неверный PIN. Осталось попыток: {left}."
TOO_MANY = "Неверный PIN. Попытки исчерпаны — начните заново: /start."
SUBSCRIBED = "Готово: вы подписаны на оповещения роли «{role}». Отписаться — /stop."
UNSUBSCRIBED = "Подписка отключена. Подписаться снова — /start."
NOT_SUBSCRIBED = "Этот чат не подписан. Нажмите /start."
BUTTON_ACK = "Принять"
BUTTON_OPEN = "Открыть"
ACK_DONE = "Принято"
ACK_ALREADY = "Оповещение уже принято или закрыто"
ACK_FORBIDDEN = "Ваша роль не может принять это оповещение"
ACK_FAILED = "Не удалось принять — попробуйте в веб-интерфейсе"
ACK_MARK = "Принято: {role}, {time}"
ESCALATION = "Эскалация (уровень {level}): не принято за {minutes} мин"
DIGEST_HEAD = "Дайджест смены {shift}: информационные оповещения ({n})"
OPEN_LINK = "Открыть: {url}"
SUBSCRIBE_FAILED = "Не удалось оформить подписку: заводские часы недоступны. Попробуйте позже."
