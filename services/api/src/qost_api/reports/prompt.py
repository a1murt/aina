"""Prompts of the LLM shift report (SPEC §11.5): strict rules, input JSON, regeneration."""

from __future__ import annotations

import re

from twin_core.report import MAX_WORDS, SECTIONS, Lang, ShiftInput, canonical_json

_LANG_NAME = {"ru": "русском", "kk": "казахском"}

_SYSTEM = """Ты пишешь сменный рапорт для мастера смены и директора автозавода.
Пиши только по данным JSON из сообщения пользователя. Правила:
1. Язык рапорта — {lang_name}. Не больше {max_words} слов.
2. Ровно четыре раздела, заголовок каждого — отдельной строкой, без разметки и двоеточия:
{headings}
3. Каждое число бери из JSON и пиши точно как там (те же знаки после запятой, десятичная запятая).
   Проценты — из полей *_pct со знаком %. Ничего не вычисляй сам: никаких сумм, разностей,
   средних, округлений и пересчётов. Если нужного числа в JSON нет — пиши без числа.
4. Даты и время — только те, что есть в JSON (дата ДД.ММ.ГГГГ, время ЧЧ:ММ).
5. Пункты списков начинай с «- », без нумерации. Не используй Markdown, коды правил (AL-…)
   и служебные ключи JSON; называй оборудование и участки по полям name.
6. «Итоги» — выпуск, план, OEE, брак, узкое место, прогноз месяца (если есть).
   «Отклонения» — из deviations. «Причины» — простои (stops), потери (losses), брак (defects).
   «Задачи на следующую смену» — 2–4 конкретных действия по причинам выше.
7. Не выдумывай фактов, которых нет в данных; не давай советов вне производства."""


def system_prompt(lang: Lang) -> str:
    headings = "\n".join(f"   {h}" for h in SECTIONS[lang])
    return _SYSTEM.format(lang_name=_LANG_NAME[lang], max_words=MAX_WORDS, headings=headings)


def user_prompt(data: ShiftInput) -> str:
    payload = canonical_json(data.model_dump(mode="json"))
    return f"Данные смены (JSON):\n{payload}\n\nНапиши сменный рапорт по правилам."


def retry_prompt(data: ShiftInput, previous: str, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        f"{user_prompt(data)}\n\nПредыдущий вариант не прошёл проверку:\n{listed}\n\n"
        f"Предыдущий вариант:\n<<<\n{previous.strip()}\n>>>\n\n"
        "Напиши рапорт заново: каждое число — дословно из JSON, все четыре раздела."
    )


_MARKDOWN = re.compile(r"(\*\*|__|`)")
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*")


def clean_text(text: str) -> str:
    """Drop Markdown emphasis and heading marks a model may add despite the rules."""
    text = _MARKDOWN.sub("", text)
    text = _HEADING.sub("", text)
    return text.strip() + "\n"


__all__ = ["clean_text", "retry_prompt", "system_prompt", "user_prompt"]
