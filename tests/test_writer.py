"""Behavioral writer dedup through the public ingest pipeline (audit TQ-04).

This exercises :meth:`MemoryWriter._resolve_conflict` (``writer.py:230-251``,
the redundancy gate — *not* a non-existent ``_is_redundant``) end-to-end via
the public :meth:`MemoryWriter.ingest_sync` surface, rather than poking the
private ``_dedup_and_write`` helper. Ingesting the *same* fact twice (same
scope) must collapse to a single stored row; a genuinely different fact must
still be inserted, so the test fails loudly on both an always-insert and an
always-skip regression.

Everything uses the offline, deterministic :class:`HashingEmbedder`: identical
text embeds identically (cosine similarity 1.0 ≥ the 0.90 ``redundancy_threshold``).
"""

from __future__ import annotations

import pytest

from nexus_memory.core.config import NexusConfig
from nexus_memory.core.db import NexusDB
from nexus_memory.core.embeddings import HashingEmbedder
from nexus_memory.layers.semantic.extraction import MockFactExtractor
from nexus_memory.layers.semantic.writer import MemoryWriter


@pytest.fixture
def writer(db_path):
    """A :class:`MemoryWriter` over a tmp on-disk DB (closed on teardown)."""
    config = NexusConfig(db_path=db_path)
    db = NexusDB(config)
    embedder = HashingEmbedder(dim=config.dim)
    w = MemoryWriter(
        db=db,
        embedder=embedder,
        extractor=MockFactExtractor(),
        config=config,
    )
    try:
        yield w, db
    finally:
        db.close()


# An informative sentence the MockFactExtractor keeps as a single atomic fact.
# The query is left empty so each ingest yields exactly one fact (the extractor
# mines both query and response), isolating the dedup behaviour under test.
_FACT = "The production database lives in the eu-west-1 region."


def test_duplicate_ingest_yields_one_row(writer):
    """Ingesting the same fact twice collapses to a single stored row."""
    w, db = writer

    first = w.ingest_sync({"query": "", "response": _FACT})
    second = w.ingest_sync({"query": "", "response": _FACT})

    # First write lands; the redundancy gate drops the identical re-ingest.
    assert len(first) == 1
    assert second == []  # _resolve_conflict -> "redundant"
    assert db.count() == 1


def test_distinct_fact_is_not_deduped(writer):
    """A genuinely different fact still inserts — guards against always-skip."""
    w, db = writer

    w.ingest_sync({"query": "", "response": _FACT})
    other = w.ingest_sync(
        {"query": "", "response": "The service runs on Python 3.12."}
    )

    assert len(other) == 1  # distinct content -> inserted, not dropped
    assert db.count() == 2
