"""Number check of report texts (SPEC §11.5, US-7): every number in the text must come from the
input.

Extraction understands Russian/Kazakh formatting:

* thousands separated by spaces (regular, no-break, narrow no-break, thin): ``5 500``;
* decimal comma or point: ``81,2`` / ``81.2``; percents ``81,2%`` / ``81,2 %``; percentage
  points ``1,5 п.п.``;
* ranges ``4 757–4 807`` give two numbers; signs (``−14``) are ignored (values compare by
  magnitude);
* dates (``15.10.2026``, ``15.10``, ``2026-10-15``) and times (``09:31``) are not data numbers:
  they are checked separately and must occur in the input (a bare ``dd.mm`` counts as a date only
  when the input has that day);
* digits inside codes and names are not numbers: anything glued to a letter (``AL-S1``, ``J7``,
  ``P10``, ``CONV-03``, ``Сборка-1``), ordinals (``1-ауысым``, ``2-я``), list markers at a line
  start (``1)``, ``2.``), standard references (``ISO 22400``) and the ``mask`` strings (entity
  names from the input).

Comparison: an integer in the text must equal an input number exactly; a decimal must be within
``tolerance`` (0.05) of one. Percents are normalised both ways: ``81,2%`` matches an input
``81.2`` or ``0.812`` (fraction × 100). Input numbers are every numeric JSON value plus the
numbers written inside input strings (alert messages), extracted by the same rules.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

DEFAULT_TOLERANCE = 0.05
"""Decimals: |text − input| ≤ 0.05 (after percent normalisation), SPEC §11.5."""

_EXACT = 1e-6
_SPACES = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " "})
_STANDARD_REFS = re.compile(r"\b(?:ISO|IEC|ISA|ГОСТ|СТ РК)[ \-]?\d+(?:[-:.]\d+)*", re.IGNORECASE)
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-•*][ \t]*)?\d{1,2}[.)](?=[ \t])")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DOT_DATE = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?![\d])")
_SHORT_DATE = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{2})(?![\d.,]\d)")
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_NUMBER = re.compile(
    r"(?<![\w§])(?<![^\W\d_][-‑])"  # not glued to a word, not after "letter-" (codes)
    r"(?P<int>\d{1,3}(?: \d{3}(?!\d))+|\d+)"
    r"(?:[,.](?P<frac>\d+))?"
    r"(?![-‑][^\W\d_])"  # not an ordinal like "1-ауысым"
    r"(?P<unit>\s?%|\s?п\.\s?п\.?)?"
)
_YEAR_MIN, _YEAR_MAX = 1900, 2100

NumberKind = Literal["int", "decimal"]


@dataclass(frozen=True, slots=True)
class NumberToken:
    text: str
    value: float
    kind: NumberKind
    percent: bool
    """``%`` or ``п.п.`` after the number."""


@dataclass(frozen=True, slots=True)
class Extraction:
    numbers: tuple[NumberToken, ...]
    dates: tuple[tuple[str, int, int, int | None], ...]
    """(text, day, month, year or None)."""
    times: tuple[str, ...]
    """``HH:MM``, zero-padded."""


@dataclass(frozen=True, slots=True)
class Mismatch:
    token: str
    kind: Literal["number", "date", "time"]
    reason: str

    def describe(self) -> str:
        return f"«{self.token}» — {self.reason}"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    checked: int
    mismatches: tuple[Mismatch, ...] = ()
    numbers: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "mismatches": [m.describe() for m in self.mismatches],
        }


@dataclass(frozen=True, slots=True)
class InputValues:
    numbers: tuple[float, ...]
    dates: frozenset[date]
    times: frozenset[str]


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + "§" * (end - start) + text[end:]


def _mask(text: str, mask: Iterable[str]) -> str:
    for item in sorted({m for m in mask if m and any(ch.isdigit() for ch in m)}, key=len)[::-1]:
        text = text.replace(item, "§" * len(item))
    return _STANDARD_REFS.sub(lambda m: "§" * len(m.group(0)), text)


def _valid_date(day: int, month: int, year: int | None) -> bool:
    try:
        date(year or 2000, month, day)
    except ValueError:
        return False
    return True


def extract(
    text: str,
    *,
    mask: Iterable[str] = (),
    known_days: Iterable[tuple[int, int]] | None = None,
) -> Extraction:
    """Numbers, dates and times of ``text`` (see the module docstring).

    ``known_days`` — (day, month) pairs that a bare ``dd.mm`` may denote; without it every valid
    ``dd.mm`` is read as a date.
    """
    work = _mask(text.translate(_SPACES), mask)
    work = _LIST_MARKER.sub(lambda m: "§" * len(m.group(0)), work)
    dates: list[tuple[str, int, int, int | None]] = []
    times: list[str] = []
    for m in list(_ISO_DATE.finditer(work)):
        y, mo, d = (int(g) for g in m.groups())
        if _valid_date(d, mo, y):
            dates.append((m.group(0), d, mo, y))
            work = _blank(work, *m.span())
    for m in list(_DOT_DATE.finditer(work)):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = y + 2000 if y < 100 else y
        if _valid_date(d, mo, year):
            dates.append((m.group(0), d, mo, year))
            work = _blank(work, *m.span())
    days = None if known_days is None else set(known_days)
    for m in list(_SHORT_DATE.finditer(work)):
        d, mo = int(m.group(1)), int(m.group(2))
        if _valid_date(d, mo, None) and (days is None or (d, mo) in days):
            dates.append((m.group(0), d, mo, None))
            work = _blank(work, *m.span())
    for m in list(_TIME.finditer(work)):
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh < 24 and mm < 60:
            times.append(f"{hh:02d}:{mm:02d}")
            work = _blank(work, *m.span())
    numbers: list[NumberToken] = []
    for m in _NUMBER.finditer(work):
        whole = m.group("int").replace(" ", "")
        frac = m.group("frac")
        value = float(f"{whole}.{frac}") if frac else float(whole)
        numbers.append(
            NumberToken(
                text=m.group(0).strip(),
                value=value,
                kind="decimal" if frac else "int",
                percent=m.group("unit") is not None,
            )
        )
    return Extraction(tuple(numbers), tuple(dates), tuple(times))


def _walk(obj: Any) -> Iterable[Any]:
    if isinstance(obj, Mapping):
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list | tuple):
        for value in obj:
            yield from _walk(value)
    else:
        yield obj


_ISO_IN_STRING = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME_IN_STRING = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d])")


def input_values(data: Any, *, mask: Iterable[str] = ()) -> InputValues:
    """Numbers, dates and ``HH:MM`` times present in ``data`` (a JSON-like structure).

    A full ISO timestamp contributes its date but not its time (times in the input are the
    explicit plant-local ``HH:MM`` strings).
    """
    masked = list(mask)
    numbers: list[float] = []
    dates: set[date] = set()
    times: set[str] = set()
    for leaf in _walk(data):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, int | float):
            if math.isfinite(float(leaf)):
                numbers.append(float(leaf))
            continue
        if not isinstance(leaf, str):
            continue
        for m in _ISO_IN_STRING.finditer(leaf):
            y, mo, d = (int(g) for g in m.groups())
            if _valid_date(d, mo, y):
                dates.add(date(y, mo, d))
        if "T" in leaf and _ISO_IN_STRING.match(leaf):
            continue  # an ISO timestamp: UTC time of day is not something a text may quote
        for m in _TIME_IN_STRING.finditer(leaf):
            hh, mm = int(m.group(1)), int(m.group(2))
            if hh < 24 and mm < 60:
                times.add(f"{hh:02d}:{mm:02d}")
        found = extract(leaf, mask=masked)
        numbers.extend(t.value for t in found.numbers)
        for _, d, mo, year in found.dates:
            if year is not None and _valid_date(d, mo, year):
                dates.add(date(year, mo, d))
        times.update(found.times)
    return InputValues(tuple(sorted(set(numbers))), frozenset(dates), frozenset(times))


def _number_ok(token: NumberToken, values: tuple[float, ...], tolerance: float) -> bool:
    x = abs(token.value)
    tol = _EXACT if token.kind == "int" else tolerance + _EXACT
    for raw in values:
        y = abs(raw)
        if abs(x - y) <= tol:
            return True
        if token.percent and y <= 1.5 and abs(x - y * 100.0) <= tol:
            return True
    return False


def verify(
    text: str,
    data: Any,
    *,
    mask: Iterable[str] = (),
    tolerance: float = DEFAULT_TOLERANCE,
) -> VerifyResult:
    """Check that every number, date and time of ``text`` is present in ``data``."""
    masked = list(mask)
    allowed = input_values(data, mask=masked)
    years = {d.year for d in allowed.dates}
    found = extract(text, mask=masked, known_days={(d.day, d.month) for d in allowed.dates})
    mismatches: list[Mismatch] = []
    checked = 0
    for token in found.numbers:
        if token.kind == "int" and not token.percent and int(token.value) in years:
            continue  # a year next to a month name, e.g. «октябрь 2026»
        checked += 1
        if not _number_ok(token, allowed.numbers, tolerance):
            mismatches.append(Mismatch(token.text, "number", "нет во входных данных"))
    for text_date, d, mo, y in found.dates:
        checked += 1
        if not any(
            a.day == d and a.month == mo and (y is None or a.year == y) for a in allowed.dates
        ):
            mismatches.append(Mismatch(text_date, "date", "дата не из периода рапорта"))
    for hhmm in found.times:
        checked += 1
        if hhmm not in allowed.times:
            mismatches.append(Mismatch(hhmm, "time", "время не из входных данных"))
    return VerifyResult(
        ok=not mismatches,
        checked=checked,
        mismatches=tuple(mismatches),
        numbers=tuple(t.text for t in found.numbers),
    )


def canonical_json(data: Any) -> str:
    """Stable JSON of the input (sorted keys) — what the prompt shows and the check reads."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DEFAULT_TOLERANCE",
    "Extraction",
    "InputValues",
    "Mismatch",
    "NumberToken",
    "VerifyResult",
    "canonical_json",
    "extract",
    "input_values",
    "verify",
]
