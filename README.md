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
- **Privacy by design** — an opt-in regex PII filter can mask emails/phones/names before
  embedding (off by default on the local path, where nothing leaves the machine; enable it
  when embedding via an external API). An optional SQLCipher encryption hook stays off the
  critical path.
- **User-centric memory** — by default only the *user's* statements become semantic facts;
  the assistant's prose is kept in the episodic diary, not the vector store.

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
#   <semantic>
#     <fact id="2" importance="8" score="2.60" timestamp="...">Assistant: You always
#       keep your house keys in the blue ceramic bowl on the kitchen counter.</fact>
#   </semantic>
# </memory_context>
# (the default speaker-aware extractor prefixes each fact with "User:" / "Assistant:")

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
| `inspect`  | `type:"health"\|"episodic"\|"semantic"\|"working"\|"procedural"`, `filter?` | `{status, data}` |
| `optimize` | —                                                        | `{before_bytes, after_bytes, facts}` |
| `diary`    | `day?`, `time_range?`, `store?`                          | `{status, period, summary, turn_count}` |
| `rule`     | `op:"add"\|"list"\|"deactivate"`, `directive?`, `priority?`, `rule_id?` | `{status, rule\|rules\|deactivated}` |
| `distill`  | —                                                        | `{status, promoted:[rule,...]}` |
| `pending_summaries` | `limit?` *(Layer V only)*                       | `{status, jobs:[{job_id, kind, period, prompt, prior_summary, input}, ...]}` |
| `submit_summary` | `job_id`, `summary` *(Layer V only)*               | `{status:"success"\|"superseded"\|"not_found", applied?:"daily"\|"section"}` |

Convenience wrappers: `memory.inspect(...)`, `memory.forget(...)`, `memory.wait(...)`,
`memory.close()`, plus `memory.remember_rule(...)`, `memory.list_rules()`,
`memory.diary(...)`, `memory.working_snapshot()`, `memory.reconstruct(...)`,
`memory.distill()`.

## Multi-layer memory

Beyond the semantic fact store, Nexus implements a **4-layer cognitive memory**
architecture (Atkinson–Shiffrin model). A single `ingest` fans out across all
relevant layers, and `assemble` returns a unified, layer-aware `<memory_context>`:

| Layer | Module | Persistence | Role |
| :-- | :-- | :-- | :-- |
| **I. Working** | `layers/working/working.py` | RAM (volatile) | last *N* turns, fast recency context |
| **II. Episodic** | `layers/episodic/` (`episodic.py` + `summarization.py`) | SQLite | the diary: raw dialogue + narrative day summaries |
| **III. Semantic** | `layers/semantic/` (`reader` / `writer`) + `core/db.py` | SQLite-vec | decontextualized fact vectors (cosine KNN) |
| **IV. Procedural** | `layers/procedural/procedural.py` | SQLite | standing behavioral directives ("Respond in German.") |

Inter-layer transfer (`core/consolidation.py`, `core/context.py`) provides **consolidation**
(write fan-out), **retrieval** (the unified context), and **distillation**
(promote recurring semantic preferences into procedural rules). Procedural
directives are meant to be injected into the system prompt so the model's
*behavior* — not just its recalled facts — persists across sessions.

```python
m.process({"action": "ingest", "interaction": {
    "query": "Sprich ab jetzt deutsch mit mir.",
    "response": "Alles klar.",
}})
m.wait()
res = m.process({"action": "assemble", "query": "anything"})
assert "Respond in German." in res["directives"]   # detected automatically
print(m.diary()["summary"])                          # narrative episodic summary
```

See [`docs/ms7_multilayer.md`](docs/ms7_multilayer.md) for the full architecture
and the runnable 4-layer demo in
[`../nexus-chat-demo/chat.py`](../nexus-chat-demo/chat.py) (`--selftest`, offline).

## Optional Layer V — the hierarchical diary (off by default)

An **optional** fifth layer adds a self-managing, bounded **diary**: a
*time-pyramid* of LLM-written narrative summaries (rolling per-day diaries folded
into a ring of multi-day epoch sections) — **without the module ever calling an
LLM**. It is **provider-agnostic via a handoff outbox**: when a summary is due,
Nexus *enqueues a job*; the host drains it, runs the prompt on any model, and
hands the text back. It is **off by default** and fully removable — enable it
explicitly with `NexusMemory(diary=DiaryConfig(enabled=True))`.

Two new actions appear only when the layer is on: **`pending_summaries`** (the
host pulls due jobs) and **`submit_summary`** (the host returns model output;
idempotent apply). `assemble` then also emits bounded `<diary>` / 
`<persistent_summary>` sections (no `id="…"`, so the needle invariant holds).

```python
from nexus_memory import NexusMemory, DiaryConfig

m = NexusMemory(db_path="diary.db", diary=DiaryConfig(enabled=True))
# ... ingest turns ...
for job in m.pending_summaries():            # host drains the outbox
    text = my_model(job["prompt"], job["prior_summary"], job["input"])
    m.submit_summary(job["job_id"], text)    # folds into the time-pyramid
print(m.inspect(type="diary")["data"])       # {"days": [...], "sections": [...]}
```

See [`docs/ms8_diary.md`](docs/ms8_diary.md) for the full design and
[`examples/diary_outbox.py`](examples/diary_outbox.py) for a runnable, offline
end-to-end (the host drain loop with a deterministic stand-in model); the chat demo
opts in with `--diary` / `NEXUS_DIARY=1` and shows the pyramid via `/pyramid`.

## How it works

- **Scoring:** `FinalScore = similarity × importance × exp(-λ · days_passed)`
  (`λ = decay_lambda`, default 0.01/day).
- **Embeddings:** default `HashingEmbedder` (768-dim, blake2b feature hashing, L2-normalized)
  preserves lexical overlap so paraphrases retrieve each other. Optional
  `SentenceTransformerEmbedder` / `OpenAIEmbedder` adapters are lazily imported.
- **Storage:** a vec0 virtual table (`distance_metric=cosine`) plus a lightweight
  `memory_edges` graph for 1-hop expansion; WAL mode for concurrent reader/writer access.
- **Config:** tune everything via `NexusConfig` (scoring, dedup threshold, cache, privacy).

## Project structure

```
src/nexus_memory/
  __init__.py            # stable public API (from nexus_memory import NexusMemory, …)
  core/                  # substrate: config, db, embeddings, cache, models, scoring,
                         #   xml_format, privacy, security, transparency,
                         #   orchestrator, context, consolidation
  layers/                # the memory systems, each its own subpackage
    working/             #   I.   volatile RAM buffer
    episodic/            #   II.  diary (raw turns + summaries)
    semantic/            #   III. vector facts (reader / writer / extraction)
    procedural/          #   IV.  behavioral directives
    diary/               #   V.   optional hierarchical diary (off by default)
```

The top-level `__init__.py` re-exports the public surface, so import paths like
`from nexus_memory import NexusMemory` are stable regardless of the internal layout.

## Run the tests

From the project root:

```sh
./.venv/Scripts/python.exe -m pytest -q
```

All 151 tests pass (141 across the core + four layers, plus 10 for the optional
Layer V diary). See [`docs/final_validation.md`](docs/final_validation.md) for the full
report, benchmark numbers, and the needle-in-a-haystack result.

## License

See project metadata in `pyproject.toml`.
