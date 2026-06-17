"""Thread-safe semantic LRU cache.

Caches values keyed by query embeddings. A lookup is a hit when the maximum
cosine similarity between the query and any cached key is at or above the
configured threshold. Vectors are assumed L2-normalized (cosine == dot
product); the implementation normalizes defensively anyway.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class SemanticCache:
    """An LRU cache with cosine-similarity (semantic) key matching."""

    def __init__(self, maxsize: int = 128, threshold: float = 0.95) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self.threshold = threshold
        self._lock = threading.RLock()
        # key: tuple(embedding) -> value; ordered for LRU semantics.
        self._store: "OrderedDict[tuple[float, ...], Any]" = OrderedDict()

    @staticmethod
    def _to_unit(embedding: list[float]) -> np.ndarray:
        vec = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return vec
        return vec / norm

    def get(self, query_embedding: list[float]) -> Any | None:
        """Return the cached value whose key is most similar to the query.

        Returns the value if the best cosine similarity is ``>= threshold``,
        otherwise ``None``. A hit refreshes the entry's LRU recency.
        """
        q = self._to_unit(query_embedding)
        with self._lock:
            if not self._store:
                return None
            keys = list(self._store.keys())
            matrix = np.asarray(keys, dtype=np.float32)
            sims = matrix @ q  # keys are stored unit-normalized
            best_idx = int(np.argmax(sims))
            best_sim = float(sims[best_idx])
            if best_sim >= self.threshold:
                key = keys[best_idx]
                self._store.move_to_end(key)
                return self._store[key]
            return None

    def put(self, query_embedding: list[float], value: Any) -> None:
        """Insert/refresh a value keyed by ``query_embedding`` (LRU eviction)."""
        key = tuple(float(x) for x in self._to_unit(query_embedding).tolist())
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = value
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    def clear(self) -> None:
        """Remove all cached entries."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
