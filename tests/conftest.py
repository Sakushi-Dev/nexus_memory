"""Shared pytest fixtures for the Nexus Memory test suite.

All database-backed tests use ``tmp_path`` so they never touch the working
directory, and everything uses the default :class:`HashingEmbedder` (offline,
deterministic, no model downloads).
"""

from __future__ import annotations

import pytest

from nexus_memory.core.config import NexusConfig
from nexus_memory.core.db import NexusDB
from nexus_memory.core.embeddings import HashingEmbedder


@pytest.fixture
def db_path(tmp_path):
    """Return a unique on-disk SQLite path inside the test's tmp dir."""
    return str(tmp_path / "nexus_test.db")


@pytest.fixture
def config(db_path):
    """A NexusConfig pointed at the tmp database."""
    return NexusConfig(db_path=db_path)


@pytest.fixture
def embedder(config):
    """Default hashing embedder sized to the config dimension."""
    return HashingEmbedder(dim=config.dim)


@pytest.fixture
def db(config):
    """An initialized NexusDB, closed on teardown."""
    database = NexusDB(config)
    try:
        yield database
    finally:
        database.close()
