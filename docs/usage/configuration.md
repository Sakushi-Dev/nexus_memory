# Configuration — every setting in one place

This page is the single, scannable reference for **everything you can configure on a `NexusMemory` instance**: the constructor arguments, every `NexusConfig` field, and every `DiaryConfig` field. It is a map, not a deep dive — each section links to the page that explains the *why* and the trade-offs.

All configuration is applied **at construction time**. There is no runtime "reconfigure" call: you build one `NexusMemory`, and it wires the embedder, database, cache, reader/writer, and every layer from the values below.

```python
from nexus_memory import NexusMemory, NexusConfig

memory = NexusMemory(
    db_path="agent.db",                       # where the single SQLite file lives
    config=NexusConfig(min_score=0.5),        # scoring / dedup / cache / layer knobs
    diary=True,                               # opt in to Layer V (off by default)
)
```

## The `NexusMemory` constructor

The constructor takes one positional path plus keyword-only collaborators. Every argument is optional — `NexusMemory()` alone is a fully working, offline, local-first instance.

```python
NexusMemory(
    db_path: str = "nexus_memory.db",
    *,
    config: NexusConfig | None = None,
    embedder: Embedder | None = None,
    extractor: FactExtractor | None = None,
    summarizer: Summarizer | None = None,
    detector: DirectiveDetector | None = None,
    diary: DiaryConfig | bool | None = None,
) -> None
```

