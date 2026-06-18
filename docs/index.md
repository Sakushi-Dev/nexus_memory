# Nexus Memory — Documentation

A **local-first**, dependency-light agent-memory library for Python. Nexus Memory turns a stream of `(query, response)` interactions into a layered, self-managing long-term memory and renders a single prompt-ready `<memory_context>` XML block on demand — all backed by **one SQLite file** (with the [`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension for vector search), with **no server, no network, and no model download** on the default path.

This page is the entry point to the documentation: it states what the library is, gives the five-layer mental model in one line each, and links every page in the docs tree.

## What it is

The public surface is intentionally tiny. You construct one object and call one method:

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="my_agent.db")

memory.process({
    "action": "ingest",
    "interaction": {
        "query": "where do I keep my keys?",
        "response": "You always keep your house keys in the blue ceramic bowl.",
    },
})
memory.wait()  # ingest is async; wait() makes a script deterministic

result = memory.process({"action": "assemble", "query": "where are my keys?", "top_k": 3})
print(result["context_xml"])  # prompt-ready <memory_context> XML

memory.close()
```

Every request is a dict (or JSON string) routed through [`NexusMemory.process()`](../src/nexus_memory/core/orchestrator.py), which validates the payload with Pydantic, routes on `action`, and **never raises to the caller** — every error comes back as `{"status": "error", "error": ...}`. The default [`HashingEmbedder`](../src/nexus_memory/core/embeddings.py) (768-dim, blake2b feature hashing, L2-normalized) is offline and deterministic, so nothing leaves the machine and vectors are reproducible.

A runnable version lives in [`examples/basic_usage.py`](../examples/basic_usage.py); the diary handoff loop is in [`examples/diary_outbox.py`](../examples/diary_outbox.py).

## The five-layer mental model

A single `ingest` consolidates across layers, and one `assemble` returns a unified, layer-aware `<memory_context>`.

| Layer | Name | Question it answers | Volatility | One-line summary |
|-------|------|---------------------|------------|------------------|
| **I** | Working | "What was just said?" | RAM only (ring buffer) | A bounded ring buffer of the last *N* turns (default **50**) for fast recency context, updated synchronously on ingest. |
| **II** | Episodic | "What happened, verbatim, and when?" | SQLite | Persistent raw dialogue transcript plus deterministic narrative day-summaries. |
| **III** | Semantic | "What facts do I know?" | SQLite + vectors | Decontextualized fact vectors retrieved by cosine KNN, graph-expanded, then re-ranked by `similarity × importance × exp(-λ · days)`. |
| **IV** | Procedural | "How should I behave?" | SQLite | Standing behavioral directives (e.g. "Respond in German.") detected automatically and injected into the assembled context. |
| **V** | Diary *(optional, off by default)* | "What is the long-arc narrative?" | SQLite (only when enabled) | A bounded time-pyramid of model-written summaries, driven through a handoff outbox — the library never calls an LLM itself. |

Layers I–IV fan out from a single `ingest`; the diary (Layer V) is opt-in via `NexusMemory(diary=True)`. The deep design of each layer is covered under [Architecture](#architecture).

## Documentation map

### Architecture

How the system is shaped — the layers, the read/write flows, scoring, persistence, and the extension seams.

| Page | Covers |
|------|--------|
| [Overview](architecture/overview.md) | The five-layer model, the `ingest` and `assemble` flows, and the one-entry-point design. |
| [Memory layers](architecture/memory-layers.md) | Working, episodic, semantic, and procedural layers in detail. |
| [Diary layer](architecture/diary-layer.md) | The optional hierarchical diary (Layer V) and its outbox handoff. |
| [Retrieval & scoring](architecture/retrieval-and-scoring.md) | KNN over-retrieval, 1-hop graph expansion, the `similarity × importance × decay` re-ranker, and the semantic cache. |
| [Persistence](architecture/persistence.md) | The single SQLite + `sqlite-vec` file, the schema, the shared connection, and the write lock. |
| [Extension points](architecture/extension-points.md) | The two seams: writer consolidators (write-side) and context providers (read-side). |

### I/O

The request/response contract and what flows through `process()`.

| Page | Covers |
|------|--------|
| [Request & response](io/request-response.md) | Per-action payload schemas and response shapes. |
| [Data flow](io/data-flow.md) | End-to-end byte-level path from request through handler to store and back. |

### Usage

Getting productive with the library.

| Page | Covers |
|------|--------|
| [Getting started](usage/getting-started.md) | Install, construct, ingest, wait, assemble. |
| [API reference](usage/api-reference.md) | Every action, request schema, response shape, and convenience wrapper. |
| [Configuration](usage/configuration.md) | One-stop map of every setting: constructor args, all `NexusConfig` fields, and `DiaryConfig`. |
| [Embedders](usage/embedders.md) | The default `HashingEmbedder` plus the `SentenceTransformer` and `OpenAI` adapters. |
| [Transparency](usage/transparency.md) | `inspect`, `forget`, `pin`, and `update` your own memories. |

### Use cases

End-to-end scenarios drawn from the layer model.

| Page | Covers |
|------|--------|
| [Agent memory](use-cases/agent-memory.md) | Wiring Nexus into an LLM agent's prompt loop. |
| [Behavioral rules](use-cases/behavioral-rules.md) | Standing directives via the procedural layer and `distill()`. |
| [Hierarchical diary](use-cases/hierarchical-diary.md) | Driving the diary outbox with any summarization model. |
| [Privacy & encryption](use-cases/privacy-and-encryption.md) | The opt-in PII filter and the optional SQLCipher hook. |

### Configuration

Every tunable, in one place.

| Page | Covers |
|------|--------|
| [NexusConfig](configuration/nexus-config.md) | The single config dataclass — scoring, dedup, cache, privacy/security, and per-layer switches. |
| [DiaryConfig](configuration/diary-config.md) | Layer V settings: `update_every`, `section_size`, `max_sections`, `inject_days`. |
| [Tuning](configuration/tuning.md) | Practical guidance on scoring (`min_score`, `decay_lambda`, `default_top_k`) and dedup (`redundancy_threshold`). |

### Changelog

Release notes, newest first.

| Page | Covers |
|------|--------|
| [Changelog](changelog/index.md) | Per-version release notes; the latest is [0.3.2](changelog/0.3.2.md). |

## Actions at a glance

Every payload carries an `action`, passed to `memory.process(...)`. Full schemas live in [Request & response](io/request-response.md) and the [API reference](usage/api-reference.md).

| action | key fields | returns |
|---|---|---|
| `assemble` | `query`, `top_k=5`, `min_score=0.6`, `filters?` | `{status, context_xml, raw_facts, directives, recent_dialogue, meta, latency_ms}` |
| `ingest` | `interaction:{query, response}`, `metadata?`, `priority?` | `{status:"processing", task_id, estimated_completion_ms}` |
| `forget` | exactly one of `fact_id` / `query` | `{status, deleted_id}` |
| `inspect` | `type:"health"\|"episodic"\|"semantic"\|"working"\|"procedural"`, `filter?` | `{status, data}` |
| `optimize` | — | `{before_bytes, after_bytes, facts}` |
| `diary` | `day?`, `time_range?`, `store?` | `{status, period, summary, turn_count}` |
| `rule` | `op:"add"\|"list"\|"deactivate"`, `directive?`, `priority?`, `rule_id?` | add: `{status, rule}` · list: `{status, rules}` · deactivate: `{status, rule_id, deactivated}` |
| `distill` | — | `{status, promoted:[rule,...]}` |
| `pending_summaries` | `limit?` *(Layer V only)* | `{status, jobs:[...]}` |
| `submit_summary` | `job_id`, `summary` *(Layer V only)* | `{status, applied?:"daily"\|"section"}` |

## Install

Clone the repo and install it with `pip` (editable, so source edits take effect immediately). Requires Python ≥ 3.11.

```sh
git clone https://github.com/Sakushi-Dev/nexus_memory.git
cd nexus_memory
pip install -e .
```

This reads [`pyproject.toml`](../pyproject.toml), pulls in the dependencies (`sqlite-vec`, `pydantic`, `numpy`), and registers `nexus_memory` so `import nexus_memory` works from anywhere with that interpreter. The optional embedder backends are extras: `pip install -e ".[sentence-transformers]"` or `pip install -e ".[openai]"`. See [Getting started](usage/getting-started.md) for the full setup, and [NexusConfig](configuration/nexus-config.md) to tune every default.

## License

MIT — see [LICENSE](../LICENSE).
