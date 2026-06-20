"""Regression tests for the §5 writer/db concurrency + dedup fixes.

* ``SW-05`` — :meth:`MemoryWriter._resolve_conflict` is namespace-aware: a
  textually near-identical fact in a *different* logical scope is kept instead
  of being silently dropped, while same-scope (and scope-less) duplicates still
  collapse to one row.
* ``persistence-01`` — concurrent async ingests and foreground reads share one
  SQLite connection; a smoke test hammers both paths to assert no
  ``sqlite3.ProgrammingError`` ("recursive use of cursors") leaks out.

Everything uses the offline, deterministic :class:`HashingEmbedder` so identical
text embeds identically (similarity 1.0 ≥ the 0.90 redundancy threshold).
"""

from __future__ import annotations

import threading

import pytest

from nexus_memory import NexusMemory
from nexus_memory.core.config import NexusConfig
from nexus_memory.core.db import NexusDB
from nexus_memory.core.embeddings import HashingEmbedder
from nexus_memory.layers.semantic.extraction import MockFactExtractor
from nexus_memory.layers.semantic.writer import MemoryWriter


@pytest.fixture
def writer(db_path):
    """A bare :class:`MemoryWriter` over a tmp on-disk DB (closed on teardown)."""
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


# --------------------------------------------------------------------------- #
# SW-05 — namespace-aware dedup
# --------------------------------------------------------------------------- #
_FACT = "The deployment region is eu-west-1."


def test_same_scope_duplicate_is_skipped(writer):
    """Identical text in the same namespace still collapses to one row."""
    w, db = writer
    first = w._dedup_and_write(_FACT, 1.0, {"namespace": "tenant-a"})
    second = w._dedup_and_write(_FACT, 1.0, {"namespace": "tenant-a"})
    assert first is not None
    assert second is None  # redundant within the same scope
    assert db.count() == 1


def test_cross_namespace_duplicate_is_kept(writer):
    """The same text under a different namespace is a distinct fact, not lost."""
    w, db = writer
    first = w._dedup_and_write(_FACT, 1.0, {"namespace": "tenant-a"})
    second = w._dedup_and_write(_FACT, 1.0, {"namespace": "tenant-b"})
    assert first is not None
    assert second is not None  # different scope -> inserted, not dropped
    assert db.count() == 2


def test_scopeless_duplicate_preserves_original_behavior(writer):
    """With no scope metadata, dedup behaves exactly as before (skip dup)."""
    w, db = writer
    first = w._dedup_and_write(_FACT, 1.0, None)
    second = w._dedup_and_write(_FACT, 1.0, None)
    assert first is not None
    assert second is None
    assert db.count() == 1


# --------------------------------------------------------------------------- #
# persistence-01 — shared-connection concurrency smoke
# --------------------------------------------------------------------------- #
@pytest.fixture
def nexus(db_path):
    nm = NexusMemory(db_path=db_path)
    try:
        yield nm
    finally:
        nm.close()


def test_concurrent_ingest_and_read_no_cursor_error(nexus):
    """Foreground reads interleaved with background writes must not raise.

    Before the fix, reads issued the cursor on the shared connection without the
    write lock; interleaving with the writer thread's commit could raise
    ``ProgrammingError`` / "recursive use of cursors". Serializing reads under
    the same lock removes that race.
    """
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for i in range(60):
                nexus.process(
                    {"action": "assemble", "query": f"region {i}", "top_k": 5}
                )
        except BaseException as exc:  # noqa: BLE001 - capture for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()

    # Hammer the writer path concurrently with the readers above.
    for i in range(60):
        nexus.process(
            {
                "action": "ingest",
                "interaction": {"query": f"I deploy to region-{i}.", "response": "ok"},
            }
        )

    for t in threads:
        t.join()
    nexus.wait()

    assert not errors, f"concurrent read/write raised: {errors!r}"
