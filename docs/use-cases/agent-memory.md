# Use Case: Persistent Agent Memory

This is the headline use case for Nexus Memory: giving a chat agent durable,
long-term memory across sessions. This page walks through the full loop —
ingesting `(query, response)` interactions, assembling a prompt-ready
`<memory_context>` block for the next turn, and the *needle-in-a-haystack*
property that lets one distinctive fact resurface from ~100 distractors at
`top_k=3`.

If you have not yet seen the API surface, start with
[Getting Started](../usage/getting-started.md) and the
[API Reference](../usage/api-reference.md); this page assumes the basic shape of
[`NexusMemory.process()`](../../src/nexus_memory/core/orchestrator.py).

---

## 1. The problem

A stateless LLM forgets everything between turns. The conventional fix —
stuffing the whole transcript back into the prompt — does not scale: context
windows fill, latency climbs, and the *one* fact that matters (where the user
keeps their keys, what their deadline is) drowns under hundreds of irrelevant
exchanges.

Nexus Memory solves this by turning the dialogue stream into a layered,
queryable store and rendering a single, compact `<memory_context>` document on
demand. The agent loop becomes:

1. **Ingest** each `(query, response)` exchange as it happens (asynchronous).
2. Before the next model call, **assemble** a `<memory_context>` for the current
   user query.
3. Prepend that block to the prompt and let the LLM answer with full recall.

