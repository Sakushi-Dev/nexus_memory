"""Writer loop for Nexus Memory.

:class:`MemoryWriter` owns the *write* side of the module:

* :meth:`ingest_async` decouples writes from the caller's main thread by
  spawning a background :class:`threading.Thread` and returning a UUID task id.
* :meth:`ingest_sync` runs the same pipeline inline (used by tests) and returns
  the list of written row ids.
* :meth:`_resolve_conflict` performs a vector redundancy check
  (``knn_search(k=1)``) before each write; a similarity at or above
  ``config.redundancy_threshold`` marks the fact as redundant (skipped).
* :meth:`optimize` vacuums the database and reports size/fact stats.
* :meth:`wait` joins outstanding background threads for deterministic tests.

A :class:`threading.Lock` guards the dedup-plus-write critical section, so the
*same* fact submitted twice (even concurrently) yields exactly one row.

The PII filter (:class:`nexus_memory.privacy.PIIFilter`) is applied to fact
content before embedding when ``config.pii_filter_enabled`` is set. It is
imported lazily and guarded, so the writer works even if ``privacy.py`` is not
yet present.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ...core.config import NexusConfig
from ...core.consolidation import Consolidator
from ...core.db import NexusDB
from ...core.embeddings import Embedder
from .extraction import FactExtractor

logger = logging.getLogger(__name__)


class MemoryWriter:
    """Asynchronous fact-writing pipeline with conflict resolution.

    Parameters
    ----------
    db:
        The :class:`~nexus_memory.db.NexusDB` store (owns all SQL).
    embedder:
        Encoder used to embed fact content before insertion.
    extractor:
        :class:`~nexus_memory.extraction.FactExtractor` turning an interaction
        into atomic facts.
    config:
        The active :class:`~nexus_memory.config.NexusConfig`.
    on_complete:
        Optional callback invoked as ``on_complete(task_id, written_ids)`` when
        an async ingest finishes (errors invoke it with ``written_ids=None``).
    consolidators:
        Optional list of :class:`~nexus_memory.consolidation.Consolidator`
        instances (v2 multi-layer transfer). When provided *and*
        ``config.auto_consolidate`` is set, each is invoked at the end of
        ``_ingest`` — after the semantic writes — to fan the interaction out to
        the episodic/procedural layers. A consolidator failure is logged and
        skipped; it never fails the semantic write. ``None`` (default) preserves
        the exact pre-v2 behavior.
    """

    def __init__(
        self,
        db: NexusDB,
        embedder: Embedder,
        extractor: FactExtractor,
        config: NexusConfig,
        on_complete: Callable[[str, list[int] | None], None] | None = None,
        consolidators: list[Consolidator] | None = None,
    ) -> None:
        self._db = db
        self._embedder = embedder
        self._extractor = extractor
        self._config = config
        self._on_complete = on_complete
        self._consolidators: list[Consolidator] = list(consolidators or [])

        # Guards the dedup + insert critical section so concurrent identical
        # facts collapse to a single row.
        self._write_lock = threading.Lock()
        # Tracks live background threads so wait() can join them.
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()

        # Lazily resolved PII filter (see _get_pii_filter). ``False`` is the
        # "not yet attempted" sentinel; ``None`` means unavailable.
        self._pii_filter: Any = False

    # ------------------------------------------------------------------ #
    # public ingest API
    # ------------------------------------------------------------------ #
    def ingest_async(
        self, interaction: dict, metadata: dict | None = None
    ) -> str:
        """Spawn a background thread to ingest ``interaction``.

        Returns a UUID ``task_id`` immediately; the actual write happens off the
        calling thread. Use :meth:`wait` to block until completion (tests).
        """
        task_id = str(uuid.uuid4())
        thread = threading.Thread(
            target=self._run_writer_pipeline,
            args=(task_id, interaction, metadata),
            name=f"nexus-writer-{task_id[:8]}",
            daemon=True,
        )
        with self._threads_lock:
            self._threads.append(thread)
        thread.start()
        logger.debug("ingest_async dispatched task %s", task_id)
        return task_id

    def ingest_sync(
        self, interaction: dict, metadata: dict | None = None
    ) -> list[int]:
        """Ingest ``interaction`` inline and return the written row ids.

        Runs the identical pipeline as :meth:`ingest_async` but on the caller's
        thread, which makes it deterministic for tests.
        """
        return self._ingest(interaction, metadata)

    # ------------------------------------------------------------------ #
    # background plumbing
    # ------------------------------------------------------------------ #
    def _run_writer_pipeline(
        self, task_id: str, interaction: dict, metadata: dict | None
    ) -> None:
        """Background thread target: run the pipeline and fire the callback."""
        written: list[int] | None = None
        try:
            written = self._ingest(interaction, metadata)
        except Exception:  # noqa: BLE001 - never let a worker thread die silently
            logger.exception("Writer pipeline failed for task %s", task_id)
            written = None
        finally:
            if self._on_complete is not None:
                try:
                    self._on_complete(task_id, written)
                except Exception:  # noqa: BLE001
                    logger.exception("on_complete callback raised for %s", task_id)

    def _ingest(
        self, interaction: dict, metadata: dict | None
    ) -> list[int]:
        """Core pipeline: extract -> (filter) -> dedup -> write. Returns ids."""
        query = str(interaction.get("query", ""))
        response = str(interaction.get("response", ""))

        facts = self._extractor.extract(query, response)
        written_ids: list[int] = []

        for fact in facts:
            content = str(fact.get("content", "")).strip()
            if not content:
                continue
            importance = float(fact.get("importance", 1))

            # Apply PII masking before embedding so the stored vector and text
            # both reflect the redacted content.
            content = self._apply_pii(content)

            memory_id = self._dedup_and_write(content, importance, metadata)
            if memory_id is not None:
                written_ids.append(memory_id)

        logger.debug("Ingest wrote %d/%d fact(s)", len(written_ids), len(facts))

        # v2 inter-layer transfer: after the semantic writes, fan the raw
        # interaction out to the episodic/procedural layers. Best-effort — a
        # consolidator failure is logged and skipped, never failing the write.
        self._run_consolidators(interaction, metadata, written_ids)

        return written_ids

    def _run_consolidators(
        self,
        interaction: dict,
        metadata: dict | None,
        written_ids: list[int],
    ) -> None:
        """Invoke each configured consolidator, isolating per-consolidator errors.

        No-op when no consolidators are configured or
        ``config.auto_consolidate`` is disabled.
        """
        if not self._consolidators or not self._config.auto_consolidate:
            return
        for consolidator in self._consolidators:
            try:
                consolidator.consolidate(interaction, metadata, written_ids)
            except Exception:  # noqa: BLE001 - a consolidator must not fail the write
                logger.exception(
                    "Consolidator %s failed; continuing",
                    type(consolidator).__name__,
                )

    def _dedup_and_write(
        self, content: str, importance: float, metadata: dict | None
    ) -> int | None:
        """Embed, resolve conflicts, and insert under the write lock.

        Returns the new ``rowid`` on insert, or ``None`` if the fact was deemed
        redundant. The lock makes the check-then-write atomic so identical facts
        submitted twice (even concurrently) collapse to a single row.
        """
        embedding = self._embedder.encode(content)
        with self._write_lock:
            decision = self._resolve_conflict(content, embedding)
            if decision == "redundant":
                logger.debug("Skipping redundant fact: %r", content)
                return None
            # 'insert' (and the 'update' placeholder) currently write a new row.
            return self._db.insert_memory(
                content=content,
                embedding=embedding,
                importance=importance,
                metadata=metadata,
            )

    # ------------------------------------------------------------------ #
    # conflict resolution
    # ------------------------------------------------------------------ #
    def _resolve_conflict(self, content: str, embedding: list[float]) -> str:
        """Decide how to handle a candidate fact: ``insert`` or ``redundant``.

        Performs a ``knn_search(k=1)`` and, if the nearest neighbour's cosine
        similarity is at or above ``config.redundancy_threshold``, returns
        ``'redundant'``. A fuller SLM-driven ``update``/``contradiction`` check
        is out of scope for this milestone, so we never return ``'update'``
        here (the value is reserved by the contract).
        """
        if self._db.count() == 0:
            return "insert"

        neighbours = self._db.knn_search(embedding, k=1)
        if not neighbours:
            return "insert"

        # similarity = 1 - cosine_distance (vectors are L2-normalized).
        distance = float(neighbours[0].get("distance", 1.0))
        similarity = 1.0 - distance
        if similarity >= self._config.redundancy_threshold:
            return "redundant"
        return "insert"

    # ------------------------------------------------------------------ #
    # maintenance
    # ------------------------------------------------------------------ #
    def optimize(self) -> dict:
        """Vacuum the database and report size/fact statistics.

        Returns ``{"before_bytes", "after_bytes", "facts"}``.
        """
        before = self._db_size_bytes()
        # Serialize against in-flight writes for a consistent VACUUM.
        with self._write_lock:
            self._db.vacuum()
        after = self._db_size_bytes()
        facts = self._db.count()
        report = {"before_bytes": before, "after_bytes": after, "facts": facts}
        logger.info(
            "optimize: %d -> %d bytes (%d facts)", before, after, facts
        )
        return report

    def _db_size_bytes(self) -> int:
        """Return the on-disk size of the database file in bytes (0 if N/A)."""
        db_path = self._config.db_path
        if db_path == ":memory:":
            return 0
        try:
            return Path(db_path).stat().st_size
        except OSError:
            return 0

    # ------------------------------------------------------------------ #
    # synchronization
    # ------------------------------------------------------------------ #
    def wait(self, timeout: float | None = None) -> None:
        """Join outstanding background ingest threads.

        With ``timeout=None`` this blocks until every spawned writer thread has
        finished, giving tests deterministic state.
        """
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)
        # Drop threads that have finished.
        with self._threads_lock:
            self._threads = [t for t in self._threads if t.is_alive()]

    # ------------------------------------------------------------------ #
    # PII
    # ------------------------------------------------------------------ #
    def _apply_pii(self, content: str) -> str:
        """Mask PII in ``content`` when enabled and the filter is available."""
        if not self._config.pii_filter_enabled:
            return content
        pii = self._get_pii_filter()
        if pii is None:
            return content
        try:
            return pii.mask(content)
        except Exception:  # noqa: BLE001 - never block a write on masking
            logger.exception("PIIFilter.mask failed; storing unmasked content")
            return content

    def _get_pii_filter(self) -> Any:
        """Lazily import and cache a :class:`PIIFilter`, or ``None`` if absent.

        ``privacy.py`` may be implemented by a later milestone; guard the import
        so the writer remains usable in the meantime.
        """
        if self._pii_filter is not False:
            return self._pii_filter
        try:
            from .privacy import PIIFilter  # local import: optional/late module

            self._pii_filter = PIIFilter(enabled=True)
        except Exception:  # noqa: BLE001 - module not present yet
            logger.debug("PIIFilter unavailable; PII masking disabled")
            self._pii_filter = None
        return self._pii_filter
