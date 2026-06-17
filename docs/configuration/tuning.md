# Tuning & Performance

This page is a practical guide to the tunable knobs in
[`NexusConfig`](../../src/nexus_memory/core/config.py) — how the scoring
parameters (`decay_lambda`, `min_score`, `default_top_k`), the writer's
`redundancy_threshold`, the semantic cache (`cache_size`, `cache_threshold`),
and the per-layer caps trade off **recall**, **precision**, and **latency**. It
closes with the measured local `assemble` latency as an indicative benchmark.

For the full field-by-field reference of every config field see
[NexusConfig](./nexus-config.md); for the diary's own parameters see
[Diary configuration](./diary-config.md). The scoring math is described
canonically in [Retrieval & scoring](../architecture/retrieval-and-scoring.md).

All knobs live on a single dataclass and are passed through the whole stack:

```python
from nexus_memory import NexusMemory, NexusConfig

cfg = NexusConfig(
    decay_lambda=0.01,
    min_score=0.6,
    default_top_k=5,
    redundancy_threshold=0.90,
    cache_size=128,
    cache_threshold=0.95,
)
memory = NexusMemory(db_path="agent.db", config=cfg)
```

---

## The read path at a glance

Knowing where each knob sits in the pipeline makes the trade-offs concrete. An
`assemble` runs (see [The assemble flow](../architecture/retrieval-and-scoring.md)):

```
embed(query)
  → SemanticCache.get()  ── hit (sim ≥ cache_threshold) ⇒ short-circuit, return cached
  → KNN over-retrieve  (k = top_k * 2)
  → 1-hop graph expansion (memory_edges)
  → rank()  =  similarity × importance × decay   (decay uses decay_lambda)
  → filter  score ≥ min_score
  → cap to top_k
  → render <fact .../>
```

The write path applies one knob — dedup:

```
extract facts → embed → 1-NN cosine ≥ redundancy_threshold ⇒ skip (duplicate)
                                                          else INSERT
```

| Knob | Stage | Primary effect |
|------|-------|----------------|
| `cache_threshold`, `cache_size` | before retrieval | latency (cache hit short-circuits) |
| `default_top_k` (`top_k`) | KNN + final cap | recall vs. context size |
| `decay_lambda` | re-rank (decay) | recency bias |
| `min_score` | post-rank filter | precision vs. recall |
| `redundancy_threshold` | write-time dedup | store growth, duplicate suppression |

> Every per-request `assemble` may override `top_k` and `min_score`; the config
> values are only the defaults (`default_top_k`, `min_score`).

---

## Scoring knobs

The re-ranker in [`scoring.py`](../../src/nexus_memory/core/scoring.py) combines
three orthogonal signals into a single **product**:

```
score = similarity × importance × decay

  similarity = clamp(1 − cosine_distance, 0, 1)        # from vector search
  importance = per-fact value in [1, 10]               # set at write time
  decay      = exp(−decay_lambda × days_passed)        # recency weight
```

Because the score is a product, any one signal near zero collapses the whole
score — a perfectly recent, high-importance fact still scores ~0 if it is
semantically unrelated to the query.

### `decay_lambda` — recency bias

Default **`0.01`** per day. `decay = exp(−decay_lambda × days_passed)`, so a
larger `decay_lambda` forgets faster. A few reference points at the default:

| Age (days) | `decay` at λ=0.01 | `decay` at λ=0.05 | `decay` at λ=0.001 |
|-----------:|------------------:|------------------:|-------------------:|
| 0 | 1.000 | 1.000 | 1.000 |
| 7 | 0.932 | 0.705 | 0.993 |
| 30 | 0.741 | 0.223 | 0.970 |
| 90 | 0.407 | 0.011 | 0.914 |
| 365 | 0.026 | ~0 | 0.694 |

- **Raise it** (e.g. `0.05`) for fast-moving contexts where stale facts should
  fade quickly — recency dominates the ranking.
- **Lower it** (e.g. `0.001`) for archival/reference memory where a year-old
  fact is still as relevant as a new one.

Edge cases handled by `time_decay()`: future timestamps clamp `decay` to `1.0`;
an unparseable timestamp is treated as "now" (no decay) with a logged warning.
**Graph-expanded neighbors** (1-hop via `memory_edges`) carry no `distance`, so
they get `similarity = 0` and are ranked by `importance × decay` alone.

> vec0 auxiliary columns do not honor `DEFAULT`, so `timestamp` is supplied
> explicitly on every insert. If you bypass the writer and omit it, time-decay
> is silently disabled for that row. See
> [Persistence](../architecture/persistence.md).

### `min_score` — the precision/recall floor

Default **`0.6`**. After ranking, any candidate with `score < min_score` is
dropped *before* the `top_k` cap. This is the main precision dial:

