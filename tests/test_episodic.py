"""Tests for Layer II — Episodic Memory (the diary / dialogue history).

Validates CONTRACT-v2 section 4 against the real
:class:`~nexus_memory.episodic.EpisodicStore` API:

* ``log_interaction`` persists a user turn followed by an assistant turn;
* ``turns()`` returns oldest-first, ``recent_turns()`` newest-last;
* ``reconstruct()`` contains the roles and the content;
* ``summarize_day`` stores a non-empty narrative summary;
* turns survive being reopened from a fresh :class:`NexusDB` on the same
  ``db_path`` (durable persistence);
* an ``ingest`` fans out into episodic turns via the writer's consolidators.

Everything is offline/deterministic: the conftest ``db``/``config`` fixtures use
a ``tmp_path`` SQLite file and the default :class:`HashingEmbedder`; the episodic
store defaults to the offline :class:`MockSummarizer`.
"""

from __future__ import annotations

import pytest

from nexus_memory.core.consolidation import EpisodicConsolidator
from nexus_memory.core.db import NexusDB
from nexus_memory.layers.episodic.episodic import EpisodicStore
from nexus_memory.layers.semantic.extraction import MockFactExtractor
from nexus_memory.layers.episodic.summarization import MockSummarizer
from nexus_memory.layers.semantic.writer import MemoryWriter


@pytest.fixture
def episodic(db, config):
    """A fresh :class:`EpisodicStore` backed by the tmp-path ``db`` fixture."""
    return EpisodicStore(db, config, summarizer=MockSummarizer())


# --------------------------------------------------------------------------- #
# log_interaction -> persisted user + assistant turns
# --------------------------------------------------------------------------- #
def test_log_interaction_persists_user_then_assistant(episodic):
    """log_interaction logs a user turn, then an assistant turn, both durable."""
    assert episodic.count() == 0

    ids = episodic.log_interaction("I live in Berlin.", "Noted, Berlin it is.")

    # Two ids returned, in [user_id, assistant_id] order, both real rows.
    assert isinstance(ids, list) and len(ids) == 2
    assert ids[0] != ids[1]
    assert episodic.count() == 2

    turns = episodic.turns()
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "I live in Berlin."
    assert turns[1]["content"] == "Noted, Berlin it is."
    # Ordering of returned ids matches the persisted chronological order.
    assert [t["id"] for t in turns] == ids


def test_log_turn_returns_incrementing_ids(episodic):
    """Sequential log_turn calls get strictly increasing row ids."""
    first = episodic.log_turn("user", "first")
    second = episodic.log_turn("assistant", "second")
    assert second > first


# --------------------------------------------------------------------------- #
# ordering: turns() oldest-first, recent_turns() newest-last
# --------------------------------------------------------------------------- #
def test_turns_are_oldest_first(episodic):
    """turns() returns rows in chronological (oldest-first) order."""
    episodic.log_interaction("first user", "first assistant")
    episodic.log_interaction("second user", "second assistant")

    contents = [t["content"] for t in episodic.turns()]
    assert contents == [
        "first user",
        "first assistant",
        "second user",
        "second assistant",
    ]


def test_recent_turns_newest_last_and_capped(episodic):
    """recent_turns(n) returns the last n turns, chronological (newest-last)."""
    for i in range(5):
        episodic.log_turn("user", f"msg {i}")

    recent = episodic.recent_turns(n=3)
    # Exactly the last three, in chronological order (newest is last).
    assert [t["content"] for t in recent] == ["msg 2", "msg 3", "msg 4"]

    # The final element is the most recent turn overall.
    assert recent[-1]["content"] == "msg 4"
    # n=0 is a no-op.
    assert episodic.recent_turns(n=0) == []


def test_recent_turns_subset_of_full_history(episodic):
    """recent_turns is the tail of the full oldest-first turns() list."""
    for i in range(8):
        episodic.log_turn("user", f"t{i}")

    full = [t["content"] for t in episodic.turns()]
    recent = [t["content"] for t in episodic.recent_turns(n=4)]
    assert recent == full[-4:]


# --------------------------------------------------------------------------- #
# reconstruct() contains roles + content
# --------------------------------------------------------------------------- #
def test_reconstruct_contains_roles_and_content(episodic):
    """reconstruct() yields a transcript with both roles and verbatim content."""
    episodic.log_interaction("What is my name?", "Your name is Sam.")

    transcript = episodic.reconstruct()

    assert "user:" in transcript
    assert "assistant:" in transcript
    assert "What is my name?" in transcript
    assert "Your name is Sam." in transcript

    # Chronology preserved: the user line precedes the assistant line.
    assert transcript.index("What is my name?") < transcript.index("Your name is Sam.")
    # One line per turn.
    assert len(transcript.splitlines()) == 2


