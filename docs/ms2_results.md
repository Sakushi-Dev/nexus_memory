# MS2 — Orchestrator, Routing, Cache, Embeddings

Status: complete. Validated by `tests/test_routing.py` (10 tests) and `tests/test_cache.py`
(6 tests).

## Orchestrator (`src/nexus_memory/core/orchestrator.py`)
`NexusMemory` is the single public entry point. Its constructor wires the whole stack from a
`NexusConfig`: `HashingEmbedder` (default), `NexusDB`, `SemanticCache`, `MemoryReader`,
`SpeakerAwareExtractor` (default) + `MemoryWriter`, `PIIFilter`, and `TransparencyInterface`. The
orchestrator's shared `PIIFilter` is injected into the writer so masking honours
`config.pii_filter_enabled` through one code path.

### `process(payload: dict | str) -> dict`
- Accepts a dict or a JSON string (decoded with `json.loads`).
- Validates via `models.parse_request` (pydantic dispatch on `action`).
- Routes on `action`:

| action     | handler                              | response |
|------------|--------------------------------------|----------|
| `assemble` | `MemoryReader.assemble_context`      | `{status, context_xml, raw_facts, meta, latency_ms}` |
| `ingest`   | `MemoryWriter.ingest_async`          | `{status:"processing", task_id, estimated_completion_ms}` |
| `forget`   | `TransparencyInterface.forget`       | `{status, deleted_id}` |
| `inspect`  | `TransparencyInterface.inspect`      | `{status, data}` |
| `optimize` | `MemoryWriter.optimize`              | `{before_bytes, after_bytes, facts}` |

`process()` **never raises to the caller**: invalid JSON, an unknown/invalid action, a
validation failure, or a handler exception all return `{"status": "error", "error": <msg>}`.
Convenience wrappers `inspect()`, `forget()`, `wait()`, `close()` are also exposed.

## Embeddings (`src/nexus_memory/core/embeddings.py`)
- `Embedder` ABC: `encode(text) -> list[float]` (L2-normalized, length `dim`),
  `encode_batch` defaults to mapping `encode`.
- `HashingEmbedder` (default, `dim=768`): a signed feature-hashing vectorizer. Tokens are
  lowercased and split on non-alphanumeric runs; each token is hashed with
  `hashlib.blake2b` (NOT Python `hash()`, so it is deterministic across processes). The
  low bits pick the bucket index `[0, dim)`, a high bit picks the sign, counts are
  accumulated and the vector is L2-normalized. This preserves **lexical overlap**, which is
  what makes the needle-in-a-haystack retrieval work without a model download.
- Optional, lazily-imported adapters that never load at import time:
  `SentenceTransformerEmbedder` and `OpenAIEmbedder`. Each raises a helpful `ImportError`
  if its optional dependency is missing.

## Semantic cache (`src/nexus_memory/core/cache.py`)
`SemanticCache(maxsize=128, threshold=0.95)` is an LRU cache keyed by query embeddings,
thread-safe via an `RLock`. Keys are stored unit-normalized; `get()` computes cosine
similarity (`matrix @ q`, numpy) against all keys and returns the most-similar value when
the best similarity is `>= threshold`, refreshing its LRU recency. `put()` evicts the
least-recently-used entry past `maxsize`. The reader uses it to short-circuit repeat
queries; `clear()` empties it.

## Config (`src/nexus_memory/core/config.py`)
`NexusConfig` dataclass carries every tunable: `dim`, `decay_lambda=0.01`, `min_score=0.6`,
`default_top_k=5`, `redundancy_threshold=0.90`, `semantic_include_assistant=False`,
`cache_size=128`, `cache_threshold=0.95`, `pii_filter_enabled=False` (opt-in; see MS6.2),
`encryption_key=None`, plus the layer knobs (`working_memory_max_turns`, `episodic_*`,
`procedural_*`, `auto_consolidate`). `DEFAULT_DIM = 768`.
