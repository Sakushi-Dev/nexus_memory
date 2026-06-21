"""Tests for the 0.7.0 local-first semantic embedder (fastembed + provenance).

The neural tests are gated with ``pytest.importorskip("fastembed")`` so the core
suite still passes without the optional ``nexus-memory[local-embeddings]`` extra.
In this repo fastembed IS installed (BAAI/bge-base-en-v1.5, dim=768), so they
run — and the headline regression (semantic recall of a PARAPHRASED fact that
lexical hashing misses) is exercised end to end.

Provenance + dim-guard tests that don't need the model run unconditionally.
"""

from __future__ import annotations

import math

import pytest

from nexus_memory import NexusConfig, NexusMemory
from nexus_memory.core.db import NexusDB
from nexus_memory.core.embeddings import HashingEmbedder
from nexus_memory.core.reindex import reembed


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
# The canonical lexical-recall failure: a paraphrased query that shares NO
# salient words with the stored fact, so signed feature-hashing cannot retrieve
# it but a real semantic embedder can.
_TIDEGLASS_FACT = "I'm building a Rust CLI tool called Tideglass"
_PARAPHRASE_QUERY = "what is the exact name of my project and which language?"

_DISTRACTORS = [
    ("My favorite color is teal.", "Noted, teal it is."),
    ("I usually have coffee in the morning.", "Sounds good."),
    ("The weather has been rainy this week.", "Bring an umbrella."),
    ("I enjoy hiking on weekends.", "Nice hobby!"),
]


def _ingest(nexus: NexusMemory, query: str, response: str) -> None:
    """Synchronously ingest one interaction through the writer (deterministic)."""
    nexus.writer.ingest_sync({"query": query, "response": response})


def _seed(nexus: NexusMemory) -> None:
    """Ingest the Tideglass fact plus distractors into ``nexus``."""
    _ingest(nexus, _TIDEGLASS_FACT, "Great, Tideglass it is.")
    for q, r in _DISTRACTORS:
        _ingest(nexus, q, r)


def _tideglass_rank(nexus: NexusMemory) -> int | None:
    """Return the 0-based rank of the Tideglass fact in raw_facts, else None."""
    result = nexus.context.assemble(
        {"query": _PARAPHRASE_QUERY, "top_k": 5, "min_score": 0.0}
    )
    facts = result["raw_facts"]
    for i, fact in enumerate(facts):
        if "Tideglass" in fact.get("content", ""):
            return i
    return None


# --------------------------------------------------------------------------- #
# fastembed adapter
# --------------------------------------------------------------------------- #
def test_fastembed_dim_and_normalized():
    """FastEmbedEmbedder reports dim==768 and returns unit-norm vectors."""
    pytest.importorskip("fastembed")
    from nexus_memory.core.embeddings import FastEmbedEmbedder

    embedder = FastEmbedEmbedder()
    assert embedder.dim == 768

    vec = embedder.encode("a small probe sentence")
    assert len(vec) == 768
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-4)


# --------------------------------------------------------------------------- #
# headline regression: semantic recall beats lexical recall
# --------------------------------------------------------------------------- #
def test_semantic_recall_beats_lexical(tmp_path):
    """The paraphrase retrieves Tideglass under fastembed but not under hashing."""
    pytest.importorskip("fastembed")

    hashing = NexusMemory(str(tmp_path / "hashing.db"))
    neural = NexusMemory(
        str(tmp_path / "neural.db"),
        config=NexusConfig(embedder_backend="fastembed"),
    )
    try:
        _seed(hashing)
        _seed(neural)

        neural_rank = _tideglass_rank(neural)
        hashing_rank = _tideglass_rank(hashing)

        # fastembed surfaces the fact and ranks it at/near the top.
        assert neural_rank is not None, "fastembed failed to recall Tideglass"
        assert neural_rank <= 1, f"Tideglass ranked too low under fastembed: {neural_rank}"

        # hashing either misses it entirely or ranks it strictly worse.
        assert hashing_rank is None or hashing_rank > neural_rank
    finally:
        hashing.close()
        neural.close()


# --------------------------------------------------------------------------- #
# dim guard
# --------------------------------------------------------------------------- #
def test_dim_guard_rejects_mismatch(tmp_path):
    """An embedder whose dim != DB dim is rejected with a clear ValueError."""
    db_path = str(tmp_path / "mismatch.db")

    # Fresh 768-dim DB (default config dim). Inject a 256-dim hashing embedder.
    with pytest.raises(ValueError, match="embedder dim 256 != DB dim 768"):
        NexusMemory(db_path, embedder=HashingEmbedder(dim=256))

    # No corrupt write occurred: the store is empty / never embedded.
    nexus = NexusMemory(db_path)
    try:
        assert nexus.db.count() == 0
    finally:
        nexus.close()


# --------------------------------------------------------------------------- #
# provenance: refuse silent vector-space mixing
# --------------------------------------------------------------------------- #
def test_provenance_mismatch_refuses(tmp_path):
    """Reopening a hashing DB as fastembed (no reindex) raises, naming reindex."""
    pytest.importorskip("fastembed")
    db_path = str(tmp_path / "prov.db")

    nexus = NexusMemory(db_path)
    try:
        _seed(nexus)
    finally:
        nexus.close()

    with pytest.raises(ValueError, match="reindex"):
        NexusMemory(db_path, config=NexusConfig(embedder_backend="fastembed"))


# --------------------------------------------------------------------------- #
# re-embed round-trip
# --------------------------------------------------------------------------- #
def test_reembed_roundtrip(tmp_path):
    """reembed(hashing->fastembed) preserves rows/content, flips provenance, recalls."""
    pytest.importorskip("fastembed")
    db_path = str(tmp_path / "reembed.db")

    # Build a hashing DB and capture the original rows + a sample vector.
    nexus = NexusMemory(db_path)
    try:
        _seed(nexus)
        before_rows = nexus.db.all_memories(limit=1000)
        before_count = nexus.db.count()
        # Capture one raw embedding to prove vectors actually change.
        sample = nexus.db.knn_search(nexus.embedder.encode(_TIDEGLASS_FACT), k=1)
        assert sample
    finally:
        nexus.close()

    # Re-embed to fastembed (same dim, transactional).
    result = reembed(db_path, backend="fastembed")
    assert result["status"] == "success"
    assert result["rows"] == before_count
    assert result["dim"] == 768

    # Reopen under fastembed — provenance now matches, no error.
    nexus2 = NexusMemory(db_path, config=NexusConfig(embedder_backend="fastembed"))
    try:
        after_rows = nexus2.db.all_memories(limit=1000)
        # Same N rows, same content (by id).
        assert nexus2.db.count() == before_count
        before_by_id = {r["id"]: r["content"] for r in before_rows}
        after_by_id = {r["id"]: r["content"] for r in after_rows}
        assert before_by_id == after_by_id

        # Vectors changed: the paraphrase now retrieves Tideglass.
        rank = _tideglass_rank(nexus2)
        assert rank is not None and rank <= 1
    finally:
        nexus2.close()


# --------------------------------------------------------------------------- #
# offline after a warm cache
# --------------------------------------------------------------------------- #
def test_offline_after_warm_cache():
    """With the model cached, offline=True constructs and encodes (no network)."""
    pytest.importorskip("fastembed")
    from nexus_memory.core.embeddings import FastEmbedEmbedder

    embedder = FastEmbedEmbedder(offline=True)
    assert embedder.dim == 768
    vec = embedder.encode("offline probe")
    assert len(vec) == 768
    assert math.sqrt(sum(x * x for x in vec)) == pytest.approx(1.0, abs=1e-4)