# --------------------------------------------------------------------------- #
# summarize_day stores a non-empty summary
# --------------------------------------------------------------------------- #
def test_summarize_day_stores_non_empty_summary(episodic):
    """summarize_day returns a non-empty narrative and persists it when store=True."""
    # Log a few substantive user statements so the extractive summarizer has
    # informative material to surface.
    episodic.log_interaction(
        "I am building a memory library and I prefer Python.",
        "Great, Python is a solid choice for that.",
    )
    episodic.log_interaction(
        "My deadline is next Friday and it is important.",
        "Understood, I'll keep the deadline in mind.",
    )

    # Derive the day from the stored timestamps so the bounds line up exactly.
    day = episodic.turns()[0]["timestamp"].split(" ", 1)[0]

    result = episodic.summarize_day(day, store=True)

    assert result["period"] == day
    assert result["turn_count"] == 4
    assert isinstance(result["summary"], str)
    assert result["summary"].strip()  # non-empty
    # The extractive summary should mention something the user actually said.
    assert "Python" in result["summary"]

    # And it was persisted to episodic_summaries.
    stored = episodic.summaries()
    assert len(stored) == 1
    assert stored[0]["period"] == day
    assert stored[0]["summary"] == result["summary"]
    assert stored[0]["turn_count"] == 4


def test_summarize_day_no_store_does_not_persist(episodic):
    """summarize_day(store=False) still returns a summary but persists nothing."""
    episodic.log_interaction("I work on a research project.", "Sounds interesting.")
    day = episodic.turns()[0]["timestamp"].split(" ", 1)[0]

    result = episodic.summarize_day(day, store=False)

    assert result["summary"].strip()
    assert episodic.summaries() == []


def test_summarize_empty_day_is_empty(episodic):
    """A day with no turns yields an empty summary and stores nothing."""
    result = episodic.summarize_day("1999-01-01", store=True)
    assert result["turn_count"] == 0
    assert result["summary"] == ""
    assert episodic.summaries() == []


# --------------------------------------------------------------------------- #
# persistence across a fresh NexusDB on the same db_path
# --------------------------------------------------------------------------- #
def test_turns_persist_across_reopen(config, db_path):
    """Turns written by one EpisodicStore are visible from a fresh NexusDB."""
    # First session: write, then close the connection cleanly.
    db1 = NexusDB(config)
    try:
        store1 = EpisodicStore(db1, config)
        store1.log_interaction("Remember my favorite color is green.", "Got it.")
        store1.summarize_day(
            store1.turns()[0]["timestamp"].split(" ", 1)[0], store=True
        )
        assert store1.count() == 2
    finally:
        db1.close()

    # Second session: a brand-new NexusDB on the SAME file path (the config
    # already points at db_path; NexusDB, not the config, owns the connection).
    assert config.db_path == db_path
    db2 = NexusDB(config)
    try:
        store2 = EpisodicStore(db2, config)
        # Raw turns survived.
        assert store2.count() == 2
        turns = store2.turns()
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["content"] == "Remember my favorite color is green."
        # The stored summary survived too.
        summaries = store2.summaries()
        assert len(summaries) == 1
        assert summaries[0]["summary"].strip()
    finally:
        db2.close()


# --------------------------------------------------------------------------- #
# consolidation: an ingest fans out into episodic turns (via the writer)
# --------------------------------------------------------------------------- #
def test_ingest_consolidates_into_episodic(db, config, embedder):
    """An ingest through the writer logs the interaction into the episodic store.

    Exercises the real EpisodicConsolidator wired into MemoryWriter; uses
    ``ingest_sync`` so the consolidation runs deterministically on this thread.
    """
    episodic = EpisodicStore(db, config)
    consolidator = EpisodicConsolidator(episodic, lambda: "session-xyz")
    writer = MemoryWriter(
        db,
        embedder,
        MockFactExtractor(),
        config,
        consolidators=[consolidator],
    )

    writer.ingest_sync(
        {"query": "I prefer concise answers.", "response": "Understood."}
    )

    turns = episodic.turns()
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "I prefer concise answers."
    assert turns[1]["content"] == "Understood."
    # The consolidator tagged the turns with the provided session id.
    assert all(t["session_id"] == "session-xyz" for t in turns)
