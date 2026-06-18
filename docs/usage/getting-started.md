# Getting Started

This page takes you from a clean checkout to a working, fully offline memory loop: set up Nexus Memory, construct a [`NexusMemory`](../../src/nexus_memory/core/orchestrator.py), ingest one interaction, assemble a prompt-ready context, inspect the store, and close cleanly. Everything here runs on the default, dependency-free [`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py) — no network, no model download, no server.

## Requirements

- **Python ≥ 3.11.**
- Three runtime dependencies, declared in [`pyproject.toml`](../../pyproject.toml): `sqlite-vec>=0.1.9`, `pydantic>=2.12`, and `numpy>=2.0`. `pip` installs them automatically. No external services and no LLM are required on the default path.

## Install

Clone the repository and install it with `pip`. The editable flag (`-e`) means edits to `src/nexus_memory/` take effect immediately, without reinstalling:

```sh
git clone https://github.com/Sakushi-Dev/nexus_memory.git
cd nexus_memory
pip install -e .
```

This reads [`pyproject.toml`](../../pyproject.toml), pulls in the runtime dependencies, and registers the `nexus_memory` package so `import nexus_memory` works from anywhere with that interpreter.

> **Multiple Pythons?** `pip` installs into the interpreter it belongs to. To target a specific one (e.g. the one your editor runs), call pip through it: `path/to/python.exe -m pip install -e .`

### Install into a virtual environment (recommended)

A virtual environment keeps Nexus and its dependencies isolated from your system Python. Create one inside the clone, activate it, then install — `pip` now resolves to the venv automatically:

```sh
git clone https://github.com/Sakushi-Dev/nexus_memory.git
cd nexus_memory

# Create the venv.
python -m venv .venv

# Activate it…
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\Activate.ps1       # Windows (PowerShell)

# …then install into it.
pip install -e .
```

While the venv is active, `python` and `pip` point at it, so `python examples/basic_usage.py` and `python -m pytest -q` just work. Leave it again with `deactivate`.

### Optional embedder backends

The defaults need none of these. The optional backends are declared as extras in [`pyproject.toml`](../../pyproject.toml) — install the one you want:

| Backend | Install | Enables |
|---------|---------|---------|
| `sentence-transformers>=2.2` | `pip install -e ".[sentence-transformers]"` | [`SentenceTransformerEmbedder`](embedders.md) (local transformer embeddings) |
| `openai>=1.0` | `pip install -e ".[openai]"` | [`OpenAIEmbedder`](embedders.md) (hosted embeddings) |

Both embedder adapters are **lazy-imported** — the library never touches `sentence-transformers` or `openai` unless you explicitly construct one of those embedders. See [Embedders](embedders.md) for how to wire one up (and keep `config.dim` in sync).

### Updating

Because the install is editable, `git pull` is usually all you need. Re-run `pip install -e .` only if the dependencies changed (e.g. after a release bumps requirements in [`pyproject.toml`](../../pyproject.toml)).

### Verify it imports

```sh
python -c "from nexus_memory import NexusMemory; print('ok')"
```

To run the test suite from a clone, install the dev extra (`pip install -e ".[dev]"`) and run `python -m pytest -q` from the repository root.

## Quickstart: an end-to-end loop

The snippet below mirrors [`examples/basic_usage.py`](../../examples/basic_usage.py). It writes to a throwaway database, ingests one user/assistant exchange, assembles a `<memory_context>` for a related query, inspects store health, and closes. Run it as-is with `python examples/basic_usage.py`.

```python
import tempfile
from pathlib import Path

from nexus_memory import NexusMemory

db_path = str(Path(tempfile.mkdtemp()) / "demo.db")
memory = NexusMemory(db_path=db_path)
try:
    # 1. Ingest an interaction (async; wait() for determinism in a script).
    memory.process({
        "action": "ingest",
        "interaction": {
            "query": "where do I keep my keys?",
            "response": (
                "You always keep your house keys in the blue ceramic "
                "bowl on the kitchen counter."
            ),
        },
    })
    memory.wait()

    # 2. Assemble a prompt-ready memory context for a related query.
    result = memory.process({
        "action": "assemble",
        "query": "where are my house keys?",
        "top_k": 3,
        "min_score": 0.0,
    })
    print("status:", result["status"])
    print(result["context_xml"])
    print("latency_ms:", round(result["latency_ms"], 3))

    # 3. Inspect store health.
    health = memory.inspect(type="health")
    print("health:", health["data"][0])

    # 4. Forget by free-text query.
    forgotten = memory.forget(query="house keys")
    print("forgot:", forgotten)
finally:
    memory.close()
```

The rest of this section walks through each step.

### 1. Construct `NexusMemory`

```python
memory = NexusMemory(db_path=db_path)
```

`db_path` is the only argument you need. It is the path to the SQLite file and **always overrides** `config.db_path` even when an explicit `config` is supplied (the default file name is `"nexus_memory.db"`). When no `embedder` is passed, the constructor builds a [`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py) sized to `config.dim` (768 by default) — deterministic, dependency-free, and offline. Construction opens the SQLite connection (loading `sqlite-vec` and applying the schema), builds the working/episodic/procedural layers, the async writer, the reader, the context assembler, and a per-instance `session_id` (UUID4) that tags every episodic turn written during this run.

