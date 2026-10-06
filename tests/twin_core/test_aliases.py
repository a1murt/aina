"""External name -> code lookup (FR-DOM-02) and closest-code suggestions (FR-IMP-02)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from twin_core.aliases import (
    AliasIndex,
    UnknownNameError,
    closest_code,
    levenshtein,
    normalize_name,
    rank_candidates,
)
from twin_core.config import TwinConfig

words = st.text(alphabet="abcАБВ-12 ", max_size=12)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Камера-02", "камера-02"),
        ("  камера - 02 ", "камера-02"),
        ("КАМЕРА–02", "камера-02"),  # en dash
        ("Камера - 02", "камера-02"),  # no-break spaces
        ("Ёлка", "елка"),
    ],
)
def test_normalize_name(raw: str, normalized: str) -> None:
    assert normalize_name(raw) == normalized


@given(words)
def test_normalize_is_idempotent(text: str) -> None:
    assert normalize_name(normalize_name(text)) == normalize_name(text)


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [("", "", 0), ("abc", "", 3), ("kitten", "sitting", 3), ("CONV-3", "CONV-03", 1)],
)
def test_levenshtein_known_values(a: str, b: str, distance: int) -> None:
    assert levenshtein(a, b) == distance


@given(words, words, words)
def test_levenshtein_is_a_metric(a: str, b: str, c: str) -> None:
    assert levenshtein(a, b) == levenshtein(b, a)
    assert (levenshtein(a, b) == 0) == (a == b)
    assert levenshtein(a, c) <= levenshtein(a, b) + levenshtein(b, c)
    assert levenshtein(a, b) <= max(len(a), len(b))


def test_closest_code_threshold() -> None:
    codes = ["WELD-1", "PAINT-1", "ASSY-1", "QC-1"]
    assert closest_code("paint1", codes) == "PAINT-1"
    assert closest_code("Something else", codes) is None
    assert closest_code("Something else", codes, max_distance=100) is not None


def test_rank_candidates_reports_each_code_once() -> None:
    ranked = rank_candidates(
        "Камера 2", {"Камера-02": "BOOTH-02", "BOOTH-02": "BOOTH-02", "Камера-01": "BOOTH-01"}
    )
    assert [s.code for s in ranked] == ["BOOTH-02", "BOOTH-01"]
    assert ranked[0].matched == "Камера-02"


def test_alias_index_resolution_and_collisions() -> None:
    index = AliasIndex("equipment")
    index.add("BOOTH-02", "Камера базы и лака", "Камера-02", None)
    index.add("BOOTH-01", "Камера-01")
    assert index.resolve("камера-02") == "BOOTH-02"
    assert index.resolve("booth-02") == "BOOTH-02"
    assert index.resolve("Камера-03") is None
    assert "КАМЕРА - 01" in index
    assert len(index) == 2
    with pytest.raises(UnknownNameError, match="did you mean 'BOOTH-02'"):
        index.require("Камера-2")
    index.add("BOOTH-03", "Камера-01")
    assert index.collisions[0].existing_code == "BOOTH-01"


@pytest.mark.parametrize(
    ("kind", "name", "code"),
    [
        ("equipment", "Камера-02", "BOOTH-02"),
        ("equipment", "конвейер-03", "CONV-03"),
        ("equipment", "Кондуктор-01", "JIG-01"),
        ("line", "Сварка-1", "WELD-1"),
        ("line", "ОТК-1", "QC-1"),
        ("area", "Окраска", "PAINT"),
        ("area", "отк", "QC"),
        ("product", "Chevrolet Onix", "ONIX"),
        ("product", "jac j7", "J7"),
        ("reason", "Обрыв цепи", "ME-CHAIN"),
        ("reason", "ППР", "PM-SCHEDULED"),
        ("reason", "Замена электродов", "MT-CONSUMABLE"),
        ("defect", "Потёки", "P-RUN"),
        ("buffer", "PBS", "PBS"),
    ],
)
def test_case_aliases_resolve(cfg: TwinConfig, kind: str, name: str, code: str) -> None:
    assert cfg.aliases.by_kind()[kind].resolve(name) == code


def test_repository_aliases_are_unambiguous(cfg: TwinConfig) -> None:
    for index in cfg.aliases.by_kind().values():
        assert index.collisions == []


def test_unknown_import_name_gets_closest_code(cfg: TwinConfig) -> None:
    assert cfg.aliases.equipment.suggest("Конвеер-03")[0].code == "CONV-03"
    with pytest.raises(UnknownNameError) as caught:
        cfg.aliases.reasons.require("Обрыв цепей")
    assert caught.value.suggestion == "ME-CHAIN"


def test_resolve_asset_over_kinds(cfg: TwinConfig) -> None:
    assert cfg.aliases.resolve_asset("Камера-02") == ("equipment", "BOOTH-02")
    assert cfg.aliases.resolve_asset("Сборка-1") == ("line", "ASSY-1")
    assert cfg.aliases.resolve_asset("Сборка") == ("area", "ASSY")
    assert cfg.aliases.resolve_asset("Неизвестно") is None
