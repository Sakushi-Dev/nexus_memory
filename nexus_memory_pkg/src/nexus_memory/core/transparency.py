"""Transparency interface for Nexus Memory.

:class:`TransparencyInterface` gives the user full sovereignty over the stored
memory: read-mostly *inspection* (health / episodic / semantic views) plus an
explicit edit surface (``forget`` / ``update`` / ``pin``). It is a thin,
deterministic layer over :class:`~nexus_memory.db.NexusDB`; all SQL lives in the
DB layer, so this module only orchestrates calls and shapes the response.

The interface is strictly local-only (Phase 9, section 5): it never opens a
network socket and operates directly on the ``.db`` file via ``NexusDB``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from .config import NexusConfig
from .db import NexusDB
from .embeddings import Embedder

if TYPE_CHECKING:  # pragma: no cover - typing-only imports (avoid import cycles)
    from .procedural import ProceduralStore
    from .working import WorkingMemory

logger = logging.getLogger(__name__)

# How many leading vector dimensions to expose in a human-facing preview before
# the ellipsis marker (Phase 9 spec: first 4 dims + "...").
_VECTOR_PREVIEW_DIMS = 4


class TransparencyInterface:
    """Inspect and correct the internal state of the memory.

    Parameters
    ----------
    db:
        The backing :class:`~nexus_memory.db.NexusDB` (owns all SQL).
    embedder:
        Embedder used to re-embed content on :meth:`update` / :meth:`pin` and to
        resolve a free-text query on :meth:`forget`.
    config:
        Active :class:`~nexus_memory.config.NexusConfig`.
    """

    def __init__(self, db: NexusDB, embedder: Embedder, config: NexusConfig) -> None:
        self.db = db
        self.embedder = embedder
        self.config = config
        # Optional layer references, injected by the orchestrator after
        # construction so inspect() can surface Layer I (working) and Layer IV
        # (procedural) state. They stay ``None`` when the interface is used
        # standalone (e.g. in semantic-only tests).
        self.working: "WorkingMemory | None" = None
        self.procedural: "ProceduralStore | None" = None

    # ------------------------------------------------------------------ #
    # inspect (read-mostly)
    # ------------------------------------------------------------------ #
    def inspect(self, type: str = "health", filter: dict | None = None) -> dict:
        """Inspect the memory and return ``{"status": ..., "data": [...]}``.

        Parameters
        ----------
        type:
            One of ``"health"`` (counts and DB size), ``"episodic"``
            (chronological entries), ``"semantic"`` (entries with a short
            ``vector_preview``), ``"working"`` (the volatile Layer I buffer), or
            ``"procedural"`` (the standing Layer IV directives).
        filter:
            Optional filter dict. Recognized keys: ``time_range``
            (``[start, end]`` ISO strings) and ``limit`` (int, default 50).

        Returns
        -------
        dict
            ``{"status": "success", "data": [...]}`` on success, or
            ``{"status": "error", "error": str, "data": []}`` for an unknown
            ``type``.
        """
        filter = filter or {}
        if type == "health":
            return {"status": "success", "data": self._inspect_health()}
        if type == "episodic":
            return {"status": "success", "data": self._inspect_episodic(filter)}
        if type == "semantic":
            return {"status": "success", "data": self._inspect_semantic(filter)}
        if type == "working":
            return {"status": "success", "data": self._inspect_working()}
        if type == "procedural":
            return {"status": "success", "data": self._inspect_procedural(filter)}

        logger.warning("inspect called with unknown type %r", type)
        return {
            "status": "error",
            "error": f"unknown inspect type: {type!r}",
            "data": [],
        }

    def _inspect_health(self) -> list[dict]:
        """Return a single health record: memory count and DB file size."""
        count = self.db.count()
        db_path = self.config.db_path
        size_bytes = self._db_size_bytes(db_path)
        return [
            {
                "count": count,
                "db_path": db_path,
                "db_size_bytes": size_bytes,
                "dim": self.config.dim,
            }
        ]

    def _inspect_episodic(self, filter: dict) -> list[dict]:
        """Return memories chronologically (newest first), optionally filtered."""
        limit, time_range = self._read_filter(filter)
        rows = self.db.all_memories(limit=limit, time_range=time_range)
        return [
            {
                "id": row["id"],
                "timestamp": row.get("timestamp"),
                "content": row.get("content"),
                "importance": row.get("importance"),
                "metadata": row.get("metadata", {}),
            }
            for row in rows
        ]

    def _inspect_semantic(self, filter: dict) -> list[dict]:
        """Return memories with a short human-facing ``vector_preview``.

        Vectors are not human-readable, so each entry exposes only the first few
        dimensions followed by an ``"..."`` marker (Phase 9, section 3).
        """
        limit, time_range = self._read_filter(filter)
        rows = self.db.all_memories(limit=limit, time_range=time_range)
        out: list[dict] = []
        for row in rows:
            content = row.get("content") or ""
            vector = self.embedder.encode(content)
            out.append(
                {
                    "id": row["id"],
                    "timestamp": row.get("timestamp"),
                    "content": content,
                    "importance": row.get("importance"),
                    "metadata": row.get("metadata", {}),
                    "vector_preview": self._vector_preview(vector),
                }
            )
        return out

    def _inspect_working(self) -> list[dict]:
        """Return the volatile Layer I working-memory turns (newest-last).

        Returns ``[]`` when no working memory is wired (standalone usage).
        """
        if self.working is None:
            logger.debug("inspect(working) with no working memory injected")
            return []
        return self.working.snapshot()

    def _inspect_procedural(self, filter: dict) -> list[dict]:
        """Return the standing Layer IV procedural directives.

        ``filter`` may carry ``active_only`` (bool, default ``True``). Returns
        ``[]`` when no procedural store is wired (standalone usage).
        """
        if self.procedural is None:
            logger.debug("inspect(procedural) with no procedural store injected")
            return []
        active_only = bool(filter.get("active_only", True))
        return self.procedural.list_rules(active_only=active_only)

    # ------------------------------------------------------------------ #
    # CRUD / edit-mode
    # ------------------------------------------------------------------ #
    def forget(self, fact_id: int | None = None, query: str | None = None) -> dict:
        """Delete a memory by id, or by best semantic match of ``query``.

        Exactly one of ``fact_id`` / ``query`` must be supplied.

        Returns
        -------
        dict
            ``{"status": "success", "deleted_id": int}`` when a row was removed,
            otherwise ``{"status": "error" | "not_found", ...}``.
        """
        if (fact_id is None) == (query is None):
            return {
                "status": "error",
                "error": "provide exactly one of fact_id or query",
            }

        if fact_id is None:
            # Resolve the free-text query to the single best matching memory.
            embedding = self.embedder.encode(query or "")
            matches = self.db.knn_search(embedding, k=1)
            if not matches:
                return {"status": "not_found", "deleted_id": None, "query": query}
            fact_id = int(matches[0]["id"])

        deleted = self.db.delete_memory(int(fact_id))
        if not deleted:
            return {"status": "not_found", "deleted_id": None, "fact_id": fact_id}
        logger.info("forget removed memory id=%s", fact_id)
        return {"status": "success", "deleted_id": int(fact_id)}

    def update(self, target_id: int, new_content: str) -> dict:
        """Re-embed ``new_content`` and overwrite the memory ``target_id``.

        Returns ``{"status": "not_found", ...}`` if ``target_id`` does not exist.
        """
        existing = self.db.get_memory(target_id)
        if existing is None:
            return {"status": "not_found", "updated_id": None, "target_id": target_id}

        embedding = self.embedder.encode(new_content)
        self.db.update_memory(target_id, new_content, embedding)
        logger.info("update re-embedded memory id=%s", target_id)
        return {
            "status": "success",
            "updated_id": int(target_id),
            "content": new_content,
        }

    def pin(self, content: str, importance: float = 10.0) -> dict:
        """Manually add a high-importance fact the AI "must never forget".

        Returns ``{"status": "success", "id": int, "importance": float}``.
        """
        embedding = self.embedder.encode(content)
        # vec0 auxiliary FLOAT column rejects a bound INTEGER, so coerce.
        importance = float(importance)
        memory_id = self.db.insert_memory(
            content=content,
            embedding=embedding,
            importance=importance,
            metadata={"pinned": True},
        )
        logger.info("pin added memory id=%s (importance=%s)", memory_id, importance)
        return {
            "status": "success",
            "id": int(memory_id),
            "content": content,
            "importance": float(importance),
        }

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_filter(filter: dict) -> tuple[int, tuple[str, str] | None]:
        """Extract ``limit`` and ``time_range`` from a filter dict."""
        limit = int(filter.get("limit", 50))
        raw_range = filter.get("time_range")
        time_range: tuple[str, str] | None = None
        if raw_range and len(raw_range) == 2:
            time_range = (str(raw_range[0]), str(raw_range[1]))
        return limit, time_range

    @staticmethod
    def _vector_preview(vector: list[float]) -> list[Any]:
        """Return the first few rounded dims followed by an ``"..."`` marker."""
        head = [round(float(v), 4) for v in vector[:_VECTOR_PREVIEW_DIMS]]
        head.append("...")
        return head

    @staticmethod
    def _db_size_bytes(db_path: str) -> int:
        """Total on-disk size of the DB and its WAL/SHM sidecar files (bytes)."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            try:
                total += os.path.getsize(path)
            except OSError:
                # In-memory DB or sidecar not present yet; skip silently.
                continue
        return total
