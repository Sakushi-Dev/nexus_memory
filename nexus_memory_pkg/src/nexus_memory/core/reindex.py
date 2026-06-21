"""Transactional same-dim re-embed / re-index tool (0.7.0).

Switching the embedder changes the vector SPACE, so every stored vector must be
recomputed before the new embedder may serve reads — otherwise old and new
vectors are silently mixed and search is corrupt (the provenance guard in
:mod:`nexus_memory.core.provenance` refuses to even open such a store).

This module ships the recommended **same-dimension** migration (e.g. Hashing
768 -> bge-base 768): read every ``(id, content)`` from the vector table,
recompute embeddings with the new embedder, ``UPDATE`` each row inside ONE
transaction, then stamp the new provenance. No schema change, no approval
needed. A **different-dimension** rebuild (a future static tier) is OUT OF SCOPE
for 0.7.0 and raises :class:`NotImplementedError`.

CLI::

    python -m nexus_memory.reindex --db <path> --backend fastembed [--model ...]
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import NexusConfig
from .db import NexusDB
from .embeddings import Embedder, FastEmbedEmbedder, HashingEmbedder
from . import provenance

logger = logging.getLogger(__name__)


# ===================================================================== #
# embedder construction for the migration
# ===================================================================== #
def _make_embedder(
    backend: str,
    *,
    model: str | None,
    cache_dir: str | None,
    dim: int,
) -> Embedder:
    """Build the target embedder for the re-embed (mirrors orchestrator wiring)."""
    if backend == "fastembed":
        return FastEmbedEmbedder(
            model or "BAAI/bge-base-en-v1.5", cache_dir=cache_dir
        )
    if backend == "hashing":
        return HashingEmbedder(dim=dim)
    raise ValueError(
        f"unknown backend {backend!r}; expected 'hashing' or 'fastembed'"
    )


# ===================================================================== #
# same-dim re-embed
# ===================================================================== #
def reembed(
    db_path: str,
    *,
    backend: str,
    model: str | None = None,
    cache_dir: str | None = None,
) -> dict:
    """Re-embed every row of the semantic store with a new SAME-DIM embedder.

    Opens ``db_path``, builds the target embedder, recomputes all vectors with
    :meth:`Embedder.encode_batch`, ``UPDATE``-s each row inside a single
    transaction, then updates the ``system_config`` provenance and clears any
    semantic cache association. The dimension is preserved (no schema change).

    Returns ``{"status", "rows", "backend", "model", "dim"}``. A different-dim
    target raises :class:`NotImplementedError` (out of scope for 0.7.0).
    """
    # Open the DB at its existing dimension (provenance, else default config dim).
    config = NexusConfig(db_path=db_path)
    db = NexusDB(config)
    try:
        existing_dim = provenance.stored_dim(db, fallback=config.dim)

        new_embedder = _make_embedder(
            backend, model=model, cache_dir=cache_dir, dim=existing_dim
        )

        if int(new_embedder.dim) != int(existing_dim):
            raise NotImplementedError(
                f"different-dimension rebuild not supported in 0.7.0: target "
                f"embedder dim {new_embedder.dim} != DB dim {existing_dim}. A "
                "dim change requires a fresh-DB rebuild deferred to a future "
                "release; pick a "
                f"{existing_dim}-dim model instead."
            )

        # Pull every (id, content) from the vector store.
        rows = db.all_memories(limit=10_000_000)
        ids = [int(r["id"]) for r in rows]
        contents = [str(r["content"]) for r in rows]

        new_vectors = (
            new_embedder.encode_batch(contents) if contents else []
        )

        # Re-embed inside ONE transaction so a failure leaves the store intact.
        import sqlite_vec

        with db.lock, db.conn:
            for memory_id, vec in zip(ids, new_vectors):
                blob = sqlite_vec.serialize_float32(vec)
                db.conn.execute(
                    "UPDATE agent_memory SET embedding = ? WHERE rowid = ?",
                    (blob, memory_id),
                )

        # Stamp the new provenance now that all vectors live in the new space.
        provenance.write_provenance(db, new_embedder)

        logger.info(
            "reembed: %d row(s) -> backend=%s model=%s dim=%d",
            len(ids),
            backend,
            getattr(new_embedder, "model_name", backend),
            existing_dim,
        )
        return {
            "status": "success",
            "rows": len(ids),
            "backend": backend,
            "model": getattr(new_embedder, "model_name", backend),
            "dim": existing_dim,
        }
    finally:
        db.close()


# ===================================================================== #
# CLI
# ===================================================================== #
def main(argv: list[str] | None = None) -> int:
    """``python -m nexus_memory.reindex`` entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m nexus_memory.reindex",
        description="Transactionally re-embed a Nexus Memory store with a new "
        "same-dimension embedder.",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite DB")
    parser.add_argument(
        "--backend",
        required=True,
        choices=["hashing", "fastembed"],
        help="target embedder backend",
    )
    parser.add_argument(
        "--model", default=None, help="override the backend's default model"
    )
    parser.add_argument(
        "--cache-dir", default=None, help="model cache directory (fastembed)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = reembed(
        args.db,
        backend=args.backend,
        model=args.model,
        cache_dir=args.cache_dir,
    )
    print(
        f"reindex: re-embedded {result['rows']} row(s) -> "
        f"{result['backend']} ({result['model']}, dim={result['dim']})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI shim
    sys.exit(main())