- **Higher `min_score`** ⇒ fewer, more confident facts (higher precision, lower
  recall). The assembled `<semantic>` block may be empty if nothing clears the
  bar — that is intentional, not an error.
- **Lower `min_score`** (e.g. `0.0`) ⇒ return whatever KNN found, ranked. Useful
  for demos, debugging, and the needle test, where you want to *see* the ranking
  rather than gate on it.

Because the score is a product of three sub-1.0 factors, an effective
`min_score` of `0.6` is already fairly strict once recency decay kicks in — an
older fact needs strong similarity **and** importance to survive. If recall
feels too low after facts age, lower `decay_lambda` before lowering `min_score`.

### `default_top_k` — breadth vs. context budget

Default **`5`**. Sets the default `top_k` when a request omits it. It controls
two things at once:

1. **Retrieval breadth.** KNN over-retrieves `top_k * 2` candidates to give the
   re-ranker headroom; results are capped to `top_k` only *after* filtering.
2. **Context size.** Each surviving fact becomes one `<fact .../>` line, so
   `top_k` bounds the semantic portion of the prompt and the
   `meta.tokens_estimated` estimate.

Raising `top_k` widens the KNN net and admits more facts (better recall, larger
prompt); lowering it tightens the prompt. A backward-compat needle invariant
greps `<fact id="(\d+)">` and asserts there are **≤ `top_k`** of them — only
`<fact>` elements carry `id="..."`, so directives and turns never count against
this cap.

---

## Writer dedup — `redundancy_threshold`

Default **`0.90`** (cosine **similarity**, not distance). At write time each new
fact is embedded and checked against its nearest existing neighbor; if the
1-NN cosine similarity is **≥ `redundancy_threshold`**, the fact is treated as a
duplicate and **skipped**.

| `redundancy_threshold` | Behavior | Trade-off |
|-----------------------:|----------|-----------|
| `1.0` | only exact-vector duplicates skipped | store grows fastest; near-duplicates accumulate |
| `0.90` (default) | near-identical paraphrases collapse | balanced |
| `0.80` | aggressive merging | risks dropping genuinely distinct-but-similar facts |

- **Raise it** toward `1.0` to keep more granular facts (and accept a larger,
  noisier store with redundant lines in retrieval).
- **Lower it** to suppress paraphrase spam, at the risk of discarding a fact
  that *looks* similar to an existing one but carries new information.

