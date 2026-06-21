"""Database layer for Nexus Memory.

:class:`NexusDB` owns **all** SQL. It connects to SQLite, loads the
``sqlite-vec`` extension, initializes the schema from ``schema.sql`` (with the
``__DIM__`` token replaced by ``config.dim``), enables WAL mode, and exposes a
small typed CRUD + KNN API. Vectors are serialized with
:func:`sqlite_vec.serialize_float32` before binding.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from .config import NexusConfig

logger = logging.getLogger(__name__)

# Token in schema.sql replaced by the configured dimension at init time.
_DIM_TOKEN = "__DIM__"


def _utc_now_str() -> str:
    """Current UTC time as ``YYYY-MM-DD HH:MM:SS`` (matches CURRENT_TIMESTAMP).

    vec0 auxiliary (``+``) columns do **not** honor a column ``DEFAULT``, so the
    timestamp must be supplied explicitly on every insert — otherwise it stays
    NULL and the time-decay scoring signal is silently disabled.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _find_schema() -> Path:
    """Locate the packaged ``schema.sql``.

    It ships as package data next to ``nexus_memory/__init__.py`` (see
    ``[tool.setuptools.package-data]`` in ``pyproject.toml``), so the same path
    resolves for source checkouts, editable installs, and built wheels.
    """
    schema = Path(__file__).resolve().parents[1] / "schema.sql"
    if not schema.is_file():
        raise FileNotFoundError(f"schema.sql could not be located at {schema}")
    return schema


