from __future__ import annotations

import pytest

from app.services.agent_graph import (
    _OUT_OF_SCOPE_GRENADE_TERMS,
    _OUT_OF_SCOPE_MAP_TERMS,
    _SUPPORTED_GRENADE_TERMS,
    _SUPPORTED_MAP_TERMS,
    scope_is_supported,
)
from app.services.normalization import CatalogTerm, matching_entities, normalize_text, term_in_text


def _term(value: str) -> tuple[CatalogTerm, ...]:
    return (CatalogTerm(value, normalize_text(value), "alias"),)


@pytest.mark.parametrize(
    "unsupported",
    sorted(_OUT_OF_SCOPE_MAP_TERMS | _OUT_OF_SCOPE_GRENADE_TERMS),
)
def test_every_fixed_unsupported_scope_term_is_rejected(unsupported: str) -> None:
    assert not scope_is_supported(f"我想在 Mirage 封烟，但其实是 {unsupported}")


@pytest.mark.parametrize("supported", sorted(_SUPPORTED_MAP_TERMS | _SUPPORTED_GRENADE_TERMS))
def test_supported_scope_terms_are_not_rejected(supported: str) -> None:
    assert scope_is_supported(f"我想在 {supported} 封烟")


def test_supported_and_unsupported_scope_terms_are_out_of_scope() -> None:
    assert not scope_is_supported("Mirage 烟雾，但我要投 flash")


def test_longest_term_occupies_span_before_shorter_term() -> None:
    entities = [
        ("short", _term("VIP")),
        ("long", _term("小VIP")),
    ]
    assert matching_entities("小VIP", entities) == ["long"]


def test_length_precedence_is_global_even_when_long_term_starts_later() -> None:
    entities = [
        ("short", _term("VIP")),
        ("long", _term("小VIP")),
    ]
    assert matching_entities("VIP 小VIP", entities) == ["long", "short"]


def test_equal_span_from_multiple_entities_remains_ambiguous() -> None:
    entities = [("first", _term("VIP")), ("second", _term("VIP"))]
    assert matching_entities("VIP", entities) == ["first", "second"]


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [("viper", "vip", False), ("VIP!", "vip", True), ("小VIP点", "vip", True)],
)
def test_ascii_boundaries_and_cjk_substrings(text: str, term: str, expected: bool) -> None:
    assert term_in_text(text, term) is expected


def test_normalization_is_nfkc_lowercase_not_casefold() -> None:
    assert normalize_text(" Ｗ＋跳投，Straße ") == "w 跳投 straße"


def test_mixed_cjk_term_uses_literal_substring_even_with_ascii_ends() -> None:
    assert term_in_text("xxA小Ayy", "A小A")
    assert matching_entities("xxA小Ayy", [("mixed", _term("A小A"))]) == ["mixed"]