Everything is backed by **one SQLite file** (with the `sqlite-vec` extension);
the default [`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py) is
deterministic and offline, so the whole memory runs with no network and no
model download. See the [Architecture Overview](../architecture/overview.md) for
how the layers fit together.

---

## 2. The agent loop, end to end

The complete lifecycle goes through the single `process()` entry point. This is
[`examples/basic_usage.py`](../../examples/basic_usage.py), runnable offline once
the package is installed (`python examples/basic_usage.py`):

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

Two invariants make this safe to wire into a real agent loop:

- **`process()` never raises.** Every failure comes back as
  `{"status": "error", "error": "<message>"}`, so always branch on
  `result["status"]` before reading other keys.
- **Ingest is asynchronous.** Working memory (Layer I) updates synchronously,
  but the durable semantic/episodic/procedural writes run on a background
  thread. A fact is **not** visible to an `assemble` issued immediately after
  `ingest` — call [`wait()`](../usage/api-reference.md) (or `close()`, which
  waits internally) first. In a long-running agent you rarely need `wait()`
  between turns; the gap between ingesting turn *N* and assembling for turn
  *N+1* is usually enough, but call it whenever a read **must** observe a
  just-written fact.

---

## 3. Ingesting interactions

Each call stores one user/assistant exchange. The request is an `IngestRequest`:

| Key | Type | Default | Required |
|-----|------|---------|----------|
| `action` | `"ingest"` | — | yes |
| `interaction` | `{"query": str, "response": str}` | — | yes |
| `metadata` | `dict \| null` | `null` | no |
| `priority` | `int (1–10) \| null` | `null` | no |

The response is immediate:

```python
{"status": "processing", "task_id": "<uuid4>", "estimated_completion_ms": 50}
```

`estimated_completion_ms` is always `50` — a coarse, non-binding hint, not a
measurement. On the background thread the writer fans out across the durable
layers:

1. A [`FactExtractor`](../../src/nexus_memory/layers/semantic/extraction.py)
   (default `SpeakerAwareExtractor`) turns the interaction into scored atomic
   facts. By default only **user** turns become semantic facts
   (`semantic_include_assistant=False`); assistant prose still flows to the
   episodic transcript.
2. Each fact is embedded, **deduplicated** (1-NN cosine ≥
   `redundancy_threshold`, default **0.90**), and written to the `agent_memory`
   vec0 table (Layer III).
3. After the semantic write, ordered consolidators run as side effects:
   `EpisodicConsolidator` → `ProceduralConsolidator` →
   `DiaryConsolidator` (only when Layer V is enabled).

See [Memory Layers](../architecture/memory-layers.md) for the per-layer detail
and the [Data Flow](../io/data-flow.md) page for the byte-level ingest path.

> **Why user-centric?** Semantic memory is about *what the agent knows about the
> user*. The needle test below buries its fact as a **user** statement for
> exactly this reason — the assistant's confirmation ("Got it, I'll remember
> that.") is episodic, not a fact.

---

## 4. Assembling the `<memory_context>`

Before each model call, ask for a context tailored to the live query. The
request is an `AssembleRequest`:

| Key | Type | Default | Required |
|-----|------|---------|----------|
| `action` | `"assemble"` | — | yes |
| `query` | `str` | — | yes |
| `top_k` | `int` | `5` | no |
| `min_score` | `float` | `0.6` | no |

[`ContextAssembler`](../../src/nexus_memory/core/context.py) composes every layer
into **one** document: it delegates the semantic block to
[`MemoryReader`](../../src/nexus_memory/layers/semantic/reader.py) (embed query →
KNN over-retrieve `top_k*2` → multi-signal re-rank →
filter by `min_score`), then nests the procedural directives, the recent
dialogue, and — when enabled — the diary fragments around it.

A rendered context block looks like this:

```xml
<memory_context>
  <procedural>
    <directive priority="2">Keep answers concise.</directive>
    <directive priority="1">Address the user as Sam.</directive>
  </procedural>
  <semantic>
    <fact id="12" importance="7" score="0.83" timestamp="...">User: ...</fact>
  </semantic>
  <recent_dialogue>
    <turn role="user" timestamp="...">...</turn>
  </recent_dialogue>
</memory_context>
```

Send `context_xml` straight to your LLM. The full `assemble` response is a
superset, with `raw_facts` provided for introspection:

```python
{
    "status": "success",
    "context_xml": "<memory_context>...</memory_context>",
    "raw_facts": [{"id": int, "content": str, "score": float, "timestamp": str}, ...],
    "directives": ["Keep answers concise.", "Address the user as Sam."],
    "recent_dialogue": [{"role": str, "content": str, "timestamp": str}, ...],
    "meta": {"tokens_estimated": int, "source_count": int,
             "directive_count": int, "recent_count": int},
    "latency_ms": float,
}
```

The scorer combines three orthogonal signals into a pure product —
`score = similarity × importance × decay` — so a fact rises on lexical/semantic
overlap, its assigned importance (`1`–`10`), and recency
(`exp(−decay_lambda × days_passed)`, default `λ = 0.01/day`). The full ranking
model is documented in
[Retrieval and Scoring](../architecture/retrieval-and-scoring.md).

> **Tuning `min_score` for recall.** The default floor is `0.6`. The walkthrough
> and the needle test set `min_score=0.0` to disable the floor and rank purely
> on the product — appropriate when you want the best available match even if
> its absolute score is low. Raise it when you would rather inject nothing than
> a weak fact. See [Tuning](../configuration/tuning.md).

---

## 5. The needle-in-a-haystack property

The defining retrieval guarantee: a single distinctive fact, buried under a pile
of unrelated chatter, still surfaces in the top results. This is validated by
[`tests/test_integration.py::test_needle_in_haystack_top_3`](../../tests/test_integration.py)
(it passes).

### The scenario

1. **Bury the needle** — ingest one distinctive user statement:
   *"I always keep my house keys in the blue ceramic bowl on the kitchen
   counter."*
2. **Add ~100 distractors** — ingest 100 unrelated exchanges about weather,
   sports scores, and stock-market trends.
3. **Query for the needle** at `top_k=3`, `min_score=0.0`.

```python
nexus.process({
    "action": "ingest",
    "interaction": {"query": NEEDLE, "response": "Got it, I'll remember that."},
})
nexus.wait()

for i in range(100):
    nexus.process({
        "action": "ingest",
        "interaction": {
            "query": f"random discussion {i}",
            "response": (
                f"Distractor note {i}: today we talked about weather, "
                f"sports scores, and stock market trends in segment {i}."
            ),
        },
    })
nexus.wait()

res = nexus.process({
    "action": "assemble",
    "query": "where do I keep my house keys blue ceramic bowl kitchen counter",
    "top_k": 3,
    "min_score": 0.0,
})
```

### What the test asserts

| Assertion | Why it matters |
|-----------|----------------|
| `res["status"] == "success"` | The read path completed. |
| `"blue ceramic bowl"` appears in `res["raw_facts"]` contents | The needle is in the **top 3** ranked facts, beating ~100 distractors. |
| `"blue ceramic bowl"` appears in `res["context_xml"]` | The needle made it into the rendered block the LLM actually sees. |
| `len(re.findall(r'<fact id="(\d+)"', res["context_xml"])) <= 3` | The XML respects `top_k`; the context stays compact. |

That last assertion leans on a deliberate rendering invariant: **only `<fact>`
elements carry `id="..."`**. Directives, dialogue turns, and diary fragments omit
`id` precisely so that greping `<fact id="(\d+)"` counts retrieved facts and
nothing else.

### Why it works at scale

Two mechanisms combine. The
[`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py) maps overlapping
tokens ("house keys", "blue ceramic bowl", "kitchen counter") to overlapping
vector features, so the needle's embedding sits far closer to a needle-shaped
query than to generic distractor prose. The multi-signal scorer then amplifies
that similarity by importance and recency. KNN over-retrieves `top_k*2`
candidates to give the re-ranker headroom before the result is capped to
`top_k`. The net effect: the right fact is retrieved from a haystack without any
network call or learned model.

