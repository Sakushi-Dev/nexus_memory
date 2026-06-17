# Retrieval & Scoring

This page documents the semantic **read path** of Nexus Memory: how a query becomes a ranked set of memories and a prompt-ready `<memory_context>` block. It covers the reader pipeline (embed → cache → over-retrieve KNN → 1-hop graph expansion → multi-signal re-rank → `min_score` filter → XML), the exact scoring formulas and their defaults, and the XML shaping rules.

The read path is implemented by [`MemoryReader`](../../src/nexus_memory/layers/semantic/reader.py) over three pure helper modules: [`scoring.py`](../../src/nexus_memory/core/scoring.py), [`xml_format.py`](../../src/nexus_memory/core/xml_format.py), and the [`SemanticCache`](../../src/nexus_memory/core/cache.py). For where this fits in the layered design, see [Memory Layers](memory-layers.md); for the data model behind KNN and the graph, see [Persistence](persistence.md).

---

## The reader pipeline

The entry point is `MemoryReader.assemble_context(request, now=None)`. The `request` is a dict with a `query` and optional `top_k` / `min_score` overrides; `now` is an injectable reference time that makes time-decay (and therefore ranking) deterministic under test.

```python
from nexus_memory.layers.semantic.reader import MemoryReader

reader = MemoryReader(db, embedder, config, cache=cache)
result = reader.assemble_context({"query": "what is the user building?"})

print(result["context_xml"])      # <memory_context> ... </memory_context>
print(result["meta"]["source_count"], "facts")
```

Internally the call runs five stages:

| # | Stage | Implementation | Notes |
|---|-------|----------------|-------|
| 1 | **Embed** | `embedder.encode(query)` | Produces the query vector. See [Embedders](../usage/embedders.md). |
| 2 | **Cache check** | `cache.get(embedding)` | Optional. On a semantic hit, returns the cached result immediately with a refreshed `latency_ms`. |
| 3 | **Over-retrieve (KNN)** | `db.knn_search(embedding, k=max(1, top_k * 2))` | Retrieves **twice** the requested `top_k` to give the re-ranker headroom. |
| 4 | **Graph expansion** | `_expand_graph(candidates)` | Adds 1-hop neighbours of the strongest hits to the candidate pool. |
| 5 | **Re-rank → filter → render** | `scoring.rank(...)` → `min_score` filter → `xml_format.format_as_xml(...)` | Sorts by score, drops rows below `min_score`, caps to `top_k`, renders XML. |

### Resolving `top_k` and `min_score`

Both are read from the request with a fallback to [`NexusConfig`](../configuration/nexus-config.md):

```python
top_k     = int(request.get("top_k", self.config.default_top_k))   # default 5
min_score = float(request.get("min_score", self.config.min_score)) # default 0.6
```

### Stage 2 — Semantic cache short-circuit

If a `SemanticCache` was supplied, the reader consults it **before** touching the vector store. The cache is keyed by query *embeddings*, not exact query strings: a lookup is a hit when the maximum cosine similarity between the query vector and any cached key is at or above `cache_threshold` (default **0.95**). Keys are stored L2-normalized so cosine reduces to a dot product (`matrix @ q`), and a hit refreshes LRU recency.

| Cache parameter | Config field | Default |
|-----------------|--------------|---------|
| Max entries (LRU) | `cache_size` | `128` |
| Similarity threshold for a hit | `cache_threshold` | `0.95` |

On a hit the cached result dict is returned with `latency_ms` recomputed for the current call; on a miss the pipeline proceeds and the freshly assembled result is written back via `cache.put(embedding, result)`.

### Stage 3 — KNN over-retrieval

`db.knn_search(embedding, k)` runs a `sqlite-vec` `MATCH` query against the `agent_memory` table, ordered by **cosine distance ascending**. Each candidate row is:

```text
{id, content, importance, timestamp, metadata(dict), distance(float)}
```

