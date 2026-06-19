"""Embedding backends for Nexus Memory.

The default :class:`HashingEmbedder` is deterministic and dependency-free: it
implements a signed feature-hashing vectorizer (a "hashing trick" vectorizer)
so that vectors carry *lexical* overlap. Paraphrases that share salient words
therefore retrieve each other, which is what the needle-in-a-haystack
integration test relies on.

Optional adapters (:class:`SentenceTransformerEmbedder`,
:class:`OpenAIEmbedder`) are lazily imported and never required at package
import time.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod

from .config import DEFAULT_DIM

logger = logging.getLogger(__name__)

# Tokenizer: lowercase, split on non-alphanumeric runs.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase ``text`` and split into alphanumeric tokens."""
    return _TOKEN_RE.findall(text.lower())


def _l2_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit L2 norm (zero vector returned unchanged)."""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    inv = 1.0 / norm
    return [v * inv for v in vec]


class Embedder(ABC):
    """Abstract base class for text embedders.

    Implementations MUST return an L2-normalized vector of length ``self.dim``
    so that cosine similarity equals the dot product.
    """

    dim: int

    @abstractmethod
    def encode(self, text: str) -> list[float]:
        """Encode ``text`` into an L2-normalized vector of length ``self.dim``."""
        raise NotImplementedError

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of texts. Default: map :meth:`encode` over ``texts``."""
        return [self.encode(t) for t in texts]


class HashingEmbedder(Embedder):
    """Deterministic, dependency-free signed feature-hashing embedder.

    Tokenizes text (lowercase, split on non-alphanumeric), hashes each token
    into ``[0, dim)`` for the bucket index and uses a second hash bit for the
    sign (to reduce collision bias), accumulates counts, then L2-normalizes.

    Determinism across processes is guaranteed by using :func:`hashlib.blake2b`
    rather than Python's randomized built-in ``hash()``.
    """

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be a positive integer")
        self.dim = dim

    @staticmethod
    def _digest(token: str) -> int:
        """Return a stable 64-bit integer digest of ``token``."""
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=8)
        return int.from_bytes(h.digest(), "big")

    def encode(self, text: str) -> list[float]:
        """Encode ``text`` into an L2-normalized feature-hashed vector."""
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            d = self._digest(token)
            index = d % self.dim
            # Use a high bit for the sign so it is independent of the bucket.
            sign = 1.0 if (d >> 63) & 1 else -1.0
            vec[index] += sign
        return _l2_normalize(vec)


class SentenceTransformerEmbedder(Embedder):
    """Optional adapter around ``sentence-transformers`` (lazy import).

    Raises :class:`ImportError` with guidance if the optional dependency is not
    installed. Not imported at package import time.
    """

    def __init__(self, model_name: str = "all-mpnet-base-v2", **kwargs: object) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional path
            raise ImportError(
                "SentenceTransformerEmbedder requires the 'sentence-transformers' "
                "package. Install it with: pip install sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(model_name, **kwargs)  # type: ignore[arg-type]
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.model_name = model_name

    def encode(self, text: str) -> list[float]:
        """Encode ``text`` with the underlying SentenceTransformer model."""
        vec = self._model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]


class OpenAIEmbedder(Embedder):
    """Optional adapter around the OpenAI embeddings API (lazy import).

    Raises :class:`ImportError` with guidance if the optional dependency is not
    installed. Not imported at package import time.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
        **kwargs: object,
    ) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional path
            raise ImportError(
                "OpenAIEmbedder requires the 'openai' package. "
                "Install it with: pip install openai"
            ) from exc
        self._client = OpenAI(**kwargs)  # type: ignore[arg-type]
        self.model = model
        self.dim = dim

    def encode(self, text: str) -> list[float]:
        """Encode ``text`` via the OpenAI embeddings endpoint."""
        resp = self._client.embeddings.create(
            model=self.model, input=text, dimensions=self.dim
        )
        return _l2_normalize([float(x) for x in resp.data[0].embedding])