The full constructor signature and every keyword (`config`, `embedder`, `extractor`, `summarizer`, `detector`, `diary`) is documented in the [API Reference](api-reference.md#the-nexusmemory-constructor).

### 2. Ingest an interaction

```python
memory.process({
    "action": "ingest",
    "interaction": {"query": "...", "response": "..."},
})
```

Every request is a plain dict (or JSON string) routed through `process()`, which validates with pydantic and **never raises** — failures come back as `{"status": "error", "error": ...}`. An `ingest` updates the volatile working buffer (Layer I) **synchronously**, then dispatches the durable semantic/episodic/procedural writes on a **background thread**, returning immediately:

```python
{"status": "processing", "task_id": "<uuid4>", "estimated_completion_ms": 50}
```

By default only the *user's* statements (`query`) become semantic fact vectors; the assistant's `response` is logged to the episodic layer but is not embedded as a fact (controlled by `config.semantic_include_assistant`, default `False`).

### 3. `wait()` for the async writes

```python
memory.wait()
```

Because ingestion is asynchronous, an `assemble` or `inspect` issued immediately after `ingest` will **not** see the new facts. In a script, call `wait()` to block until all outstanding writes finish, which makes the run deterministic. (`close()` also waits internally, so a `wait()` right before `close()` is redundant but harmless.)

### 4. Assemble a context

```python
result = memory.process({
    "action": "assemble",
    "query": "where are my house keys?",
    "top_k": 3,
    "min_score": 0.0,
})
print(result["context_xml"])
```

`assemble` returns a unified, layer-aware `<memory_context>` ready to drop into an LLM prompt. The defaults are `top_k=5` and `min_score=0.6`; the quickstart lowers `min_score` to `0.0` so the single just-ingested fact is returned regardless of its similarity score. The response is a dict:

| Key | Meaning |
|-----|---------|
| `status` | `"success"` (always branch on this first) |
| `context_xml` | prompt-ready `<memory_context>...</memory_context>` block — send this to your LLM |
| `raw_facts` | `[{id, content, score, timestamp}, ...]` — the semantic facts, for introspection |
| `directives` | active Layer IV behavioral rules, priority-descending |
| `recent_dialogue` | `[{role, content, timestamp}, ...]` recent turns |
| `meta` | counts and `tokens_estimated` |
| `latency_ms` | assembly latency |

For the full `<memory_context>` structure and scoring (`similarity × importance × exp(-λ · days)`), see [Retrieval and Scoring](../architecture/retrieval-and-scoring.md) and the [`assemble` action reference](api-reference.md#action-assemble).

### 5. Inspect the store

```python
health = memory.inspect(type="health")
print("health:", health["data"][0])
```

`inspect` is a convenience wrapper around the transparency interface. `inspect(type="health")` returns `{"status": "success", "data": [...]}` whose single element is:

```python
{"count": int, "db_path": str, "db_size_bytes": int, "dim": int}
```

`db_size_bytes` includes the `-wal` and `-shm` sidecar files. Other `type` values (`"episodic"`, `"semantic"`, `"working"`, `"procedural"`) expose each layer's contents — see [Transparency](transparency.md).

### 6. Close

```python
memory.close()
```

`close()` flushes the background writer, finalizes the diary (if enabled), and closes the database connection. Always call it from a `try/finally` so the SQLite file is released even on error.

## Important behaviors to internalize

- **`process()` never raises.** Invalid JSON, an unknown action, a validation failure, or a handler error all return `{"status": "error", "error": "<message>"}`. Always check `result["status"]` before reading other keys.
- **Ingest is asynchronous.** Call `wait()` before any read that must observe newly ingested facts, and always `close()` in a `try/finally`.
- **`db_path` wins over `config.db_path`** — pass the path you want as the positional argument.
- **`dim` is locked at table creation.** Keep `config.dim` and the embedder's dimension in sync from the very first run; changing it later requires a re-embed/migration.
- **The diary (Layer V) is off by default.** It only exists when you pass `NexusMemory(diary=True)` (or an explicit `DiaryConfig(enabled=True)`).

## Next steps

- [API Reference](api-reference.md) — every action, request schema, and response shape, plus the convenience wrappers.
- [Embedders](embedders.md) — swap the default `HashingEmbedder` for `SentenceTransformerEmbedder` or `OpenAIEmbedder`, or write your own.
- [Transparency](transparency.md) — `inspect`, `forget`, `pin`, and `update` your own memories.
- [Architecture Overview](../architecture/overview.md) and [Memory Layers](../architecture/memory-layers.md) — how working, episodic, semantic, procedural, and diary layers fit together.
- [Request / Response](../io/request-response.md) and [Data Flow](../io/data-flow.md) — the dict-in / dict-out contract and how an ingest fans out.
- [NexusConfig](../configuration/nexus-config.md) and [Tuning](../configuration/tuning.md) — scoring, dedup threshold, cache, privacy, and per-layer settings.
- [Agent Memory](../use-cases/agent-memory.md) — a worked use case built on this loop.
