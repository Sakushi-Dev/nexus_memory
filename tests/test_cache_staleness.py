"""Regression test for R9 — semantic-cache staleness on read-after-write.

The reader caches a fully-assembled retrieval result keyed by the query
embedding (``cache_threshold`` default 0.95). Before the fix no write path ever
invalidated that cache, so a query issued *after* an ``ingest`` kept returning
the *old* assembled result and the freshly-stored fact was invisible.

The fix invalidates the cache on every mutation (here: when the async ingest
commits, via the writer's completion callback). This test populates the cache,
ingests a clearly-relevant needle, waits for the background write, and asserts
the needle is now visible through the same query. It MUST pass after the fix and
fail before it.
"""

from __future__ import annotations

import pytest

from nexus_memory import NexusMemory

_QUERY = "What pet does the user have?"
_NEEDLE = "The user has a golden retriever named Rufus."


@pytest.fixture
def nexus(db_path):
    nm = NexusMemory(db_path=db_path)
    try:
        yield nm
    finally:
        nm.close()


def _assemble_text(nexus, query: str) -> str:
    """Return the assembled context as one searchable string."""
    res = nexus.process({"action": "assemble", "query": query})
    assert res["status"] == "success"
    facts = " ".join(f.get("content", "") for f in res.get("raw_facts", []))
    return res.get("context_xml", "") + " " + facts


def _semantic_facts(nexus, query: str) -> str:
    """Return only the cached *semantic* facts (the layer ``forget`` mutates).

    The assembled ``context_xml`` also embeds ``recent_dialogue`` (working/
    episodic turns), which ``forget`` legitimately does not delete; scoping to
    ``raw_facts`` isolates the semantic read-cache behaviour under test.
    """
    res = nexus.process({"action": "assemble", "query": query})
    assert res["status"] == "success"
    return " ".join(f.get("content", "") for f in res.get("raw_facts", []))


def test_assemble_sees_fact_ingested_after_cache_populated(nexus):
    # 1. Seed an unrelated fact and assemble the query once. This populates the
    #    semantic cache keyed by the query embedding.
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "I work as a carpenter.",
                "response": "Noted.",
            },
        }
    )
    nexus.wait()

    before = _assemble_text(nexus, _QUERY)  # populates the cache
    assert "Rufus" not in before

    # 2. Ingest a clearly-relevant needle and let the background write commit.
    nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": _NEEDLE, "response": "Got it."},
        }
    )
    nexus.wait()

    # 3. The SAME query must now surface the needle. Before the cache-invalidation
    #    fix this returned the stale cached result and "Rufus" was missing.
    after = _assemble_text(nexus, _QUERY)
    assert "Rufus" in after, (
        "stale read after write: needle ingested after the cache was populated "
        "is not visible — cache was not invalidated on mutation"
    )


def test_forget_invalidates_cache(nexus):
    """A delete must not keep serving the removed fact from cache."""
    nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": _NEEDLE, "response": "Got it."},
        }
    )
    nexus.wait()

    seeded = _semantic_facts(nexus, _QUERY)  # populates the cache with the needle
    assert "Rufus" in seeded

    res = nexus.process({"action": "forget", "query": _NEEDLE})
    assert res["status"] == "success"

    after = _semantic_facts(nexus, _QUERY)
    assert "Rufus" not in after, (
        "stale read after delete: forgotten fact still served from cache"
    )
