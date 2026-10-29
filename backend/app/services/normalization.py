"""Deterministic, provider-free text normalization and term matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_SPACE_RE = re.compile(r"\s+")
_ASCII_WORD = re.compile(r"[a-z0-9]")


def normalize_text(value: str) -> str:
    """Return the comparison form required by Spec section 6.2.

    NFKC makes full-width Latin input comparable to the catalog.  Punctuation
    and symbols become separators, while CJK letters remain intact.  We do not
    remove words or perform fuzzy matching here; callers decide whether a
    match is semantically acceptable.
    """

    normalized = unicodedata.normalize("NFKC", value).lower()
    chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if char.isspace() or category.startswith(("P", "S")) or category == "Cc":
            chars.append(" ")
        else:
            chars.append(char)
    return _SPACE_RE.sub(" ", "".join(chars)).strip()


def _is_ascii_word(char: str | None) -> bool:
    return bool(char and _ASCII_WORD.fullmatch(char))


def term_in_text(text: str, term: str) -> bool:
    """Match a normalized catalog term with ASCII-safe boundaries.

    Chinese terms do not need an artificial boundary.  ASCII terms such as
    ``vip`` must not match the middle of ``viper``.  The catalog terms are
    normalized once at the call site, so this function remains pure and
    inexpensive for the small Mirage catalog.
    """

    haystack = normalize_text(text)
    needle = normalize_text(term)
    if not needle:
        return False
    start = 0
    while True:
        position = haystack.find(needle, start)
        if position < 0:
            return False
        before = haystack[position - 1] if position else None
        after_index = position + len(needle)
        after = haystack[after_index] if after_index < len(haystack) else None
        if not needle.isascii() or (not _is_ascii_word(before) and not _is_ascii_word(after)):
            return True
        start = position + 1


@dataclass(frozen=True, slots=True)
class CatalogTerm:
    value: str
    normalized: str
    source: str


def catalog_terms(name_zh: str, name_en: str | None, aliases: list[str]) -> tuple[CatalogTerm, ...]:
    values: list[CatalogTerm] = []
    named_values = [("name_zh", name_zh)]
    if name_en:
        named_values.append(("name_en", name_en))
    for source, value in named_values:
        normalized = normalize_text(value)
        if normalized:
            values.append(CatalogTerm(value, normalized, source))
    for alias in aliases:
        normalized = normalize_text(alias)
        if normalized:
            values.append(CatalogTerm(alias, normalized, "alias"))
    # Prefer the longest spelling when multiple aliases overlap.  The caller
    # still receives every entity match, never a model-generated guess.
    return tuple(sorted(values, key=lambda item: (-len(item.normalized), item.normalized)))


def _matching_spans(
    text: str, entities: list[tuple[str, tuple[CatalogTerm, ...]]]
) -> list[tuple[str, int, int, str]]:
    """Return occupied entity spans after deterministic longest matching."""
    haystack = normalize_text(text)
    # Length and spelling are global precedence rules.  A later long term
    # must consume its span before an earlier short term gets a chance to
    # claim it.
    candidates: list[tuple[int, str, int, int, str]] = []
    for entity_id, terms in entities:
        for term in terms:
            needle = term.normalized
            if not needle:
                continue
            start = 0
            while True:
                position = haystack.find(needle, start)
                if position < 0:
                    break
                before = haystack[position - 1] if position else None
                after_index = position + len(needle)
                after = haystack[after_index] if after_index < len(haystack) else None
                if not needle.isascii() or (
                    not _is_ascii_word(before) and not _is_ascii_word(after)
                ):
                    candidates.append(
                        (len(needle), needle, position, position + len(needle), entity_id)
                    )
                start = position + 1
    candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[4]))
    accepted: list[tuple[int, int, str, str]] = []
    for _length, needle, start, end, entity_id in candidates:
        overlaps = any(
            start < accepted_end and accepted_start < end
            for accepted_start, accepted_end, _accepted_id, _accepted_term in accepted
        )
        # Equal normalized spans may belong to multiple entities; preserve
        # that ambiguity.  All other overlaps are consumed by the first
        # longest/lexical candidate.
        same_span = any(
            start == accepted_start and end == accepted_end and needle == accepted_term
            for accepted_start, accepted_end, _accepted_id, accepted_term in accepted
        )
        if overlaps and not same_span:
            continue
        accepted.append((start, end, entity_id, needle))
    return [(entity_id, start, end, needle) for start, end, entity_id, needle in accepted]


def matching_entity_spans(
    text: str, entities: list[tuple[str, tuple[CatalogTerm, ...]]]
) -> list[tuple[str, int, int, str]]:
    """Return distinct entities together with their occupied text spans.

    The span is needed by the public resolver to distinguish a same-field
    ambiguity from a Target/Origin cross-field ambiguity.  Callers should not
    infer semantics from the term text; only exact normalized intervals are
    significant.
    """

    return _matching_spans(text, entities)


def matching_entities(text: str, entities: list[tuple[str, tuple[CatalogTerm, ...]]]) -> list[str]:
    """Return distinct entity ids using longest-term span occupancy.

    Without span occupancy, a catalog entry named ``小VIP`` would also match
    the shorter ``VIP`` term in the same text.  Equal-length terms sharing a
    span remain an intentional ambiguity, while a shorter overlapping term is
    consumed by the longer spelling.
    """

    matched: list[str] = []
    for entity_id, _start, _end, _term in _matching_spans(text, entities):
        if entity_id not in matched:
            matched.append(entity_id)
    return matched
