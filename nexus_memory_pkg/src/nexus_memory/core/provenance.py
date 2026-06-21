"""Embedder provenance bookkeeping (0.7.0).

A vector store is only meaningful when every row was embedded by the SAME
embedder: switching backend/model (even at the same dimension, e.g. Hashing 768
-> bge-base 768) produces vectors in a DIFFERENT space, so silently mixing them
corrupts search. This module records the active embedder's identity (backend +
model + dim) in the existing ``system_config`` table — adding rows, NOT a schema
change — and refuses to open a store whose stored provenance disagrees with the
configured embedder, pointing the host at the re-index tool.

The helpers here own NO SQL of their own; they go through
:meth:`NexusDB.get_config` / :meth:`NexusDB.set_config`.
"""

from __future__ import annotations

import logging

from .embeddings import Embedder, FastEmbedEmbedder, HashingEmbedder

logger = logging.getLogger(__name__)

# ===================================================================== #
# system_config keys (rows, not columns — zero DDL)
# ===================================================================== #
_KEY_BACKEND = "embedder_backend"
_KEY_MODEL = "embedder_model"
_KEY_DIM = "embedder_dim"


def embedder_provenance(embedder: Embedder) -> dict[str, str]:
    """Return the ``{backend, model, dim}`` identity of ``embedder`` as strings.

    HashingEmbedder -> ``{backend:"hashing", model:"hashing", dim}``;
    FastEmbedEmbedder -> ``{backend:"fastembed", model:<model_name>, dim}``; any
    other adapter falls back to its class name as the backend/model.
    """
    if isinstance(embedder, HashingEmbedder):
        backend, model = "hashing", "hashing"
    elif isinstance(embedder, FastEmbedEmbedder):
        backend = "fastembed"
        model = getattr(embedder, "model_name", "fastembed")
    else:
        name = type(embedder).__name__
        backend = name
        model = str(getattr(embedder, "model_name", getattr(embedder, "model", name)))
    return {
        _KEY_BACKEND: backend,
        _KEY_MODEL: str(model),
        _KEY_DIM: str(int(embedder.dim)),
    }


def read_provenance(db) -> dict[str, str] | None:
    """Return the stored provenance dict, or ``None`` for a fresh DB (no row)."""
    backend = db.get_config(_KEY_BACKEND)
    if backend is None:
        return None
    return {
        _KEY_BACKEND: backend,
        _KEY_MODEL: db.get_config(_KEY_MODEL) or "",
        _KEY_DIM: db.get_config(_KEY_DIM) or "",
    }


def write_provenance(db, embedder: Embedder) -> None:
    """Persist ``embedder``'s provenance into ``system_config`` (upsert)."""
    prov = embedder_provenance(embedder)
    db.set_config(_KEY_BACKEND, prov[_KEY_BACKEND])
    db.set_config(_KEY_MODEL, prov[_KEY_MODEL])
    db.set_config(_KEY_DIM, prov[_KEY_DIM])
    logger.debug("Wrote embedder provenance: %s", prov)


def stored_dim(db, *, fallback: int) -> int:
    """Return the DB's real stored vector dim from provenance, else ``fallback``.

    A fresh DB has no provenance row yet; the table dimension was created from
    ``config.dim``, so ``fallback`` (``config.dim``) is the correct width there.
    """
    prov = read_provenance(db)
    if prov is None or not prov.get(_KEY_DIM):
        return int(fallback)
    try:
        return int(prov[_KEY_DIM])
    except (TypeError, ValueError):
        return int(fallback)


def reconcile(db, embedder: Embedder) -> bool:
    """Reconcile ``embedder`` against the store's provenance.

    On a FRESH store (no provenance row) the embedder's provenance is written and
    ``True`` is returned (caller should flush the read cache for cleanliness). On
    an EXISTING store whose provenance MATCHES (same backend+model+dim) this is a
    no-op returning ``False``. On a MISMATCH it raises a clear :class:`ValueError`
    pointing at the re-index tool — never silently mixing vector spaces.
    """
    current = embedder_provenance(embedder)
    stored = read_provenance(db)

    if stored is None:
        # Fresh store: stamp the active embedder's identity.
        write_provenance(db, embedder)
        return True

    if stored == current:
        return False

    raise ValueError(
        "embedder provenance mismatch: this database was built with "
        f"backend={stored.get(_KEY_BACKEND)!r} model={stored.get(_KEY_MODEL)!r} "
        f"dim={stored.get(_KEY_DIM)!r}, but the configured embedder is "
        f"backend={current[_KEY_BACKEND]!r} model={current[_KEY_MODEL]!r} "
        f"dim={current[_KEY_DIM]!r}. Old and new vectors live in different "
        "spaces; refusing to mix them. Re-embed the store with: "
        "python -m nexus_memory.reindex --db <path> --backend "
        f"{current[_KEY_BACKEND]}"
        + (
            f" --model {current[_KEY_MODEL]}"
            if current[_KEY_BACKEND] == "fastembed"
            else ""
        )
    )
