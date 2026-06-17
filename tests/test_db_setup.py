"""MS1: infrastructure — extension loading, schema, WAL, CRUD/KNN."""

from __future__ import annotations

import sqlite_vec

from nexus_memory.core.db import NexusDB


def test_sqlite_vec_extension_loads(db):
    """sqlite-vec is loaded: a vec0 virtual table query works."""
    version = db.conn.execute("SELECT vec_version()").fetchone()[0]
    assert isinstance(version, str) and version


def test_required_tables_exist(db):
    """agent_memory, system_config and memory_edges exist after init."""
    names = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    assert "agent_memory" in names
    assert "system_config" in names
    assert "memory_edges" in names


def test_wal_mode_enabled(db):
    """PRAGMA journal_mode must report 'wal' (MS1.4)."""
    mode = db.conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_insert_and_knn_roundtrip(db, embedder):
    """Insert returns a rowid and KNN finds the inserted vector."""
    vec = embedder.encode("the quick brown fox jumps")
    rowid = db.insert_memory("the quick brown fox jumps", vec, importance=5.0)
    assert isinstance(rowid, int) and rowid > 0

    results = db.knn_search(vec, k=1)
    assert len(results) == 1
    top = results[0]
    assert top["id"] == rowid
    assert top["content"] == "the quick brown fox jumps"
    assert top["importance"] == 5.0
    # Identical vector -> ~0 cosine distance.
    assert top["distance"] < 1e-4


def test_insert_populates_timestamp(db, embedder):
    """vec0 ignores column DEFAULTs, so insert must set the timestamp itself.

    Regression: a NULL timestamp silently disables the time-decay scoring signal.
    """
    rid = db.insert_memory("fact with a timestamp", embedder.encode("fact with a timestamp"))
    got = db.get_memory(rid)
    assert got["timestamp"], "timestamp must be populated, not None/empty"
    # Parseable as 'YYYY-MM-DD HH:MM:SS'.
    from datetime import datetime

    datetime.strptime(got["timestamp"], "%Y-%m-%d %H:%M:%S")


def test_update_preserves_timestamp(db, embedder):
    """A content correction keeps the original creation time (recency stable)."""
    rid = db.insert_memory("first version", embedder.encode("first version"))
    original = db.get_memory(rid)["timestamp"]
    db.update_memory(rid, "second version", embedder.encode("second version"))
    assert db.get_memory(rid)["timestamp"] == original


def test_metadata_roundtrips_as_dict(db, embedder):
    """Metadata is stored as JSON and returned as a dict."""
    vec = embedder.encode("payload with metadata")
    rid = db.insert_memory("payload with metadata", vec, metadata={"k": "v", "n": 3})
    got = db.get_memory(rid)
    assert got is not None
    assert got["metadata"] == {"k": "v", "n": 3}


def test_update_is_delete_plus_insert(db, embedder):
    """update_memory rewrites content while keeping count stable."""
    rid = db.insert_memory("original content here", embedder.encode("original content here"))
    db.update_memory(rid, "brand new content text", embedder.encode("brand new content text"))
    assert db.count() == 1
    got = db.get_memory(rid)
    assert got["content"] == "brand new content text"


def test_delete_memory(db, embedder):
    """delete_memory removes the row and reports success."""
    rid = db.insert_memory("to be deleted soon", embedder.encode("to be deleted soon"))
    assert db.delete_memory(rid) is True
    assert db.get_memory(rid) is None
    assert db.delete_memory(rid) is False


def test_edges_and_neighbors(db, embedder):
    """add_edge/neighbors implement a 1-hop graph."""
    a = db.insert_memory("node a content", embedder.encode("node a content"))
    b = db.insert_memory("node b content", embedder.encode("node b content"))
    db.add_edge(a, b)
    assert db.neighbors(a) == [b]
    assert db.neighbors(b) == []
    # Idempotent on the primary key.
    db.add_edge(a, b)
    assert db.neighbors(a) == [b]


def test_serialize_float32_used(db, embedder):
    """The serialized vector blob length matches dim * 4 bytes."""
    vec = embedder.encode("dimension check")
    blob = sqlite_vec.serialize_float32(vec)
    assert len(blob) == db.config.dim * 4


def test_count_and_close(config):
    """count reflects inserts; conn raises after close."""
    database = NexusDB(config)
    e_vec = [0.0] * config.dim
    e_vec[0] = 1.0
    database.insert_memory("one", e_vec)
    assert database.count() == 1
    database.close()
    import pytest

    with pytest.raises(RuntimeError):
        _ = database.conn