The reader requests `k = top_k * 2` (clamped to a minimum of 1). Over-retrieving lets the multi-signal re-ranker promote an older-but-important or graph-connected fact above a marginally-closer vector match.

### Stage 4 — 1-hop graph expansion

`_expand_graph` walks the typed edges in the `memory_edges` table to pull in *associated* memories that the vector search alone would miss. Behaviour:

- Expansion iterates over the **strongest hits only** — the first `max(1, config.default_top_k)` rows of the distance-ascending KNN pool.
- For each such hit, `db.neighbors(id)` returns the 1-hop target ids reachable via edges; each new id is fetched with `db.get_memory(id)` and appended to the pool.
- Neighbours already present in the pool are skipped (deduplicated by `id`).
- A graph-pulled row carries **no `distance`** (it was not a vector match), so the ranker assigns it `similarity = 0` and scores it on importance and recency alone. Its `final_score` is therefore `0` and it will only survive the `min_score` filter if `min_score <= 0` — expansion widens the *candidate* pool and surfaces association in `raw_facts`, but does not by itself inject zero-similarity neighbours into the final XML under default settings.

### Stage 5 — Re-rank, filter, render

`scoring.rank` returns scored **copies** of the candidate rows sorted highest-score-first (see [Scoring model](#the-scoring-model)). The reader then keeps rows at or above `min_score` and caps the survivors to `top_k`:

```python
ranked = scoring.rank(candidates, self.config, now=now)
kept   = [row for row in ranked if row["score"] >= min_score][:top_k]
```

The kept rows are rendered to `<memory_context>` XML and projected into a `raw_facts` list.

---

## The scoring model

All scoring lives in [`scoring.py`](../../src/nexus_memory/core/scoring.py) as pure, side-effect-free functions. The final score is the **product** of three independent signals:

```text
FinalScore = similarity × importance × decay
```

| Signal | Source | Formula | Range |
|--------|--------|---------|-------|
| **similarity** | vector search `distance` | `clamp(1 − cosine_distance, 0, 1)` | `[0, 1]` |
| **importance** | per-fact multiplier set at write time | stored salience value (default `1.0` if missing/zero) | unbounded (typically `1–10`) |
| **decay** | fact `timestamp` + `now` | `exp(−decay_lambda × days_passed)` | `(0, 1]` |

### Similarity — `similarity_from_distance(distance)`

```python
similarity = 1.0 - float(distance)   # clamped to [0, 1]
```

Cosine distance from the vector store is converted to a similarity in `[0, 1]`; values are clamped to guard against minor floating-point excursions outside the valid cosine range. Candidates with no `distance` (graph-expanded) are assigned `similarity = 0.0`.

### Time-decay — `time_decay(timestamp, now=None, lam=0.01)`

```python
days_passed = (now - created).total_seconds() / 86_400.0
decay       = exp(-lam * days_passed)
```

- **`lam` (decay constant, per day)** defaults to **`0.01`** and is supplied from `config.decay_lambda` by the ranker. Larger values forget faster. At `λ = 0.01`, a fact loses ~1% of its weight per day (~50% at ~69 days).
- **Timestamp parsing** tries the SQLite `CURRENT_TIMESTAMP` format `"%Y-%m-%d %H:%M:%S"` first, then ISO variants, then a final `fromisoformat` fallback. Stored naive UTC text is treated as UTC.
- **Edge cases:** a future timestamp (negative age) clamps `decay` to `1.0`; an unparseable timestamp is treated as "now" (age 0, `decay = 1.0`) with a logged warning rather than raising — keeping ranking robust.

### Final score — `final_score(similarity, importance, decay)`

```python
return float(similarity) * float(importance) * float(decay)
```

### Ranking — `rank(rows, config, now=None)`

Returns **copies** of each input row augmented with `similarity`, `decay`, and `score` keys, sorted by `score` descending. `importance` defaults to `1.0` when missing or falsy. Because `now` is injectable, decay and the resulting order are fully deterministic in tests.

> **Worked example.** A fact with `distance = 0.17` → `similarity = 0.83`, `importance = 7`, and an age of 2 days at `λ = 0.01` → `decay = exp(-0.02) ≈ 0.980`. Final score ≈ `0.83 × 7 × 0.980 ≈ 5.69`. Note that importance can push scores well above 1, so `min_score = 0.6` is a low-end floor, not a similarity cutoff.

### Scoring defaults at a glance

| Parameter | Config field | Default | Effect |
|-----------|--------------|---------|--------|
| Decay constant λ | `decay_lambda` | `0.01` / day | Recency weight `exp(−λ·days)` |
| Minimum kept score | `min_score` | `0.6` | Rows below are dropped |
| Default result cap | `default_top_k` | `5` | Final `top_k`; KNN retrieves `2×` |

See [Tuning](../configuration/tuning.md) for guidance on adjusting these.

---

## The `<memory_context>` XML

Kept rows are rendered by `xml_format.format_as_xml(scored_facts)` into a compact block that a host application can inject directly into a prompt. Fact **content** is escaped with `xml.sax.saxutils.escape`; **attribute values** with `quoteattr`.

```xml
<memory_context>
  <fact id="12" importance="7" score="0.83" timestamp="2026-06-15 14:30:00">User is building the Nexus library</fact>
</memory_context>
```

Each `<fact>` element carries:

| Attribute | Source key | Formatting |
|-----------|------------|------------|
| `id` | `id` | string of the row id |
| `importance` | `importance` | integers stay clean (`7`), floats keep a digit (`%g`); default `1` |
| `score` | `score` | two decimal places (`%.2f`); default `0.00` |
| `timestamp` | `timestamp` | stored UTC text, verbatim |
| *(text body)* | `content` | XML-escaped |

An **empty** input yields a single empty container: `<memory_context>\n</memory_context>`.

### Token estimate

`estimate_tokens(text)` returns `len(text) // 4` (~4 characters per token). This value is surfaced as `meta.tokens_estimated` so callers can budget prompt size without a real tokenizer.

---

## Return shape

`assemble_context` returns:

```python
{
  "status": "success",
  "context_xml": "<memory_context>...</memory_context>",
  "raw_facts": [
    {"id": 12, "content": "...", "score": 5.6912, "timestamp": "2026-06-15 14:30:00"},
    # ...
  ],
  "meta": {"tokens_estimated": 24, "source_count": 1},
  "latency_ms": 3.41,
}
```

| Key | Type | Description |
|-----|------|-------------|
| `status` | `str` | `"success"` on the normal path |
| `context_xml` | `str` | The rendered `<memory_context>` block |
| `raw_facts` | `list[dict]` | Per-fact `{id, content, score, timestamp}`; `score` is the **final** score rounded to 4 decimals |
| `meta.tokens_estimated` | `int` | `len(context_xml) // 4` |
| `meta.source_count` | `int` | Number of facts kept (= `len(raw_facts)`) |
| `latency_ms` | `float` | Wall-clock time for the call (also refreshed on cache hits) |

Note that `raw_facts[].score` is the full `similarity × importance × decay` product (rounded to 4 dp), whereas the `score` *attribute* in the XML is the same value formatted to 2 dp.

---

## Related pages

- [Architecture Overview](overview.md) — where the reader sits in the request lifecycle.
- [Memory Layers](memory-layers.md) — the semantic layer and how it composes with diary/procedural context.
- [Persistence](persistence.md) — the `agent_memory` vector table and the `memory_edges` graph that back KNN and 1-hop expansion.
- [Request / Response](../io/request-response.md) and [Data Flow](../io/data-flow.md) — the `assemble` action contract end-to-end.
- [Embedders](../usage/embedders.md) — how query and fact vectors are produced.
- [Tuning](../configuration/tuning.md) — choosing `decay_lambda`, `min_score`, `top_k`, and cache thresholds.