> **Note on the embedder.** The default `HashingEmbedder` relies on **lexical**
> overlap — it is dependency-free and deterministic, ideal for the needle test
> and for getting started. For paraphrase-robust semantic matching (where the
> query shares *meaning* but not words), swap in
> [`SentenceTransformerEmbedder`](../usage/embedders.md) and keep `config.dim` in
> sync. See [Embedders](../usage/embedders.md).

---

## 6. Beyond raw facts

A real agent's memory is more than a flat fact store. The same `<memory_context>`
the agent receives also carries:

- **Standing behavioral rules** (Layer IV, `<procedural>`) — "Keep answers
  concise.", "Address the user as Sam." — mined from interactions or pinned
  explicitly. See
  [Behavioral Rules](behavioral-rules.md).
- **Recent dialogue** (`<recent_dialogue>`) — the last few verbatim turns
  (`episodic_recent_turns`, default `6`), from the episodic store when enabled
  or the volatile working buffer otherwise.
- **A long-arc narrative** (the optional Layer V diary) — rolling per-session
  summaries folded into a single growing persistent summary, useful when the
  agent needs the *story* of a relationship rather than individual facts. It
  plugs in without the core knowing it exists. See
  [Hierarchical Diary](hierarchical-diary.md).

When you send embeddings to an external API, mask PII before it leaves the
machine — see [Privacy and Encryption](privacy-and-encryption.md).

---

## 7. Inspecting and correcting memory

The same loop is fully transparent. Check store health, audit what was stored,
and correct or remove facts:

```python
health = memory.inspect(type="health")
# [{"count": int, "db_path": str, "db_size_bytes": int, "dim": int}]

facts = memory.inspect(type="semantic")   # newest-first, with a vector_preview
memory.forget(query="house keys")          # delete the best semantic match
memory.forget(fact_id=12)                  # or delete by id
```

For programmatic edits, the
[`TransparencyInterface`](../../src/nexus_memory/core/transparency.py) (reachable
via `memory.transparency`) adds `pin()` and `update()` — used by the lifecycle
test [`test_full_lifecycle_pin_update_forget`](../../tests/test_integration.py)
to pin a high-importance fact, edit it in place (a DELETE + re-INSERT that
preserves the original timestamp so a correction does not artificially boost
recency), and finally forget it. The full transparency surface is documented in
[Transparency](../usage/transparency.md).

---

## 8. Performance

A local benchmark over ~192 seeded facts (cache cleared before each call, 50
iterations, `top_k=5`):

| Metric | Value |
|--------|-------|
| Median `assemble` latency | ~3.2 ms |
| p95 | ~3.4 ms |

That is comfortably under the project's retrieval target (< 80 ms). The
dependency-free `HashingEmbedder` and the vec0 indexed KNN keep the read path
cheap; absolute numbers vary with hardware. A repeated query can be even
cheaper: the in-RAM
[`SemanticCache`](../../src/nexus_memory/core/cache.py) short-circuits the read
path on a near-identical query embedding (cosine ≥ `cache_threshold`, default
`0.95`).

---

## See also

- [Getting Started](../usage/getting-started.md) — install and first run.
- [API Reference](../usage/api-reference.md) — every action and response shape.
- [Retrieval and Scoring](../architecture/retrieval-and-scoring.md) — the ranking model.
- [Memory Layers](../architecture/memory-layers.md) — what each layer owns.
- [Embedders](../usage/embedders.md) — swapping the default hashing embedder.
- [`examples/basic_usage.py`](../../examples/basic_usage.py) — the runnable loop above.
