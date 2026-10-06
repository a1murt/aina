"""Cell value parsing shared by all formats (FR-IMP-01)."""

from __future__ import annotations

import re
from datetime import date
from typing import Final

_SPACES: Final = re.compile(r"[\s    ']")
_NUMBER: Final = re.compile(r"[+-]?\d+(\.\d+)?")
_DMY: Final = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_ISO: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def normalize_header(text: str) -> str:
    """Header for matching: case-folded, single spaces, ``ё`` -> ``е``."""
    return " ".join(text.casefold().replace("ё", "е").split())


def parse_number(text: str) -> float:
    """Parse ``"1,7"``, ``"5 500"``, ``"7.8"``, ``"98%"``, ``"1 234,5"`` (FR-IMP-01).

    Spaces (incl. non-breaking/thin) and apostrophes group thousands; a single ``,`` or ``.`` is
    the decimal separator; with both present, the last one is decimal and the other groups.
    """
    value = _SPACES.sub("", text).removesuffix("%")
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        group = "." if decimal == "," else ","
        value = value.replace(group, "")
    value = value.replace(",", ".")
    if not _NUMBER.fullmatch(value):
        raise ValueError(f"not a number: {text!r}")
    return float(value)


def parse_int(text: str) -> int:
    """Parse a whole quantity (``"118"``, ``"5 500"``, ``"120.0"``)."""
    number = parse_number(text)
    if not number.is_integer():
        raise ValueError(f"not a whole number: {text!r}")
    return int(number)


def parse_date(text: str) -> date:
    """Parse ``dd.MM.yyyy`` (FR-IMP-01); ISO ``yyyy-MM-dd`` is accepted as well."""
    value = text.strip()
    if match := _DMY.fullmatch(value):
        day, month, year = (int(g) for g in match.groups())
    elif match := _ISO.fullmatch(value):
        year, month, day = (int(g) for g in match.groups())
    else:
        raise ValueError(f"not a date dd.MM.yyyy: {text!r}")
    return date(year, month, day)