| Argument | Type | Default | What it sets |
| :-- | :-- | :-- | :-- |
| `db_path` | `str` | `"nexus_memory.db"` | Path to the **one** SQLite file holding every layer. **Always wins over `config.db_path`** (see [NexusConfig](../configuration/nexus-config.md#db_path-is-overridden-by-the-constructor)). |
| `config` | `NexusConfig \| None` | `None` → defaults | The central tunables object (see [`NexusConfig`](#nexusconfig--the-central-tunables) below). When omitted, a default `NexusConfig` is built and `db_path` is applied to it. |
| `embedder` | `Embedder \| None` | `HashingEmbedder(dim=config.dim)` | The vectorizer. Default is offline, deterministic, dependency-free. Swap in `SentenceTransformer`/`OpenAI` adapters — keep `config.dim` in sync. See [Embedders](embedders.md). |
| `extractor` | `FactExtractor \| None` | `SpeakerAwareExtractor` | Turns interactions into semantic facts. Default attributes each fact to user/assistant and drops the assistant's filler. Pass `MockFactExtractor` for the naive splitter. |
| `summarizer` | `Summarizer \| None` | `MockSummarizer` | Produces the episodic (Layer II) day-summaries. Default is offline and deterministic. |
| `detector` | `DirectiveDetector \| None` | `MockDirectiveDetector` | Mines standing behavioral rules (Layer IV) from interactions. Default is offline and deterministic. |
| `diary` | `DiaryConfig \| bool \| None` | `None` (off) | Opt in to **Layer V** (the hierarchical diary). `diary=True` is shorthand for `DiaryConfig(enabled=True)`; pass a full `DiaryConfig` to tune its knobs (see [`DiaryConfig`](#diaryconfig--the-optional-layer-v) below). |

> The four collaborator defaults (`embedder`, `extractor`, `summarizer`, `detector`) are all **offline and deterministic**, which is what keeps the default path local-first with no network and no model download.

## `NexusConfig` — the central tunables

One dataclass holding scoring, writer, cache, privacy, security, and per-layer switches. Pass it as `config=...`. Full reference with field interactions: **[NexusConfig](../configuration/nexus-config.md)**.

```python
from nexus_memory import NexusConfig
cfg = NexusConfig(min_score=0.5, decay_lambda=0.02, default_top_k=8)
```

### Storage & embedding

| Field | Type | Default | Controls |
| :-- | :-- | :-- | :-- |
| `db_path` | `str` | `"nexus_memory.db"` | SQLite file path — **overridden** by the constructor kwarg. |
| `dim` | `int` | `768` | Embedding dimension. Must match the embedder; **locked at table creation**. |

### Scoring (read path) — see [Retrieval & scoring](../architecture/retrieval-and-scoring.md)

| Field | Type | Default | Controls |
| :-- | :-- | :-- | :-- |
| `decay_lambda` | `float` | `0.01` | Time-decay rate per day: `exp(-decay_lambda · days)`. Higher → older facts fade faster. |
| `min_score` | `float` | `0.6` | Retrieval score floor for `assemble`. Per-request overridable. |
| `default_top_k` | `int` | `5` | Default number of facts retrieved per query. Per-request overridable via `top_k`. |

### Writer (write path)

| Field | Type | Default | Controls |
| :-- | :-- | :-- | :-- |
| `redundancy_threshold` | `float` | `0.90` | Cosine similarity at/above which an incoming fact is a duplicate and skipped. |
| `semantic_include_assistant` | `bool` | `False` | When `False`, only the user's turns become semantic facts. `True` also mines assistant statements. |

### Cache

| Field | Type | Default | Controls |
| :-- | :-- | :-- | :-- |
| `cache_size` | `int` | `128` | Capacity of the semantic LRU query cache. |
| `cache_threshold` | `float` | `0.95` | Cosine similarity at which a new query is a cache hit against a prior one. |

### Privacy & security — see [Privacy & encryption](../use-cases/privacy-and-encryption.md)

| Field | Type | Default | Controls |
| :-- | :-- | :-- | :-- |
| `pii_filter_enabled` | `bool` | `False` | Pre-embedding PII masking. Off for local-first; turn **on** when embedding through an external API. |
| `encryption_key` | `bytes \| None` | `None` | Optional key for an encrypted-at-rest database (separate driver required). |

### Memory layers — see [Memory layers](../architecture/memory-layers.md)

| Field | Type | Default | Controls |
| :-- | :-- | :-- | :-- |
| `working_memory_max_turns` | `int` | `50` | Capacity of the volatile Layer I RAM ring buffer (turns). |
| `episodic_recent_turns` | `int` | `6` | How many recent turns `assemble` injects into `<recent_dialogue>`. |
| `episodic_enabled` | `bool` | `True` | Toggle Layer II (episodic). When off, recent dialogue falls back to working memory. |
| `procedural_max_directives` | `int` | `12` | Cap on active directives injected into context. |
| `procedural_enabled` | `bool` | `True` | Toggle Layer IV procedural directives in context. |
| `auto_consolidate` | `bool` | `True` | When on, `ingest` also logs episodic + runs directive detection. |

> Layer III (semantic) is always on — it has no toggle. Layer V (diary) is configured separately by `DiaryConfig` (below), not here.

## `DiaryConfig` — the optional Layer V

Layer-owned, **off by default**, and not part of `NexusConfig`. Activate with `diary=True` (defaults) or pass a `DiaryConfig` for custom knobs. Full reference: **[DiaryConfig](../configuration/diary-config.md)**.

```python
from nexus_memory import NexusMemory, DiaryConfig
NexusMemory(diary=True)                                   # defaults
NexusMemory(diary=DiaryConfig(enabled=True, update_every=5))  # custom
```

| Field | Type | Default | Symbol | Controls |
| :-- | :-- | :-- | :-- | :-- |
| `enabled` | `bool` | `False` | — | Master switch. When `False`, the layer is never built (no tables, no provider, no routing). |
| `update_every` | `int` | `3` | `N` | Interactions between rolling daily-summary jobs (L1). |
| `section_size` | `int` | `7` | `SECTION_SIZE` | Finalized daily diaries folded into one persistent section (L2). |
| `max_sections` | `int` | `8` | `M` | Ring capacity for persistent sections; oldest overwritten (≈ `M · SECTION_SIZE` = 56 days). |
| `inject_days` | `int` | `1` | `K` | Finalized daily diaries injected into `<memory_context>`. |

## Per-request overrides (not construction-time)

A few `assemble` knobs are passed **per call** and override the `NexusConfig` defaults for that request only:

| Request field | Overrides | Default |
| :-- | :-- | :-- |
| `top_k` | `default_top_k` | `5` |
| `min_score` | `min_score` | `0.6` |

```python
memory.process({"action": "assemble", "query": "...", "top_k": 8, "min_score": 0.4})
```

See [Request & response](../io/request-response.md) for every per-action field.

## Recipes

Each block is self-contained. Imports are shown once here:

```python
from nexus_memory import NexusMemory, NexusConfig, DiaryConfig
```

### Quick starts

```python
# Minimal — fully local, offline, deterministic; default file "nexus_memory.db".
NexusMemory()

# Pick the database location.
NexusMemory(db_path="data/agent.db")

# Diary on, everything else default.
NexusMemory(diary=True)
```

### Scoring & retrieval

```python
# Higher recall — looser floor, return more, fade older facts slower.
NexusMemory(config=NexusConfig(min_score=0.4, default_top_k=10, decay_lambda=0.005))

# Higher precision — strict floor, fewer results, fade older facts faster.
NexusMemory(config=NexusConfig(min_score=0.75, default_top_k=3, decay_lambda=0.03))

# Stricter dedup so near-identical facts are not stored twice.
NexusMemory(config=NexusConfig(redundancy_threshold=0.95))

# Also mine the assistant's turns into semantic memory (default is user-only).
NexusMemory(config=NexusConfig(semantic_include_assistant=True))
```

### Cache

```python
# Bigger query cache, looser hit threshold (more cache reuse).
NexusMemory(config=NexusConfig(cache_size=512, cache_threshold=0.92))
```

### Turning layers on/off

```python
# Lean semantic-only store: skip episodic + procedural + consolidation.
NexusMemory(config=NexusConfig(
    episodic_enabled=False,
    procedural_enabled=False,
    auto_consolidate=False,
))

# Keep more recent dialogue in context, and a larger working-memory buffer.
NexusMemory(config=NexusConfig(episodic_recent_turns=12, working_memory_max_turns=100))

# Allow more standing directives to be injected.
NexusMemory(config=NexusConfig(procedural_max_directives=24))
```

### Diary (Layer V)

```python
# Defaults (N=3, SECTION_SIZE=7, M=8, K=1).
NexusMemory(diary=True)

# Custom cadence + a longer retention ring; inject the last 2 finalized days.
NexusMemory(diary=DiaryConfig(
    enabled=True,
    update_every=5,     # summarize every 5 interactions
    section_size=14,    # 14 days per persistent section
    max_sections=12,    # ring of 12 sections (~168 days)
    inject_days=2,
))
```

### Embedders & privacy

```python
# Local neural embedder (all-mpnet-base-v2 → 768-dim); match config.dim to it.
from nexus_memory.core.embeddings import SentenceTransformerEmbedder
emb = SentenceTransformerEmbedder("all-mpnet-base-v2")
NexusMemory(config=NexusConfig(dim=emb.dim), embedder=emb)

# External API embedder — keep dim in sync AND turn on PII masking.
from nexus_memory.core.embeddings import OpenAIEmbedder
emb = OpenAIEmbedder(model="text-embedding-3-small", dim=1536)
NexusMemory(config=NexusConfig(dim=1536, pii_filter_enabled=True), embedder=emb)
```

### Custom strategies

```python
# Naive splitter instead of the speaker-aware extractor.
from nexus_memory.layers.semantic.extraction import MockFactExtractor
NexusMemory(extractor=MockFactExtractor())
```

### Everything together

```python
from nexus_memory.core.embeddings import OpenAIEmbedder

emb = OpenAIEmbedder(model="text-embedding-3-small", dim=1536)
cfg = NexusConfig(
    dim=1536,
    min_score=0.5,
    default_top_k=8,
    cache_size=256,
    pii_filter_enabled=True,
    episodic_recent_turns=10,
)
memory = NexusMemory(
    db_path="data/agent.db",
    config=cfg,
    embedder=emb,
    diary=DiaryConfig(enabled=True, update_every=5),
)
```

## Related pages

- [NexusConfig](../configuration/nexus-config.md) — full field reference and interactions.
- [DiaryConfig](../configuration/diary-config.md) — Layer V settings in depth.
- [Tuning](../configuration/tuning.md) — choosing scoring, cache, and layer values.
- [Embedders](embedders.md) — the default `HashingEmbedder` and the external adapters.
- [API reference](api-reference.md) — the constructor, every action, and convenience wrappers.
- Source: [`core/config.py`](../../src/nexus_memory/core/config.py), [`layers/diary/config.py`](../../src/nexus_memory/layers/diary/config.py).
