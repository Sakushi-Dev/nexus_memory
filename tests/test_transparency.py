"""Isolated tests for :class:`TransparencyInterface`.

Focuses on the ``forget(query=...)`` relevance floor (audit R10): an unrelated
query must NOT delete the nearest stored memory, while a genuine paraphrase
still resolves to and deletes the intended row.
"""

from __future__ import annotations

import pytest

from nexus_memory.core.transparency import TransparencyInterface


@pytest.fixture
def transparency(db, embedder, config):
    """A standalone TransparencyInterface over the tmp db/embedder/config."""
    return TransparencyInterface(db=db, embedder=embedder, config=config)


def test_forget_query_unrelated_does_not_delete(transparency, db, embedder):
    """An unrelated query stays below the similarity floor → not_found, no delete."""
    fact = "The user lives in Berlin and works as a backend engineer."
    fact_id = db.insert_memory(fact, embedder.encode(fact))

    res = transparency.forget(query="totally unrelated gibberish xyzzy")

    assert res["status"] == "not_found"
    assert res["deleted_id"] is None
    # The fact survives the unrelated forget attempt.
    assert db.get_memory(fact_id) is not None


def test_forget_query_paraphrase_deletes(transparency, db, embedder):
    """A close paraphrase clears the floor and deletes the matching memory."""
    fact = "The user lives in Berlin and works as a backend engineer."
    fact_id = db.insert_memory(fact, embedder.encode(fact))

    res = transparency.forget(query="backend engineer who works and lives in Berlin")

    assert res["status"] == "success"
    assert res["deleted_id"] == fact_id
    assert db.get_memory(fact_id) is None