class NexusDB:
    """SQLite + sqlite-vec backed store. Owns all SQL for Nexus Memory."""

    def __init__(self, config: NexusConfig) -> None:
        self.config = config
        self._conn: sqlite3.Connection | None = None
        # Shared access lock for the single SQLite connection. Guards both the
        # writes from the layer stores (semantic + episodic + procedural) and
        # the reads below, so a foreground reader never interleaves a cursor
        # with the writer thread's commit on the same connection. Re-entrant so
        # a method holding it may call another that also acquires it (e.g.
        # update_memory -> get_memory). Used by the layer stores via
        # ``with db.lock:``.
        self.lock: threading.RLock = threading.RLock()
        self.initialize()

    # ------------------------------------------------------------------ #
    # connection / lifecycle
    # ------------------------------------------------------------------ #
    @property
    def conn(self) -> sqlite3.Connection:
        """The live SQLite connection (raises if the DB has been closed)."""
        if self._conn is None:
            raise RuntimeError("NexusDB connection is closed.")
        return self._conn

    def initialize(self) -> None:
        """Open the connection, load sqlite-vec, apply schema, enable WAL."""
        conn = sqlite3.connect(self.config.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        schema_sql = _find_schema().read_text(encoding="utf-8")
        schema_sql = schema_sql.replace(_DIM_TOKEN, str(self.config.dim))
        conn.executescript(schema_sql)

        # WAL for concurrent reader/writer access; NORMAL sync for speed.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.commit()

        self._conn = conn
        logger.debug("NexusDB initialized at %s (dim=%d)", self.config.db_path, self.config.dim)

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("NexusDB connection closed.")

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def insert_memory(
        self,
        content: str,
        embedding: list[float],
        importance: float = 1.0,
        metadata: dict | None = None,
    ) -> int:
        """Insert a memory row and return its integer ``rowid``."""
        blob = sqlite_vec.serialize_float32(embedding)
        meta_json = json.dumps(metadata) if metadata is not None else None
        # Serialize the commit under the shared write lock: the connection is
        # shared across the writer thread(s) and the layer stores, and SQLite
        # raises on interleaved commits from different threads on one connection.
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO agent_memory (embedding, content, metadata, importance, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (blob, content, meta_json, importance, _utc_now_str()),
            )
            self.conn.commit()
        return int(cur.lastrowid)

    def update_memory(
        self,
        memory_id: int,
        content: str,
        embedding: list[float],
        importance: float | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Update a memory as DELETE + re-INSERT (vec0 has no in-place UPDATE).

        The ``rowid`` is preserved by explicitly re-inserting with the same id.
        """
        existing = self.get_memory(memory_id)
        if importance is None:
            importance = existing["importance"] if existing else 1.0
        if metadata is None and existing is not None:
            metadata = existing.get("metadata")
        # Preserve the original creation time on a content correction so an edit
        # does not artificially boost the fact's recency score.
        timestamp = (existing.get("timestamp") if existing else None) or _utc_now_str()

        blob = sqlite_vec.serialize_float32(embedding)
        meta_json = json.dumps(metadata) if metadata is not None else None
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM agent_memory WHERE rowid = ?", (memory_id,))
            self.conn.execute(
                "INSERT INTO agent_memory (rowid, embedding, content, metadata, importance, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (memory_id, blob, content, meta_json, importance, timestamp),
            )

    def delete_memory(self, memory_id: int) -> bool:
        """Delete a memory by id. Returns ``True`` if a row was removed."""
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM agent_memory WHERE rowid = ?", (memory_id,)
            )
            self.conn.commit()
        return cur.rowcount > 0

    def add_edge(self, source_id: int, target_id: int, relation: str = "related") -> None:
        """Add a 1-hop edge between two memories (idempotent on the PK)."""
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO memory_edges (source_id, target_id, relation) "
                "VALUES (?, ?, ?)",
                (source_id, target_id, relation),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def knn_search(self, embedding: list[float], k: int) -> list[dict]:
        """Return the ``k`` nearest memories ordered by cosine distance ASC.

        Each dict: ``{id, content, importance, timestamp, metadata(dict),
        distance(float)}``. ``id`` is the row's ``rowid``.
        """
        blob = sqlite_vec.serialize_float32(embedding)
        # Reads share the single connection with the writer thread; serialize
        # them under the same (re-entrant) lock as writes so a reader cursor is
        # never interleaved with a writer commit on the same connection.
        with self.lock:
            rows = self.conn.execute(
                "SELECT rowid AS id, content, importance, timestamp, metadata, distance "
                "FROM agent_memory "
                "WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                (blob, k),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_memory(self, memory_id: int) -> dict | None:
        """Fetch a single memory by id, or ``None`` if it does not exist."""
        with self.lock:
            row = self.conn.execute(
                "SELECT rowid AS id, content, importance, timestamp, metadata "
                "FROM agent_memory WHERE rowid = ?",
                (memory_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def neighbors(self, memory_id: int) -> list[int]:
        """Return 1-hop target ids reachable from ``memory_id`` via edges."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT target_id FROM memory_edges WHERE source_id = ?",
                (memory_id,),
            ).fetchall()
        return [int(r["target_id"]) for r in rows]

    def all_memories(
        self,
        limit: int = 50,
        time_range: tuple[str, str] | None = None,
    ) -> list[dict]:
        """Return memories ordered by newest first, optionally time-filtered."""
        with self.lock:
            if time_range is not None:
                start, end = time_range
                rows = self.conn.execute(
                    "SELECT rowid AS id, content, importance, timestamp, metadata "
                    "FROM agent_memory WHERE timestamp >= ? AND timestamp <= ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (start, end, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT rowid AS id, content, importance, timestamp, metadata "
                    "FROM agent_memory ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self) -> int:
        """Return the total number of stored memories."""
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM agent_memory").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    # system_config (key/value bookkeeping) — embedder provenance (0.7.0)
    # ------------------------------------------------------------------ #
    def get_config(self, key: str) -> str | None:
        """Return the ``system_config`` value for ``key`` (``None`` if absent)."""
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM system_config WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row["value"]

    def set_config(self, key: str, value: str) -> None:
        """Insert or replace a ``system_config`` row (no DDL — plain upsert)."""
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
                (key, value),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # maintenance
    # ------------------------------------------------------------------ #
    def vacuum(self) -> None:
        """Run VACUUM to reclaim space (also checkpoints WAL)."""
        with self.lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            self.conn.execute("VACUUM;")
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """Convert a result row into a plain dict, parsing metadata JSON."""
        d = dict(row)
        raw_meta = d.get("metadata")
        if raw_meta:
            try:
                d["metadata"] = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
        else:
            d["metadata"] = {}
        if "distance" in d and d["distance"] is not None:
            d["distance"] = float(d["distance"])
        if "importance" in d and d["importance"] is not None:
            d["importance"] = float(d["importance"])
        return d
