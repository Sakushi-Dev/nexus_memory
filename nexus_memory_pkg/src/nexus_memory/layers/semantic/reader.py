"""Reader loop: query -> retrieve -> graph-expand -> score -> XML.

:class:`MemoryReader` implements the read path of Nexus Memory. Given a query
it embeds it (optionally short-circuiting via the semantic cache), retrieves
nearest neighbours from the vector store, expands the candidate pool by one
graph hop over the top hits, re-ranks everything with the multi-signal scorer,
filters by ``min_score`` and renders a prompt-ready ``<memory_context>`` block.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from ...core import scoring, xml_format
from ...core.cache import SemanticCache
from ...core.config import NexusConfig
from ...core.db import NexusDB
from ...core.embeddings import Embedder

logger = logging.getLogger(__name__)


class MemoryReader:
    """Assemble scored, XML-formatted memory context for a query."""

    def __init__(
        self,
        db: NexusDB,
        embedder: Embedder,
        config: NexusConfig,
        cache: SemanticCache | None = None,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.config = config
        self.cache = cache

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def assemble_context(self, request: dict, now: datetime | None = None) -> dict:
        """Build a memory context for ``request``.

        Steps:
            1. Embed the query (consult the semantic cache first, if present).
            2. KNN search for ``top_k * 2`` candidates (over-retrieve to re-rank).
            3. Expand 1 graph hop from the top hits via :meth:`NexusDB.neighbors`.
            4. Re-rank all candidates with the multi-signal scorer.
            5. Filter by ``min_score`` and render to ``<memory_context>`` XML.

        Args:
            request: Dict with ``query`` and optional ``top_k`` / ``min_score``.
            now: Injectable reference time for deterministic time-decay.

        Returns:
            ``{"status", "context_xml", "raw_facts", "meta": {"tokens_estimated",
            "source_count"}, "latency_ms"}``.
        """
        start = time.perf_counter()

        query = request.get("query", "")
        top_k = int(request.get("top_k", self.config.default_top_k))
        min_score = float(request.get("min_score", self.config.min_score))

        embedding = self.embedder.encode(query)

        # 1. Optional cache short-circuit on the query embedding.
        if self.cache is not None:
            cached = self.cache.get(embedding)
            if cached is not None:
                logger.debug("Reader cache hit for query %r", query)
                result = dict(cached)
                result["latency_ms"] = (time.perf_counter() - start) * 1000.0
                return result

        # 2. Over-retrieve so the re-ranking has headroom.
        candidates = self.db.knn_search(embedding, k=max(1, top_k * 2))

        # 3. Lightweight 1-hop graph expansion over the strongest hits.
        candidates = self._expand_graph(candidates)

        # 4. Multi-signal re-ranking.
        ranked = scoring.rank(candidates, self.config, now=now)

        # 5. Filter by minimum score and cap to the requested top_k.
        kept = [row for row in ranked if row["score"] >= min_score][:top_k]

        context_xml = xml_format.format_as_xml(kept)
        raw_facts = [self._to_fact(row) for row in kept]

        result = {
            "status": "success",
            "context_xml": context_xml,
            "raw_facts": raw_facts,
            "meta": {
                "tokens_estimated": xml_format.estimate_tokens(context_xml),
                "source_count": len(kept),
            },
            "latency_ms": (time.perf_counter() - start) * 1000.0,
        }

        if self.cache is not None:
            # Cache the assembled result keyed by the query embedding.
            self.cache.put(embedding, result)

        return result

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _expand_graph(self, candidates: list[dict]) -> list[dict]:
        """Add 1-hop neighbours of the top hits to the candidate pool.

        Neighbours already present in the pool are skipped; newly pulled rows
        carry no ``distance`` (they were not vector matches) and are scored on
        importance/recency alone by the ranker.
        """
        if not candidates:
            return candidates

        seen_ids = {row["id"] for row in candidates}
        expanded = list(candidates)

        # Expand from the strongest (smallest-distance) hits — the pool from
        # knn_search is already distance-ascending.
        for hit in candidates[: max(1, self.config.default_top_k)]:
            for neighbor_id in self.db.neighbors(hit["id"]):
                if neighbor_id in seen_ids:
                    continue
                neighbor = self.db.get_memory(neighbor_id)
                if neighbor is None:
                    continue
                seen_ids.add(neighbor_id)
                expanded.append(neighbor)

        return expanded

    @staticmethod
    def _to_fact(row: dict) -> dict:
        """Project a scored row into the Fact-shaped dict returned to callers."""
        return {
            "id": row.get("id"),
            "content": row.get("content", ""),
            "score": round(float(row.get("score", 0.0)), 4),
            "timestamp": str(row.get("timestamp", "")),
        }
