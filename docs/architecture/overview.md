# Architecture Overview

This page is the system at a glance: the five cognitive memory layers, the core design principles that constrain every component, and how the pieces fit together around a single entry point. Each section links to the deeper architecture pages for the full detail.

Nexus Memory turns a stream of `(query, response)` interactions into a layered, queryable memory and renders a single prompt-ready `<memory_context>` XML block on demand. The entire system is backed by **one SQLite file** and runs with no network and no heavyweight ML dependencies by default.

## The single entry point

The public surface is intentionally tiny. You construct one object and call one method:

```python
from nexus_memory import NexusMemory

nexus = NexusMemory("nexus_memory.db")

nexus.process({"action": "ingest",
               "interaction": {"query": "I prefer answers in German.",
                               "response": "Got it."}})
nexus.wait()  # async ingest — flush before reading back

result = nexus.process({"action": "assemble", "query": "What language?"})
print(result["context_xml"])   # <memory_context>...</memory_context>
nexus.close()
```

[`NexusMemory.process(payload)`](../../src/nexus_memory/core/orchestrator.py) accepts a dict or JSON string, validates it via Pydantic, routes on the `action` field, and **never raises** to the caller — every error comes back as `{"status": "error", "error": <message>}`. Everything else in the library is internal wiring behind this method. The full action table and response shapes live in the [API Reference](../usage/api-reference.md); the request/response envelope is detailed in [Request & Response](../io/request-response.md).

## The five cognitive layers

Nexus models memory after a coarse cognitive hierarchy. Each layer answers a different question and owns a different slice of persistence.

| Layer | Name | Question it answers | Volatility | Owning module |
|-------|------|---------------------|------------|---------------|
| **I** | Working memory | "What was just said?" | RAM only (ring buffer) | [working.py](../../src/nexus_memory/layers/working/working.py) |
| **II** | Episodic | "What happened, verbatim, and when?" | SQLite | [episodic.py](../../src/nexus_memory/layers/episodic/episodic.py) |
| **III** | Semantic | "What facts do I know?" | SQLite + vectors | [writer.py](../../src/nexus_memory/layers/semantic/writer.py), [reader.py](../../src/nexus_memory/layers/semantic/reader.py) |
| **IV** | Procedural | "How should I behave?" | SQLite | [procedural.py](../../src/nexus_memory/layers/procedural/procedural.py) |
| **V** | Diary *(optional)* | "What is the long-arc narrative?" | SQLite (only when enabled) | [layers/diary/](../../src/nexus_memory/layers/diary/) |

- **Layer I — Working memory** is a thread-safe bounded ring buffer (`deque(maxlen=working_memory_max_turns)`, default **50**) of the most recent turns. It is updated **synchronously** on ingest and is the fallback source of recent dialogue when the episodic layer is off. Nothing here is persisted.
- **Layer II — Episodic** persists the raw dialogue transcript (`episodic_turns`) plus optional day-summaries (`episodic_summaries`). A pluggable `Summarizer` (default `MockSummarizer`, offline) turns a day's turns into a short narrative.
- **Layer III — Semantic** is the vector store and the only layer that does similarity search. A `FactExtractor` turns an interaction into scored atomic facts; each is embedded, deduplicated, and written to the `agent_memory` vec0 table. The read path embeds the query, over-retrieves via KNN, re-ranks, and renders `<fact .../>` XML.
- **Layer IV — Procedural** holds *directives* — standing behavioral rules ("Respond in German.", "Be concise."). Rules are upserted (UNIQUE on `directive`), priority-ranked (1–10), and activatable.
- **Layer V — Diary** *(off by default)* builds a hierarchical long-arc narrative: rolling per-session summaries fold into a single growing persistent summary. Crucially, it **never calls an LLM itself** — it enqueues summarization jobs into an outbox that the host drains and answers.

For the full per-layer schema, methods, and defaults, see [Memory Layers](memory-layers.md); the diary has its own page at [Diary Layer](diary-layer.md).

## How the pieces fit

Every request flows through `NexusMemory.process()`, which validates and routes on `action`. An **ingest** dispatches a synchronous Layer-I write plus an asynchronous fan-out to the durable layers; an **assemble** composes every layer into one document.

```
                          ┌─────────────────────────────────────────────┐
                          │            NexusMemory.process()             │
                          │     (validate → route → never raises)        │
                          └───────────────────────┬─────────────────────┘
                                                  │
        ingest ───────────────┬──────────────────┼───────────────────────── assemble
                              │                   │                               │
                  ┌───────────▼──────────┐        │            ┌──────────────────▼────────────────┐
                  │  I. Working Memory    │        │            │        ContextAssembler            │
                  │  (sync, RAM ring)     │        │            │  nests layer sections into one     │
                  └───────────┬──────────┘        │            │       <memory_context>             │
                              │ (async writer)    │            └───┬───────┬───────┬───────────┬────┘
                  ┌───────────▼──────────┐        │                │       │       │           │
                  │ III. Semantic Writer  │        │            procedural semantic recent   provider
                  │  extract→dedup→embed  │        │             (IV)     (III)  dialogue   (diary V)
                  └───────────┬──────────┘        │                       reader (II/I)
                              │ consolidators      │
              ┌───────────────┼───────────────────┼──────────────┐
              ▼               ▼                    ▼              ▼
      II. Episodic      IV. Procedural      V. Diary           (writes)
      log turns         detect rules        scheduler →
                                            outbox jobs
```

