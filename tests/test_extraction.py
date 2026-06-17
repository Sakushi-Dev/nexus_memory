"""Fact extraction always yields JSON-valid (schema-validating) facts."""

from __future__ import annotations

from nexus_memory.layers.semantic.extraction import FactExtractor, MockFactExtractor
from nexus_memory.core.models import ExtractedFacts


def test_extractor_is_subclass():
    assert issubclass(MockFactExtractor, FactExtractor)


def test_extract_returns_list_of_dicts():
    facts = MockFactExtractor().extract(
        "My name is John.",
        "You prefer dark mode and use Python daily for your work.",
    )
    assert isinstance(facts, list)
    assert facts, "expected at least one extracted fact"
    for f in facts:
        assert set(f) >= {"content", "importance"}
        assert isinstance(f["content"], str) and f["content"]
        assert 1 <= f["importance"] <= 10


def test_extract_output_validates_against_schema():
    """The contract guarantees output validates against ExtractedFacts."""
    facts = MockFactExtractor().extract(
        "Where is the office located?",
        "The headquarters is located in Berlin near the central station.",
    )
    # Must not raise.
    ExtractedFacts(facts=facts)


def test_filler_and_short_sentences_dropped():
    facts = MockFactExtractor().extract("hi", "ok thanks")
    assert facts == []


def test_extract_is_deterministic():
    extractor = MockFactExtractor()
    a = extractor.extract("q text here", "a longer informative response sentence about cats")
    b = extractor.extract("q text here", "a longer informative response sentence about cats")
    assert a == b


def test_high_value_tokens_boost_importance():
    extractor = MockFactExtractor()
    plain = extractor.extract("", "the cat sat quietly on a warm mat")
    durable = extractor.extract("", "the user always prefers concise written summaries")
    assert plain and durable
    assert max(f["importance"] for f in durable) >= max(f["importance"] for f in plain)


def test_empty_interaction_returns_empty_list():
    assert MockFactExtractor().extract("", "") == []
