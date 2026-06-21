# Embedders

This page explains how Nexus Memory turns text into vectors: the [`Embedder`](../../src/nexus_memory/core/embeddings.py) abstract base class and its contract, the default dependency-free [`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py), and the optional, lazily-imported adapters — the **recommended local semantic** [`FastEmbedEmbedder`](../../src/nexus_memory/core/embeddings.py) (0.7.0, provider-agnostic: downloads a model once, then runs offline), plus [`SentenceTransformerEmbedder`](../../src/nexus_memory/core/embeddings.py) and [`OpenAIEmbedder`](../../src/nexus_memory/core/embeddings.py) — including which dependencies they need, how their dimensions are determined, and the privacy implications of sending text to an external API. **(0.7.0)** you can also select a backend declaratively via `NexusConfig(embedder_backend=…)` and re-embed an existing store with `python -m nexus_memory.reindex`.

All embedding code lives in [`src/nexus_memory/core/embeddings.py`](../../src/nexus_memory/core/embeddings.py).

## The `Embedder` contract

Every embedder subclasses the `Embedder` ABC and satisfies a small, strict contract. The same contract is relied on by the retrieval math (cosine similarity), the [semantic cache](../architecture/retrieval-and-scoring.md), and the database schema.

```python
class Embedder(ABC):
    dim: int

    @abstractmethod
    def encode(self, text: str) -> list[float]:
        """Encode text into an L2-normalized vector of length self.dim."""

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """Default: map encode() over texts. Override for efficiency."""
        return [self.encode(t) for t in texts]
```

The contract has three load-bearing rules:

| Rule | Why it matters |
|------|----------------|
| `encode(text)` returns a Python `list[float]` of length exactly `self.dim`. | The vector is stored against a fixed-width column in the DB; a wrong length corrupts retrieval. |
| The returned vector is **L2-normalized** (unit length). | Cosine similarity then reduces to a plain dot product, which is what scoring and the [semantic cache](../architecture/retrieval-and-scoring.md) compute. |
| `self.dim` **must match** [`config.dim`](../configuration/nexus-config.md). | The dimension is fixed at table creation and validated when the store is built. Mismatching it requires a re-embed/migration. |

`encode_batch` is provided for free by the base class as a `map` over `encode`; adapters that have a native batch path (such as a model that vectorizes many strings in one call) may override it for throughput, but are not required to.

### How the embedder is wired in

The orchestrator defaults the embedder to a `HashingEmbedder` sized to the config, so by default no model download and no network access occur:

```python
# src/nexus_memory/core/orchestrator.py
self.embedder: Embedder = embedder or HashingEmbedder(dim=config.dim)
```

You may pass any `Embedder` instance via the `embedder` argument of [`NexusMemory`](api-reference.md); when you do, keep `config.dim` in sync with `embedder.dim`. See [Choosing an embedder](#choosing-an-embedder-summary) below and the [API reference](api-reference.md) for the full constructor.

## `HashingEmbedder` — the default

`HashingEmbedder` is the default backend: it is deterministic, dependency-free, and fully offline. It implements a **signed feature-hashing vectorizer** (a "hashing trick" vectorizer) that produces vectors carrying *lexical* overlap — paraphrases that share salient words retrieve each other, which is exactly what the needle-in-a-haystack integration test relies on (see [Use Cases — Persistent Agent Memory](../use-cases/agent-memory.md)).

```python
from nexus_memory.core.embeddings import HashingEmbedder

emb = HashingEmbedder()        # dim defaults to DEFAULT_DIM == 768
v = emb.encode("where do I keep my house keys?")
assert len(v) == emb.dim       # 768
```

### How it works

1. **Tokenize.** Text is lowercased and split on non-alphanumeric runs via the regex `[a-z0-9]+`, yielding a list of alphanumeric tokens.
2. **Hash each token.** Each token is digested with `hashlib.blake2b` (8-byte / 64-bit digest), deliberately **not** Python's built-in `hash()`, whose per-process randomization would make vectors non-reproducible across runs and processes.
3. **Bucket + sign.** The digest modulo `dim` picks the bucket index in `[0, dim)`; a high bit (bit 63) of the same digest picks the sign (`+1.0` or `-1.0`). Using an independent bit for the sign reduces collision bias.
4. **Accumulate, then normalize.** Signed counts are accumulated per bucket, and the resulting vector is L2-normalized (a zero vector — e.g. empty/non-alphanumeric text — is returned unchanged).

### Properties

| Property | Value |
|----------|-------|
| Default dimension | `768` (`DEFAULT_DIM`); configurable via the `dim` constructor argument |
| Dependencies | none |
| Network / model download | none — fully offline |
| Determinism | identical across processes and machines (blake2b, not `hash()`) |
| Similarity signal | **lexical** overlap (shared tokens), not learned semantics |
| Validation | `dim <= 0` raises `ValueError("dim must be a positive integer")` |

Because the signal is lexical rather than semantic, `HashingEmbedder` retrieves on shared vocabulary — it will not match two phrases that mean the same thing but share no words. For semantic matching, use one of the optional adapters below.

## Optional adapters

These adapters are **lazily imported**: their third-party dependency is only imported inside `__init__`, never at package import time. If the dependency is missing, the constructor raises a helpful `ImportError` telling you exactly which extra to install.

### `FastEmbedEmbedder` *(recommended local semantic — 0.7.0)*

A **provider-agnostic, local** semantic embedder built on [`fastembed`](https://github.com/qdrant/fastembed) (ONNX Runtime — **no PyTorch**). It downloads a small model once (default `BAAI/bge-base-en-v1.5`, ~210 MB) and then runs fully **offline** on CPU (a few ms per text). Unlike `HashingEmbedder`, it matches on *meaning*: a paraphrased query retrieves facts that share no words (e.g. *"what's my project's name?"* finds *"I'm building a tool called Tideglass"*, which the lexical default misses).

Its default model is **768-dimensional — the same as `DEFAULT_DIM`** — so adopting it on a fresh store needs **no schema/dim change**, and migrating an existing store is a pure [re-embed](#switching-embedders--re-indexing).

```python
from nexus_memory import NexusMemory, NexusConfig

# Declarative (recommended): pick the backend in config.
memory = NexusMemory("agent.db", config=NexusConfig(embedder_backend="fastembed"))

# …or inject the adapter explicitly:
from nexus_memory.core.embeddings import FastEmbedEmbedder
memory = NexusMemory("agent.db", embedder=FastEmbedEmbedder())
```

- **Install:** `pip install "nexus-memory[local-embeddings]"` (pulls `fastembed[cpu]`). Missing → `ImportError` with that hint.
- **Constructor:** `FastEmbedEmbedder(model_name="BAAI/bge-base-en-v1.5", *, cache_dir=None, offline=False, **kwargs)`.
- **Config knobs (0.7.0):** `embedder_backend` (`"hashing"|"fastembed"`), `embedder_model`, `embedder_cache_dir`, `embedder_offline`.
- **Dimension:** probed from the model (`emb.dim` == 768 for bge-base); the dim guard enforces it against the store.
- **Offline:** the first construction downloads the model (a one-line log notes the size + cache dir); afterwards no network is used. With `embedder_offline=True` and no warm cache it raises an actionable error.
- **Normalization:** vectors are returned L2-normalized (bge `*-en-v1.5` is already normalized; the adapter normalizes once, idempotently).

### `SentenceTransformerEmbedder`

A local, learned, semantic embedder backed by [`sentence-transformers`](../../src/nexus_memory/core/embeddings.py). It runs on your machine (no external API), so it suits semantic retrieval without sending text off-box — at the cost of a model download and the heavier dependency.

```python
from nexus_memory import NexusMemory, NexusConfig
from nexus_memory.core.embeddings import SentenceTransformerEmbedder

emb = SentenceTransformerEmbedder("all-mpnet-base-v2")  # dim read from the model
memory = NexusMemory(
    db_path="agent.db",
    config=NexusConfig(dim=emb.dim),   # keep dim in sync with the model
    embedder=emb,
)
```

- **Constructor:** `SentenceTransformerEmbedder(model_name="all-mpnet-base-v2", **kwargs)`. Extra `kwargs` are forwarded to the underlying `SentenceTransformer`.
- **Dimension:** read from the loaded model via `get_sentence_embedding_dimension()` and exposed as `emb.dim` — do not hard-code it; pass it into `NexusConfig(dim=emb.dim)`.
- **Normalization:** the model is invoked with `normalize_embeddings=True`, so vectors are already unit length.
- **Install:** `pip install -e ".[sentence-transformers]"` (or `pip install sentence-transformers`). Missing → `ImportError` with that hint.

### `OpenAIEmbedder`

An adapter around the OpenAI embeddings API. It sends text to an **external service**, so read the [PII guidance](#pii-guidance-for-external-apis) below before using it on sensitive data.

```python
from nexus_memory import NexusMemory, NexusConfig
from nexus_memory.core.embeddings import OpenAIEmbedder

emb = OpenAIEmbedder(model="text-embedding-3-small", dim=1536)
cfg = NexusConfig(dim=1536, pii_filter_enabled=True)   # mask before text leaves the machine
memory = NexusMemory(db_path="agent.db", config=cfg, embedder=emb)
```

- **Constructor:** `OpenAIEmbedder(model="text-embedding-3-small", dim=1536, **kwargs)`. Extra `kwargs` are forwarded to the OpenAI `OpenAI(...)` client (API key, base URL, etc.).
- **Dimension:** defaults to `1536`, configurable via the `dim` argument; the same value is passed to the API as the `dimensions` parameter. Keep `config.dim` equal to it.
- **Normalization:** the raw API embedding is L2-normalized by the adapter before it is returned.
- **Install:** `pip install -e ".[openai]"` (or `pip install openai`). Missing → `ImportError` with that hint.

## Switching embedders / re-indexing

Each embedder produces vectors in its *own* space, so switching backend (or model, or dimension) invalidates every stored vector — the existing facts must be **re-embedded**. Nexus refuses to silently mix spaces: it records the embedder *provenance* (backend + model + dim) in the store's `system_config` and errors if you open it with a different embedder than it was written with.

Re-embed an existing store with the bundled tool:

```bash
python -m nexus_memory.reindex --db agent.db --backend fastembed
```

For the recommended **same-dimension** path (the default `HashingEmbedder` and `bge-base-en-v1.5` are both 768), this recomputes every fact's vector and rewrites the provenance **in a single transaction** — no schema change, no data loss. Switching to a *different* dimension is a larger, schema-affecting migration and is out of scope for the bundled same-dim tool.

## PII guidance for external APIs

`HashingEmbedder` and `SentenceTransformerEmbedder` run entirely on-box; `OpenAIEmbedder` transmits the text to be embedded to a third party. When you use an external embedder, enable pre-embedding PII masking so personal data is scrubbed **before** it leaves the machine:

```python
cfg = NexusConfig(dim=1536, pii_filter_enabled=True)
```

`pii_filter_enabled` is `False` by default (opt-in) and is intended for external embedders — it is unnecessary for the local hashing default. See [Privacy and encryption](../use-cases/privacy-and-encryption.md) and the [`NexusConfig` reference](../configuration/nexus-config.md) for the full masking behavior.

## Writing a custom embedder

To plug in any other backend, subclass `Embedder`, set `self.dim`, and implement `encode` to return an L2-normalized list of that length. Override `encode_batch` only if your backend has a faster batch path.

```python
from nexus_memory.core.embeddings import Embedder, _l2_normalize  # or normalize yourself

class MyEmbedder(Embedder):
    def __init__(self, dim: int) -> None:
        self.dim = dim

    def encode(self, text: str) -> list[float]:
        raw = my_model(text)              # -> list[float] of length self.dim
        return _l2_normalize(list(raw))   # must be unit length
```

Then construct the store with a matching dimension:

```python
emb = MyEmbedder(dim=512)
memory = NexusMemory(db_path="agent.db", config=NexusConfig(dim=emb.dim), embedder=emb)
```

See [Extension points](../architecture/extension-points.md) for the other pluggable components.

## Choosing an embedder (summary)

| Embedder | Constructor | `dim` | Dependencies | Best for |
|----------|-------------|-------|--------------|----------|
| `HashingEmbedder` *(default)* | `HashingEmbedder(dim=DEFAULT_DIM)` | `768` (configurable) | none — deterministic, offline | offline use, tests, lexical-overlap retrieval |
| `FastEmbedEmbedder` *(recommended semantic)* | `FastEmbedEmbedder(model_name="BAAI/bge-base-en-v1.5", …)` or `NexusConfig(embedder_backend="fastembed")` | `768` (bge-base; matches the default — no schema change) | `fastembed[cpu]` (lazy; ONNX, no torch) | **local semantic retrieval**, provider-agnostic, offline after a one-time download |
| `SentenceTransformerEmbedder` | `SentenceTransformerEmbedder(model_name="all-mpnet-base-v2", **kwargs)` | from the model (`emb.dim`) | `sentence-transformers` (lazy; `ImportError` if missing) | local semantic retrieval (heavier; needs torch) |
| `OpenAIEmbedder` | `OpenAIEmbedder(model="text-embedding-3-small", dim=1536, **kwargs)` | `1536` (configurable) | `openai` (lazy; `ImportError` if missing) | hosted semantic embeddings (enable PII masking) |

> **Keep `dim` in sync.** `config.dim` is locked at table creation. Decide on your embedder and dimension before the first run; changing dimension later requires a re-embed/migration.

## Related pages

- [Getting started](getting-started.md) — first run with the default embedder.
- [API reference](api-reference.md) — the `NexusMemory` constructor and the `embedder` argument.
- [`NexusConfig`](../configuration/nexus-config.md) — the `dim` and `pii_filter_enabled` fields.
- [Retrieval and scoring](../architecture/retrieval-and-scoring.md) — how the normalized vectors are scored.
- [Extension points](../architecture/extension-points.md) — other pluggable components.
- [Privacy and encryption](../use-cases/privacy-and-encryption.md) — PII masking and encryption at rest.
