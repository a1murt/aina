"""External name -> code resolution (FR-DOM-02) and closest-code suggestions (FR-IMP-02).

Names coming from external sources (docx/xlsx imports, MES exports, operator journals) are matched
case-insensitively and ignoring whitespace; dash variants and "ё" are unified as well, so
"камера - 02", "Камера–02" and "КАМЕРА-02" all resolve to the same code.

Unknown names get a suggestion of the closest known code by Levenshtein distance computed on the
normalised strings.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# Hyphen, non-breaking hyphen, figure dash, en/em dash, bar, minus, small/fullwidth hyphen.
_DASH_VARIANTS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
_DASHES = dict.fromkeys(map(ord, _DASH_VARIANTS), "-")
_MIN_PART_LEN = 4


def normalize_name(name: str) -> str:
    """Normalise a name for lookup: NFKC, casefold, no whitespace, one dash, ё -> е."""
    text = unicodedata.normalize("NFKC", name).translate(_DASHES).casefold()
    text = text.replace("ё", "е")
    return "".join(ch for ch in text if not ch.isspace())


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insert / delete / substitute, each cost 1)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


@dataclass(frozen=True, order=True)
class Suggestion:
    """A candidate code for an unknown name, ordered by distance then code."""

    distance: int
    code: str
    matched: str = field(compare=False)
    """The known name (code or alias) the input was closest to."""


def rank_candidates(
    value: str, candidates: Mapping[str, str] | Iterable[str], *, limit: int = 3
) -> list[Suggestion]:
    """Rank candidates by normalised Levenshtein distance to ``value``.

    ``candidates`` is either an iterable of codes or a mapping ``known name -> code`` (several
    names may point to the same code; each code is reported once, with its best distance).
    """
    pairs: Iterable[tuple[str, str]]
    pairs = candidates.items() if isinstance(candidates, Mapping) else ((c, c) for c in candidates)
    needle = normalize_name(value)
    best: dict[str, Suggestion] = {}
    for known, code in pairs:
        dist = levenshtein(needle, normalize_name(known))
        current = best.get(code)
        if current is None or dist < current.distance:
            best[code] = Suggestion(distance=dist, code=code, matched=known)
    return sorted(best.values())[:limit]


def closest_code(
    value: str,
    candidates: Mapping[str, str] | Iterable[str],
    *,
    max_distance: int | None = None,
) -> str | None:
    """Return the closest candidate code, or ``None`` if nothing is close enough.

    By default a suggestion is offered when the edit distance is at most
    ``max(2, len(value) // 3)``, or else when the value is a part of exactly one known name
    (``vibration`` -> ``vibration_mm_s``); beyond that a "did you mean" confuses more than it
    helps. Pass ``max_distance`` explicitly to override (FR-IMP-02 import reports always want the
    nearest code: pass a large value).
    """
    pairs = (
        list(candidates.items())
        if isinstance(candidates, Mapping)
        else [(c, c) for c in candidates]
    )
    ranked = rank_candidates(value, dict(pairs), limit=1)
    if not ranked:
        return None
    needle = normalize_name(value)
    top = ranked[0]
    if max_distance is not None:
        return top.code if top.distance <= max_distance else None
    if top.distance <= max(2, len(needle) // 3):
        return top.code
    if len(needle) >= _MIN_PART_LEN:
        containing = {code for known, code in pairs if needle in normalize_name(known)}
        if len(containing) == 1:
            return containing.pop()
    return None


class UnknownNameError(LookupError):
    """Raised by :meth:`AliasIndex.require` for names that resolve to no code."""

    def __init__(self, kind: str, name: str, suggestion: str | None) -> None:
        self.kind = kind
        self.name = name
        self.suggestion = suggestion
        hint = f"; did you mean '{suggestion}'?" if suggestion else ""
        super().__init__(f"unknown {kind} '{name}'{hint}")


@dataclass(frozen=True)
class AliasCollision:
    """Two different codes claim the same normalised name."""

    kind: str
    name: str
    existing_code: str
    new_code: str


class AliasIndex:
    """Case/whitespace-insensitive index ``external name -> code`` for one entity kind."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._by_norm: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._codes: list[str] = []
        self.collisions: list[AliasCollision] = []

    def add(self, code: str, *names: str | None) -> None:
        """Register ``code`` under itself and any number of names/aliases (``None`` skipped)."""
        if code not in self._codes:
            self._codes.append(code)
        for name in (code, *names):
            if not name:
                continue
            key = normalize_name(name)
            existing = self._by_norm.get(key)
            if existing is not None and existing != code:
                self.collisions.append(AliasCollision(self.kind, name, existing, code))
                continue
            self._by_norm[key] = code
            self._names.setdefault(name, code)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(self._codes)

    @property
    def names(self) -> Mapping[str, str]:
        """All registered names (codes and aliases, original spelling) -> code."""
        return dict(self._names)

    def resolve(self, name: str) -> str | None:
        """Return the code for an external name, or ``None``."""
        return self._by_norm.get(normalize_name(name))

    def suggest(self, name: str, *, limit: int = 3) -> list[Suggestion]:
        """Closest known codes for an unknown name (best first)."""
        return rank_candidates(name, self._names, limit=limit)

    def require(self, name: str) -> str:
        """Return the code for ``name`` or raise :class:`UnknownNameError` with a suggestion."""
        code = self.resolve(name)
        if code is None:
            raise UnknownNameError(self.kind, name, closest_code(name, self._names))
        return code

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.resolve(name) is not None

    def __len__(self) -> int:
        return len(self._codes)