This threshold interacts with the embedder: it is cosine over the **active
embedder's** vectors. The default offline
[`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py) is lexical, so
`0.90` collapses near-verbatim restatements; a semantic embedder
(`SentenceTransformerEmbedder`) will collapse meaning-equivalent but lexically
different sentences at the same threshold. Re-tune dedup when you change
embedders. See [Embedders](../usage/embedders.md).

A related write-side knob is `semantic_include_assistant` (default `False`):
when `False`, only the user's turns become semantic facts (the assistant's prose
still goes to the episodic diary), which keeps conversational filler out of the
vector store and indirectly out of retrieval.

---

## The semantic cache — `cache_size` & `cache_threshold`

[`SemanticCache`](../../src/nexus_memory/core/cache.py) is an in-RAM, thread-safe
LRU keyed by the **query embedding**, matched by cosine similarity. On
`assemble`, the query is embedded and looked up first; a hit short-circuits the
entire read path (no KNN, no graph expansion, no re-ranking).

| Field | Default | Meaning |
|-------|---------|---------|
| `cache_size` | `128` | LRU capacity (entries); oldest evicted on overflow |
| `cache_threshold` | `0.95` | cosine similarity for a **hit** (`best_sim ≥ threshold`) |

How a lookup works (`SemanticCache.get`): the query is unit-normalized, dotted
against all stored unit keys, and the best match is a hit iff its similarity is
**≥ `cache_threshold`**; a hit refreshes LRU recency. Keys are stored
L2-normalized, so cosine == dot product.

**Tuning `cache_threshold`:**

- **Higher (e.g. `0.99`)** ⇒ only near-identical queries reuse a cached context.
  Safest for correctness — almost no risk of serving a stale answer to a
  different question — but a lower hit rate.
- **Lower (e.g. `0.90`)** ⇒ paraphrased queries hit more often (lower latency),
  at the risk of returning a context assembled for a *subtly different* query.
  Because the default `0.95` is **above** the `0.90` dedup threshold, the cache
  is deliberately stricter about "same query" than the writer is about "same
  fact."

**Tuning `cache_size`:** larger caches retain more distinct query contexts
across a session (higher hit rate, more RAM); the cost is purely memory — entries
are small (an embedding key plus the assembled result).

> **Freshness caveat.** The cache is keyed by query, not by store contents. A new
> `ingest` does **not** invalidate cached contexts. In a long-lived process where
> facts change between reads of the *same* query, call `memory.close()`/restart or
> keep `cache_size` modest so entries cycle out — or set `cache_threshold` high so
> only truly repeated queries are served from cache.

---

## Per-layer caps

These bound how much each non-semantic layer contributes to the assembled
context (and, for Layer I, how much volatile history is retained).

| Field | Default | Layer | Effect |
|-------|---------|-------|--------|
| `working_memory_max_turns` | `50` | I (working, RAM) | ring-buffer capacity; oldest turn silently evicted on overflow |
| `episodic_recent_turns` | `6` | II (episodic) | how many recent turns `assemble` injects into `<recent_dialogue>` |
| `procedural_max_directives` | `12` | IV (procedural) | cap on active directives injected, priority-desc |

- **`working_memory_max_turns`** is pure RAM and never persisted. It is the
  **fallback** source of recent dialogue when `episodic_enabled=False`. Raising
  it keeps more recent turns available to that fallback at the cost of memory;
  it does not affect durable storage.
- **`episodic_recent_turns`** directly sizes the `<recent_dialogue>` block (one
  `<turn>` per recent turn). The source is the episodic store when
  `episodic_enabled` (default `True`), otherwise the working buffer — callers
  always get *some* recency. Raising it gives the LLM more verbatim context but a
  larger prompt.
- **`procedural_max_directives`** caps the `<directive>` lines, taken highest
  priority first (1–10). Standing rules are usually few, but this guards against
  a runaway rule set dominating the prompt. Lower it to keep only the top
  directives; the omitted ones still exist and are returned by `list_rules`.

Each cap is a precision-vs-completeness lever on its own section of
`<memory_context>`; none of them touch the semantic KNN/scoring path. See
[Memory layers](../architecture/memory-layers.md) for how the layers compose.

---

## Latency benchmark (indicative)

The read path is engineered to be cheap: a deterministic offline embedder plus a
vec0-indexed KNN, with the semantic cache short-circuiting repeats. Measured
**locally** — seeded with ~192 facts, cache cleared before each call, 50
iterations of a single `assemble` at `top_k=5`:

| Metric | Value |
|--------|-------|
| median | **~3.2 ms** |
| p95 | ~3.4 ms |
| representative `latency_ms` | ~3.2 ms |
| MS6.1 target | retrieval **< 80 ms** |

The measured median is comfortably (~25×) under the 80 ms target. Every
`assemble` response also reports its own wall-clock `latency_ms`, so you can
measure on your own hardware and workload:

```python
result = memory.process({"action": "assemble", "query": "...", "top_k": 5})
print(result["latency_ms"], result["meta"]["source_count"])
```

> **Not a CI gate.** This benchmark is informational only — absolute numbers vary
> with hardware, store size, embedder, and `top_k`. The
> [test suite](../../tests/) (151 tests) gates correctness, not
> latency. Swapping in a heavier embedder (e.g. `SentenceTransformerEmbedder` or
> a network-bound `OpenAIEmbedder`) shifts the cost onto `embedder.encode()` and
> will dominate this figure.

---

## A tuning recipe

A pragmatic order for adjusting recall/precision/latency without thrashing:

1. **Start from defaults.** They are tuned for the offline `HashingEmbedder`.
2. **Too few results?** Lower `min_score` first (it gates hardest), then raise
   `top_k`. If older facts are dropping out, lower `decay_lambda` instead.
3. **Too noisy / duplicate-heavy results?** Lower `redundancy_threshold` to
   suppress paraphrases at write time, and/or raise `min_score`.
4. **Recency matters more (or less)?** Adjust `decay_lambda` — up to forget
   faster, down to keep archival facts competitive.
5. **Latency on repeated queries?** Lower `cache_threshold` (more hits) or raise
   `cache_size`; weigh against the freshness caveat above.
6. **Prompt too large?** Lower `top_k`, `episodic_recent_turns`, and
   `procedural_max_directives` — each trims a distinct section of
   `<memory_context>`.

> Re-tune `redundancy_threshold` and `cache_threshold` whenever you change the
> embedder — both are cosine thresholds over that embedder's vector space.

---

## See also

- [NexusConfig reference](./nexus-config.md) — every config field and default.
- [Diary configuration](./diary-config.md) — Layer V (`update_every`,
  `section_size`, `max_sections`, `inject_days`).
- [Retrieval & scoring](../architecture/retrieval-and-scoring.md) — the scoring
  model and read path in full.
- [Embedders](../usage/embedders.md) — how the embedder choice interacts with the
  cosine thresholds here.
- [`scoring.py`](../../src/nexus_memory/core/scoring.py),
  [`cache.py`](../../src/nexus_memory/core/cache.py),
  [`config.py`](../../src/nexus_memory/core/config.py) — the source of truth.
