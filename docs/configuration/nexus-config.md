# NexusConfig

`NexusConfig` is the single dataclass that every Nexus Memory component is constructed from. This page is the complete field reference: all 21 fields, their types, defaults, and exactly what each one controls. The dataclass lives in [`core/config.py`](../../src/nexus_memory/core/config.py) and is re-exported from the top-level package as `from nexus_memory import NexusConfig`.

## At a glance

```python
from nexus_memory import NexusMemory, NexusConfig

cfg = NexusConfig(dim=768, min_score=0.5, pii_filter_enabled=False)
memory = NexusMemory(db_path="agent.db", config=cfg)
```

All tunable parameters — scoring, writer dedup, cache, privacy, security, and the per-layer knobs — live on this one object so they can be threaded through the whole stack as a single argument. The orchestrator builds the embedder, the database, the semantic cache, the reader/writer, the PII filter, and every memory layer directly from the values below.

> **`db_path` constructor arg always wins.** The `NexusConfig.db_path` field exists, but the `db_path` keyword on [`NexusMemory(...)`](../usage/api-reference.md#the-nexusmemory-constructor) **overrides** `config.db_path` every time — even when you pass an explicit `config`. Set the path you actually want as the constructor argument; treat the field as a fallback default. See [Field interactions](#field-interactions) below.

## Field reference

All defaults are copied verbatim from the dataclass definition. `DEFAULT_DIM` is `768`.

| Field | Type | Default | Controls |
|-------|------|---------|----------|
| `db_path` | `str` | `"nexus_memory.db"` | SQLite database file path. **Overridden by the `db_path` constructor kwarg** (see below). |
| `dim` | `int` | `768` (`DEFAULT_DIM`) | Embedding vector dimension. Must match the active embedder; fixed at table creation (see [`dim` is locked](#dim-is-locked-at-table-creation)). |
| `decay_lambda` | `float` | `0.01` | Time-decay rate per day in scoring: `exp(-decay_lambda * days_passed)`. Higher → older facts fade faster. |
| `min_score` | `float` | `0.6` | Default retrieval score floor for `assemble`. Facts below this combined score are dropped. Overridable per request. |
| `default_top_k` | `int` | `5` | Default number of facts retrieved per `assemble` query. Overridable per request via `top_k`. |
| `forget_min_similarity` | `float` | `0.6` | Relevance floor for `forget(query=...)`. The nearest KNN match is only deleted when its cosine similarity (`1 - distance`) is `>=` this value; otherwise `forget` returns `{"status": "not_found"}` and deletes nothing. Guards against deleting an unrelated row on a non-empty store. |
| `redundancy_threshold` | `float` | `0.90` | Cosine **similarity** above which an incoming fact is treated as a duplicate of an existing one and skipped by the writer. |
| `semantic_include_assistant` | `bool` | `False` | When `False`, only the **user's** turns become semantic facts; assistant prose still goes to the episodic diary but does not flood the vector store. Set `True` to also mine assistant statements into semantic memory. |
| `cache_size` | `int` | `128` | Capacity of the semantic LRU query cache. Least-recently-used entries are evicted past this size. |
| `cache_threshold` | `float` | `0.95` | Cosine similarity at which a new query is considered a cache hit against a prior cached query. |
| `pii_filter_enabled` | `bool` | `False` | Pre-embedding PII masking. **Off by default** for the local-first path; turn on only when embedding through an **external API** (see below). |
| `encryption_key` | `bytes \| None` | `None` | Optional SQLCipher-style key for an encrypted-at-rest database. Off the core path; requires a separate driver. |
| `working_memory_max_turns` | `int` | `50` | Capacity of the volatile Layer I RAM buffer, in turns. Oldest turns are evicted when full. |
| `episodic_recent_turns` | `int` | `6` | How many recent turns are assembled into the `<recent_dialogue>` block of the context. |
| `episodic_enabled` | `bool` | `True` | Whether Layer II (episodic diary) is active. When `False`, recent dialogue falls back to working memory. |
| `history_truncation` | `str` | `"turns"` | Default truncation mode for the [`NexusMemory.history`](../usage/api-reference.md) accessor: `"turns"` or `"tokens"`. Validated in `__post_init__` (any other value raises `ValueError`). |
| `history_max_turns` | `int` | `20` | Default cap (number of turns) applied by the history accessor in `"turns"` mode. |
| `history_token_budget` | `int` | `2000` | Default token budget applied by the history accessor in `"tokens"` mode. |
| `procedural_max_directives` | `int` | `12` | Cap on the number of active directives injected into context (priority-ordered). |
| `procedural_enabled` | `bool` | `True` | Whether Layer IV procedural directives are added to the assembled context. |
| `auto_consolidate` | `bool` | `True` | When `True`, `ingest` also logs to the episodic layer and runs directive detection (inter-layer transfer). |

## Grouped by concern

The dataclass groups fields by the subsystem they configure. The same grouping is the easiest way to reason about which knob affects what.

### Storage and embedding

- **`db_path`** — the on-disk SQLite file. The vector table, episodic rows, procedural rules, and (optionally) diary tables all live in this single file plus its `-wal`/`-shm` sidecars.
- **`dim`** — the embedding dimension, fixed for the lifetime of the database (see the caveat below).

### Scoring (read path)

`decay_lambda`, `min_score`, and `default_top_k` shape how `assemble` ranks and filters facts. The recency component is `exp(-decay_lambda * days_passed)`; `min_score` is the floor a fact's combined relevance must clear; `default_top_k` bounds how many survive. Per-request `min_score` and `top_k` override the config defaults. See [Retrieval and scoring](../architecture/retrieval-and-scoring.md) for the full formula.

### Transparency / forget

- **`forget_min_similarity`** — the relevance floor for `forget(query=...)`. When you forget by query, the store finds the nearest fact and only deletes it if its cosine similarity (`1 - distance`) is `>= forget_min_similarity`; if the best match falls below the floor, `forget` returns `{"status": "not_found"}` and deletes nothing. This prevents a query that matches nothing in particular from silently deleting the nearest unrelated row. Default `0.6` — moderate enough that genuine paraphrases still match while clearly unrelated queries do not. (Forgetting by explicit `id` is exact and ignores this floor.)

### Writer (write path)

- **`redundancy_threshold`** — the deduplication gate. An incoming fact whose cosine similarity to an existing fact is at or above this value (`>=`) is considered redundant and not stored a second time.
- **`semantic_include_assistant`** — controls who contributes to semantic memory. Left `False`, the default [`SpeakerAwareExtractor`](../../src/nexus_memory/layers/semantic/extraction.py) mines only user turns into facts, keeping conversational filler out of the vector store. The orchestrator wires this field straight into the extractor's `include_assistant` argument.

### Cache

`cache_size` and `cache_threshold` configure the [`SemanticCache`](../../src/nexus_memory/core/cache.py) — an LRU cache keyed by query embeddings that short-circuits repeat or near-repeat queries. A new query whose cosine similarity to a cached query is `>= cache_threshold` reuses the cached result; entries past `cache_size` are evicted least-recently-used first.

### Privacy and security

- **`pii_filter_enabled`** — masks PII *before* text is embedded. It is **off by default** on purpose: on the local-first path nothing leaves the machine, so masking would only destroy useful memory (for example, the user's own name). Turn it on when you swap in an external embedder such as `OpenAIEmbedder`. See [Privacy and encryption](../use-cases/privacy-and-encryption.md).
- **`encryption_key`** — an optional key for an encrypted database. This sits off the core path and requires a separate (e.g. SQLCipher) driver; `None` means the standard, unencrypted SQLite path.

### Memory layers

These mirror the four-layer architecture (see [Memory layers](../architecture/memory-layers.md)):

- **Layer I — working memory:** `working_memory_max_turns` caps the volatile RAM buffer.
- **Layer II — episodic:** `episodic_enabled` toggles the durable dialogue layer; `episodic_recent_turns` sets how much recent dialogue is injected into context.
- **History accessor:** `history_truncation` (`"turns"` | `"tokens"`), `history_max_turns`, and `history_token_budget` set the defaults for the [`NexusMemory.history`](../usage/api-reference.md) accessor — the read-back of recent chat history. They size that accessor, not the `<recent_dialogue>` block (which is governed by `episodic_recent_turns`).
- **Layer IV — procedural:** `procedural_enabled` toggles standing behavioral rules in context; `procedural_max_directives` caps how many are injected.
- **Consolidation:** `auto_consolidate` decides whether `ingest` performs the inter-layer transfer work (episodic logging plus rule detection) in addition to the semantic write.

> Layer III (semantic) has no on/off switch — it is the always-on vector store. Layer V (the hierarchical diary) is configured separately by [`DiaryConfig`](./diary-config.md), not by `NexusConfig`.

## Beyond NexusConfig — the rest of the configuration surface

`NexusConfig` holds the *tunable values*, but it is **not** everything you can configure on a `NexusMemory`. Three things live outside this dataclass on purpose. For a single map of all of them, see [Configuration (usage)](../usage/configuration.md).

### Constructor collaborators (pluggable strategies)

These are passed directly to the [`NexusMemory` constructor](../usage/api-reference.md#the-nexusmemory-constructor), not via `NexusConfig`, because they are *behaviors* (objects), not scalars. Each defaults to an offline, deterministic implementation, so the default path needs no network and no model download.

| Constructor arg | Type | Default | Configures |
| :-- | :-- | :-- | :-- |
| `embedder` | `Embedder` | `HashingEmbedder(dim=config.dim)` | How text becomes vectors. Swap in the `SentenceTransformer`/`OpenAI` adapters — keep `config.dim` in sync. See [Embedders](../usage/embedders.md). |
| `extractor` | `FactExtractor` | `SpeakerAwareExtractor` | How interactions become semantic facts (wired to `semantic_include_assistant`). |
| `summarizer` | `Summarizer` | `MockSummarizer` | How the episodic (Layer II) day-summaries are produced. |
| `detector` | `DirectiveDetector` | `MockDirectiveDetector` | How standing behavioral rules (Layer IV) are mined from interactions. |

### Layer V (the diary) is configured separately

The optional hierarchical diary is **off by default** and is *not* part of `NexusConfig`. Opt in at construction with `diary=True` (defaults) or a `DiaryConfig` for custom knobs:

```python
NexusMemory(diary=True)                                   # defaults
NexusMemory(diary=DiaryConfig(enabled=True, update_every=3))  # custom (pin pre-0.3.5 cadence)
```

Its knobs (`update_every`, `diary_window`, `max_sentences`, `sessions_per_summary`, `inject_sessions`, `summary_max_sentences`) live in [`DiaryConfig`](./diary-config.md), not here.

### Per-request overrides

Two scoring values are overridable **per `assemble` call**, taking precedence over the `NexusConfig` defaults for that request only:

| Request field | Overrides | Default |
| :-- | :-- | :-- |
| `top_k` | `default_top_k` | `5` |
| `min_score` | `min_score` | `0.6` |

```python
memory.process({"action": "assemble", "query": "...", "top_k": 8, "min_score": 0.4})
```

See [Request & response](../io/request-response.md) for every per-action field.

## Field interactions

### `db_path` is overridden by the constructor

The `db_path` keyword on the `NexusMemory` constructor takes precedence over `config.db_path` unconditionally:

```python
cfg = NexusConfig(db_path="ignored.db")
memory = NexusMemory(db_path="agent.db", config=cfg)
# → the database is agent.db, NOT ignored.db
```

When you omit `config` entirely, a default `NexusConfig` is created and the constructor's `db_path` is applied to it. Either way, the positional/keyword `db_path` is the one that takes effect. See the [constructor reference](../usage/api-reference.md#the-nexusmemory-constructor).

### `dim` is locked at table creation

The vector table's dimension is fixed when the schema is first applied. Changing `dim` afterward against an existing database requires a full re-embed/migration — it cannot be altered in place. Keep `config.dim` and the embedder's dimension in sync **from the very first run**:

```python
from nexus_memory import NexusMemory, NexusConfig
from nexus_memory.core.embeddings import OpenAIEmbedder

emb = OpenAIEmbedder(model="text-embedding-3-small", dim=1536)
cfg = NexusConfig(dim=1536, pii_filter_enabled=True)   # dim matches the embedder
memory = NexusMemory(db_path="agent.db", config=cfg, embedder=emb)
```

### `dim` and the embedder must agree

Every embedder returns an L2-normalized vector of length equal to its own dimension. If `config.dim` disagrees with the embedder, vectors will not fit the table. The default [`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py) uses `DEFAULT_DIM` (`768`); when omitting the `embedder` argument the orchestrator constructs `HashingEmbedder(dim=config.dim)`, so they stay aligned automatically. See [Choosing an embedder](../usage/embedders.md).

### `pii_filter_enabled` pairs with external embedders

Because the orchestrator injects a single shared `PIIFilter` into the writer, enabling `pii_filter_enabled` masks sensitive text on the one write path before it is embedded. Enable it precisely when text would otherwise leave the machine (an external embedding API); leave it off for the fully local `HashingEmbedder`.

## Example: a fully customized config

```python
from nexus_memory import NexusMemory, NexusConfig

cfg = NexusConfig(
    dim=768,
    decay_lambda=0.02,            # fade older facts a bit faster
    min_score=0.5,                # looser retrieval floor
    default_top_k=8,
    redundancy_threshold=0.92,    # stricter dedup
    semantic_include_assistant=True,
    cache_size=256,
    cache_threshold=0.97,
    working_memory_max_turns=80,
    episodic_recent_turns=10,
    procedural_max_directives=20,
    auto_consolidate=True,
)
memory = NexusMemory(db_path="agent.db", config=cfg)
```

## Related pages

- [Configuration (usage)](../usage/configuration.md) — one-stop map of every setting: constructor args, all `NexusConfig` fields, and `DiaryConfig`.
- [DiaryConfig](./diary-config.md) — the separate, opt-in Layer V configuration.
- [Tuning](./tuning.md) — guidance on choosing scoring, cache, and layer values.
- [Retrieval and scoring](../architecture/retrieval-and-scoring.md) — how `decay_lambda`, `min_score`, and `default_top_k` combine.
- [Memory layers](../architecture/memory-layers.md) — the layers the `episodic_*`, `procedural_*`, `working_memory_*`, and `auto_consolidate` fields configure.
- [API reference](../usage/api-reference.md) — the `NexusMemory` constructor and `process()` actions.
- Source: [`core/config.py`](../../src/nexus_memory/core/config.py).