- **Write path.** `ingest` updates Layer I inline, then `MemoryWriter.ingest_async` returns a `task_id` immediately and runs the rest on a background thread: extract facts → embed → dedup → write to `agent_memory`, then run consolidators in a fixed order (`EpisodicConsolidator` → `ProceduralConsolidator` → `DiaryConsolidator` if enabled). Because writes are async, freshly ingested facts are **not** visible to an `assemble` issued immediately after — call `wait()` (or `close()`, which waits internally) first.
- **Read path.** `assemble` is coordinated by [`ContextAssembler`](../../src/nexus_memory/core/context.py), which delegates the semantic block to `MemoryReader` and composes it with procedural directives, recent dialogue, and any context providers into one `<memory_context>`.

The two ways the system extends — write-side **consolidators** and read-side **context providers** — are exactly how Layer V attaches without the core knowing the diary exists. Both seams are documented in [Extension Points](extension-points.md). The end-to-end byte-level flow is in [Data Flow](../io/data-flow.md), and the scoring math behind the read path is in [Retrieval & Scoring](retrieval-and-scoring.md).

## Core design principles

| Principle | What it means in practice |
|-----------|---------------------------|
| **Local-first** | One SQLite file at `config.db_path` (default `nexus_memory.db`), backed by the `sqlite-vec` extension for vector search. No external service is required. |
| **Offline & deterministic** | The default embedder ([`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py)), fact extractor (`SpeakerAwareExtractor`), summarizer (`MockSummarizer`), and directive detector (`MockDirectiveDetector`) are all dependency-free, offline, and deterministic — no network, no model download. |
| **Single entry point** | All communication funnels through `process(payload)`, which validates, routes, and never raises. Convenience wrappers (`wait`, `close`, `inspect`, `forget`, `remember_rule`, `distill`, …) sit on top of the same machinery. |
| **Provider-agnostic** | The optional diary never imports an LLM; it hands summarization jobs to the host through an outbox table, so any provider — local, remote, or hybrid — can drive it. |
| **One `.db` file** | All five layers share a single SQLite connection (`check_same_thread=False`) with every write serialized through one re-entrant lock, [`NexusDB.lock`](../../src/nexus_memory/core/db.py). Reads run lock-free under WAL. |

All tunables live in one [`NexusConfig`](../../src/nexus_memory/core/config.py) dataclass threaded through the whole stack — scoring, dedup, cache, privacy/security, and per-layer switches. The constructor lets you swap any default component:

```python
NexusMemory(
    db_path="nexus_memory.db", *,
    config=None,         # NexusConfig; db_path arg always overrides config.db_path
    embedder=None,       # default: HashingEmbedder(dim=config.dim) — offline, deterministic
    extractor=None,      # default: SpeakerAwareExtractor (filters assistant filler)
    summarizer=None,     # default: MockSummarizer (offline)
    detector=None,       # default: MockDirectiveDetector (DE+EN, offline)
    diary=None,          # DiaryConfig; None/disabled ⇒ Layer V not built
)
```

See [Nexus Configuration](../configuration/nexus-config.md) for every field and default, [Embedders](../usage/embedders.md) for swapping the vectorizer, and [Privacy & Encryption](../use-cases/privacy-and-encryption.md) for the opt-in PII and SQLCipher subsystems.

## Dependencies

The default path is intentionally lean — three small runtime dependencies, no ML stack, no network:

| Dependency | Role |
|------------|------|
| [`sqlite-vec`](https://github.com/asg017/sqlite-vec) `>=0.1.9` | The `vec0` virtual table and indexed cosine-KNN behind semantic search; loaded as a SQLite extension at connect time (see [Persistence](persistence.md)). |
| [`pydantic`](https://docs.pydantic.dev) `>=2.12` | Strict v2 models that validate every `process()` payload before it reaches a layer (see [Request / Response](../io/request-response.md)). |
| [`numpy`](https://numpy.org) `>=2.0` | Vector math on the read path — cosine similarity in the semantic cache and the scoring functions (see [Retrieval & Scoring](retrieval-and-scoring.md)). |

Everything else is the Python standard library (`sqlite3`, `threading`, `hashlib`, `xml.sax.saxutils`, …). The default embedder, extractor, summarizer, and directive detector pull in **nothing extra** — that is what makes the default path fully offline and deterministic.

Heavier or networked embedders are **optional extras**, lazily imported so they are never touched unless you explicitly construct one: `sentence-transformers` (local transformer embeddings) and `openai` (hosted embeddings). See [Embedders](../usage/embedders.md) for the install extras and trade-offs.

## Where to go next

- [Memory Layers](memory-layers.md) — per-layer schema, methods, and persistence detail.
- [Diary Layer](diary-layer.md) — the optional Layer V and its outbox state machine.
- [Retrieval & Scoring](retrieval-and-scoring.md) — `score = similarity × importance × decay`, KNN over-retrieval, re-ranking, and the semantic cache.
- [Persistence](persistence.md) — the single-file schema, the write lock, and vec0 gotchas.
- [Extension Points](extension-points.md) — the consolidator and context-provider seams.
- [Getting Started](../usage/getting-started.md) and the [API Reference](../usage/api-reference.md) — the practical integration path.
- [examples/basic_usage.py](../../examples/basic_usage.py) — a minimal offline ingest → wait → assemble loop.
