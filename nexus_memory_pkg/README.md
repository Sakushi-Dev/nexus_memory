# Nexus Memory

A **local-first**, dependency-light agent-memory library for Python. It gives an LLM
application a persistent, self-managing long-term memory backed by SQLite +
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) — no server, no network, no model
download required for the default path.

- **Local & offline** — a single `.db` file; the default embedder is a deterministic,
  dependency-free hashing vectorizer.
- **Cognitive loops** — a *reader* (retrieve → graph-expand → multi-signal score → XML) and
  an asynchronous *writer* (extract → dedup → store).
- **One entry point** — everything goes through `NexusMemory.process()`, which validates
  every request with pydantic and never raises to the caller.
- **Transparent & sovereign** — inspect, pin, update, and forget your own memories.
- **Privacy by design** — regex PII masking is applied before embedding; an optional
  SQLCipher encryption hook stays off the critical path.

## Install

A project-local virtual environment lives at `.venv`. Install the package into it
(editable):

```sh
./.venv/Scripts/python.exe -m pip install -e .
```

Always use this interpreter (`./.venv/Scripts/python.exe`); do not use a global `python`.

## Quickstart

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="my_agent.db")

# Store an interaction (async; wait() makes it deterministic in a script)
memory.process({
    "action": "ingest",
    "interaction": {
        "query": "where do I keep my keys?",
        "response": "You always keep your house keys in the blue ceramic bowl "
                    "on the kitchen counter.",
    },
})
memory.wait()

# Assemble a prompt-ready memory context
result = memory.process({
    "action": "assemble",
    "query": "where are my house keys?",
    "top_k": 3,
})
print(result["context_xml"])
# <memory_context>
#   <fact id="2" importance="8" score="2.60" timestamp="...">You always keep your
#     house keys in the blue ceramic bowl on the kitchen counter</fact>
# </memory_context>

memory.close()
```

A runnable version is in [`examples/basic_usage.py`](examples/basic_usage.py).

## Actions

Every request is a dict (or JSON string) with an `action` field, passed to
`memory.process(...)`.

| action     | payload (key fields)                                      | returns |
|------------|----------------------------------------------------------|---------|
| `assemble` | `query`, `top_k=5`, `min_score=0.6`, `filters?`          | `{status, context_xml, raw_facts, meta, latency_ms}` |
| `ingest`   | `interaction:{query, response}`, `metadata?`, `priority?` | `{status:"processing", task_id, estimated_completion_ms}` |
| `forget`   | exactly one of `fact_id` / `query`                       | `{status, deleted_id}` |
| `inspect`  | `type:"health"\|"episodic"\|"semantic"`, `filter?`       | `{status, data}` |
| `optimize` | —                                                        | `{before_bytes, after_bytes, facts}` |

Convenience wrappers: `memory.inspect(...)`, `memory.forget(...)`, `memory.wait(...)`,
`memory.close()`.

## How it works

- **Scoring:** `FinalScore = similarity × importance × exp(-λ · days_passed)`
  (`λ = decay_lambda`, default 0.01/day).
- **Embeddings:** default `HashingEmbedder` (768-dim, blake2b feature hashing, L2-normalized)
  preserves lexical overlap so paraphrases retrieve each other. Optional
  `SentenceTransformerEmbedder` / `OpenAIEmbedder` adapters are lazily imported.
- **Storage:** a vec0 virtual table (`distance_metric=cosine`) plus a lightweight
  `memory_edges` graph for 1-hop expansion; WAL mode for concurrent reader/writer access.
- **Config:** tune everything via `NexusConfig` (scoring, dedup threshold, cache, privacy).

## Run the tests

From the project root:

```sh
./.venv/Scripts/python.exe -m pytest -q
```

All 68 tests pass (see [`docs/final_validation.md`](docs/final_validation.md) for the full
report, benchmark numbers, and the needle-in-a-haystack result).

## License

See project metadata in `pyproject.toml`.
