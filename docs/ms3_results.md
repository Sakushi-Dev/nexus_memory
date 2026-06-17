# MS3 — Reader Loop

Status: complete. Validated by `tests/test_scoring.py` (9 tests) plus the assemble paths in
`tests/test_routing.py` and `tests/test_integration.py`.

## Pipeline (`src/nexus_memory/layers/semantic/reader.py`)
`MemoryReader.assemble_context(request, now=None)` runs:

1. **Embed** the query with the configured embedder.
2. **Cache check** — if a `SemanticCache` is present and the query embedding hits
   (cosine `>= cache_threshold`), return the cached result with a refreshed `latency_ms`.
3. **Over-retrieve** — `db.knn_search(embedding, k=top_k*2)` so the re-ranker has headroom.
4. **Graph expansion** — for the strongest hits, pull `db.neighbors(id)` (1 hop) and add any
   memories not already in the pool. Expanded neighbours carry no `distance` (they were not
   vector matches), so the ranker scores them on importance/recency alone (`similarity=0`).
5. **Re-rank** with `scoring.rank`.
6. **Filter** by `min_score`, cap to `top_k`, render XML, return.

Return shape:
`{"status":"success", "context_xml":str, "raw_facts":[{id,content,score,timestamp}],
"meta":{"tokens_estimated":int, "source_count":int}, "latency_ms":float}`.

## Scoring formula (`src/nexus_memory/core/scoring.py`, pure functions)
The final score is the **product** of three signals:

```
FinalScore = similarity × importance × decay
similarity = clamp(1 - cosine_distance, 0, 1)
decay      = exp(-decay_lambda × days_passed)      # decay_lambda default 0.01/day
```

- `similarity_from_distance(distance)` — `1 - distance`, clamped to `[0,1]`.
- `time_decay(timestamp, now=None, lam=0.01)` — parses the stored UTC text timestamp
  (several formats tried, falling back to ISO 8601), computes age in days, returns
  `exp(-lam·days)`. Future timestamps clamp to `1.0`; an unparseable timestamp is treated as
  "now" (no decay) rather than raising.
- `final_score(similarity, importance, decay)` — their product.
- `rank(rows, config, now=None)` — returns **copies** of the rows augmented with
  `similarity`, `decay`, `score`, sorted by `score` descending. `now` is injectable so tests
  are deterministic.

## XML formatting (`src/nexus_memory/core/xml_format.py`)
`format_as_xml(scored_facts)` renders a prompt-ready block. Content is escaped with
`xml.sax.saxutils.escape`; attribute values with `quoteattr`. Empty input yields an empty
`<memory_context></memory_context>` container.

```xml
<memory_context>
  <fact id="12" importance="7" score="0.83" timestamp="2026-06-15 14:30:00">User is building the Nexus library</fact>
</memory_context>
```

`estimate_tokens(text)` returns `len(text) // 4` (~4 chars/token), surfaced as
`meta.tokens_estimated`.
