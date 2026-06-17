"""The default SpeakerAwareExtractor attributes facts and drops noise.

Regression for the misattribution seen in real chat use: the assistant's own
utterances were stored speaker-less, so a downstream model later misremembered
who said what. These tests pin the corrected behaviour.
"""

from __future__ import annotations

from nexus_memory import NexusMemory, SpeakerAwareExtractor
from nexus_memory.layers.semantic.extraction import FactExtractor
from nexus_memory.core.models import ExtractedFacts


def _contents(query: str, response: str) -> list[str]:
    return [f["content"] for f in SpeakerAwareExtractor().extract(query, response)]


def test_is_factextractor_subclass():
    assert issubclass(SpeakerAwareExtractor, FactExtractor)


def test_output_validates_against_schema():
    facts = SpeakerAwareExtractor().extract("My name is John.", "You live in Berlin.")
    ExtractedFacts(facts=facts)  # must not raise


def test_default_is_user_centric():
    """By default only USER turns become semantic facts (assistant -> diary only)."""
    facts = _contents(
        "kannst du in zukunft deutsch mit mir reden?",
        "Ja, natürlich! Ich spreche ab jetzt Deutsch mit dir. Wie geht's dir heute?",
    )
    # The user's request is captured and attributed to the user...
    assert any(f.startswith("User:") and "deutsch" in f.lower() for f in facts)
    # ...and NO assistant prose leaks into semantic memory.
    assert all(f.startswith("User:") for f in facts)


def test_include_assistant_keeps_attributed_assistant_facts():
    """With include_assistant=True, the assistant's statements are kept AND tagged."""
    facts = [
        f["content"]
        for f in SpeakerAwareExtractor(include_assistant=True).extract(
            "kannst du in zukunft deutsch mit mir reden?",
            "Ja, natürlich! Ich spreche ab jetzt Deutsch mit dir. Wie geht's dir heute?",
        )
    ]
    assert any(f.startswith("Assistant:") and "Deutsch" in f for f in facts)
    assert any(f.startswith("User:") for f in facts)
    # No fact is ambiguous about its speaker.
    assert all(f.startswith(("User:", "Assistant:")) for f in facts)
    # The assistant's trailing question ("Wie geht's dir heute?") is still dropped.
    assert not any("wie geht" in f.lower() for f in facts)


def test_assistant_questions_and_filler_dropped():
    facts = _contents("Hey", "Hey there! How's it going? What can I help you with today?")
    assert facts == [], f"expected no facts, got {facts}"


def test_number_survives_user_question():
    facts = _contents("kannst du dir eine zahl merken? 3658?", "Ja, klar!")
    assert any("3658" in f for f in facts), f"number lost: {facts}"
    assert not any("klar" in f.lower() for f in facts)  # "Ja, klar!" is filler


def test_user_name_is_high_importance():
    facts = SpeakerAwareExtractor().extract("Mein Name ist Chris.", "Schön!")
    user = [f for f in facts if f["content"].startswith("User:")]
    assert user and "Chris" in user[0]["content"]
    assert user[0]["importance"] >= 6


def test_default_extractor_is_speaker_aware(db_path):
    """NexusMemory uses SpeakerAwareExtractor unless overridden."""
    nm = NexusMemory(db_path=db_path)
    try:
        assert isinstance(nm.extractor, SpeakerAwareExtractor)
        nm.process(
            {
                "action": "ingest",
                "interaction": {"query": "Ich heiße Chris.", "response": "Hallo Chris!"},
            }
        )
        nm.wait()
        rows = nm.db.all_memories(limit=10)
        assert any(r["content"].startswith("User:") and "Chris" in r["content"] for r in rows)
    finally:
        nm.close()
