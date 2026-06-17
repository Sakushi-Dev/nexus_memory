# MS4 — Writer Loop

Status: complete. Validated by `tests/test_extraction.py` (7 tests) plus the ingest /
optimize paths in `tests/test_routing.py` and `tests/test_integration.py`.

## Async ingest (`src/nexus_memory/layers/semantic/writer.py`)
`MemoryWriter` decouples writes from the caller's thread.

- `ingest_async(interaction, metadata=None) -> str` — spawns a daemon
  `threading.Thread` running the pipeline and immediately returns a UUID `task_id`.
- `ingest_sync(interaction, metadata=None) -> list[int]` — runs the identical pipeline
  inline and returns the written row ids (used by tests for determinism).
- `wait(timeout=None)` — joins outstanding background threads so tests have deterministic
  state. The orchestrator's `wait()` / `close()` delegate here.

Pipeline (`_ingest`): extract facts → for each: strip, PII-mask, embed, dedup-and-write.
A worker thread never dies silently — exceptions are logged and an optional
`on_complete(task_id, written_ids)` callback fires (with `written_ids=None` on error).
At the **end** of `_ingest`, the writer runs any configured `Consolidator`s (best-effort,
each isolated by `try/except` so a consolidator failure never fails the semantic write).
The orchestrator wires the episodic + procedural consolidators here, so a single `ingest`
fans out to all layers — see `docs/ms7_multilayer.md`.

## Extraction (`src/nexus_memory/layers/semantic/extraction.py`)
`FactExtractor` ABC with `extract(query, response) -> list[dict]`. Two implementations ship;
both validate their output through the pydantic `ExtractedFacts(facts: list[FactItem])`
container (`FactItem.importance` constrained `ge=1, le=10`), so extraction **always** yields
JSON-valid facts.

### `SpeakerAwareExtractor` (default)
The default extractor exploits the fact that an interaction is a `(query, response)` pair, so
it *knows* who said each sentence (a naive splitter throws that away, after which the
assistant's own utterances become indistinguishable from the user's once stored). It:

1. Is **user-centric by default**: only the **user query** becomes semantic facts; the
   assistant's response is kept in the episodic diary, not the vector store — so the store
   is not flooded with conversational prose. Pass `SpeakerAwareExtractor(include_assistant=True)`
   (or set `config.semantic_include_assistant=True`) to also mine the assistant's statements.
2. Prefixes every kept fact with `"User: "` (or `"Assistant: "` when included), so stored
   memory is unambiguous about who said it.
3. Drops conversational **filler** (bilingual DE + EN: `ok`, `danke`, `thanks`, `klar`, …)
   and the **assistant's questions** (never durable facts about the user); **user**
   questions are kept, because they may carry the actual information (a number, a standing
   request).
4. Keeps a short fragment when it contains a **number**, so e.g. `"...remember? 3658?"` does
   not lose the `3658`.
5. Assigns heuristic importance in `[1,10]`: base 5 for user / 3 for assistant, +1/+2 for
   length, +2 if a number is present, +2 if a high-value keyword (DE + EN, e.g. `prefer`,
   `always`, `heiße`, `nummer`, `merken`, …) is present.

### `MockFactExtractor` (baseline)
A simpler, non-default deterministic stand-in for a local SLM (Phi/Gemma), kept as a
documented baseline:

1. Concatenate query + response, split into atomic sentences.
2. Drop filler (`ok`, `thanks`, …) and sentences shorter than 3 tokens.
3. Assign heuristic importance in `[1,10]`: base 4, +1/+2 for length, +2 if a high-value
   keyword (`prefer`, `always`, `deadline`, `email`, …) is present.

It does **not** attribute speakers; pass it explicitly (`NexusMemory(extractor=MockFactExtractor())`)
to opt out of speaker attribution.

## Conflict resolution / dedup
`_resolve_conflict(content, embedding) -> 'insert' | 'redundant'`:
- If the store is empty → `insert`.
- Otherwise `knn_search(k=1)`; if the nearest neighbour's cosine similarity
  (`1 - distance`) `>= config.redundancy_threshold` (0.90) → `redundant` (skipped).
- The `update` decision is reserved by the contract but not emitted at this milestone (a
  fuller SLM contradiction check is out of scope).

`_dedup_and_write` performs the KNN check **and** the insert under a single
`threading.Lock`, so the same fact submitted twice — even concurrently — collapses to
exactly one row. PII masking is applied to fact content *before* embedding so both the
stored text and the vector reflect the redacted form.

## Optimize (`optimize() -> dict`)
Measures the on-disk DB size, acquires the write lock, runs `db.vacuum()`
(`wal_checkpoint(TRUNCATE)` + `VACUUM`), re-measures, and reports
`{"before_bytes", "after_bytes", "facts"}`.
