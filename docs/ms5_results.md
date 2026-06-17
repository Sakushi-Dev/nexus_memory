# MS5 — Public Interface, Schemas, Transparency

Status: complete. Validated by `tests/test_schema.py` (12 tests) and the transparency paths
in `tests/test_routing.py` / `tests/test_integration.py`.

## Public interface (`src/nexus_memory/__init__.py`)
Exports: `NexusMemory`, `NexusConfig`, `DEFAULT_DIM`, `Embedder`, `HashingEmbedder`,
`__version__ = "0.1.0"`. The package is now organized into `core/` (config, db, embeddings,
cache, models, scoring, xml_format, privacy, security, transparency, orchestrator, context,
consolidation) and `layers/` (working, episodic, semantic, procedural), but the top-level
`__init__.py` **re-exports the public API**, so `from nexus_memory import NexusMemory, ...`
is unchanged. The entire surface is the single `NexusMemory.process()` method
plus convenience wrappers (`inspect`, `forget`, `wait`, `close`) — see
`docs/ms2_results.md` for the routing table.

## Pydantic v2 schemas (`src/nexus_memory/core/models.py`)
Strict request models (`extra="forbid"`):

- `AssembleRequest(action, query, top_k=5, min_score=0.6, filters=None)`
- `Interaction(query, response)` and
  `IngestRequest(action, interaction, metadata=None, priority: int|None ge=1 le=10)`
- `ForgetRequest(action, fact_id=None, query=None)` — a `model_validator` enforces
  **exactly one** of `fact_id` / `query`.
- `InspectRequest(action, type: Literal["episodic","semantic","health"]="health", filter=None)`
- `OptimizeRequest(action)`
- Output helpers `Fact(id, content, score, timestamp)` and
  `AssembleResponse(status, context_xml, raw_facts, latency_ms)`.
- Extraction models `FactItem(content, importance ge=1 le=10)` and
  `ExtractedFacts(facts)` live here and are imported by
  `layers/semantic/extraction.py`.

`parse_request(payload) -> BaseModel` dispatches on `action`; unknown or invalid actions
raise `pydantic.ValidationError`. The orchestrator catches that and returns an error dict.

## Transparency / inspect (`src/nexus_memory/core/transparency.py`)
`TransparencyInterface` is a thin, local-only layer over `NexusDB` (no network):

- `inspect(type="health"|"episodic"|"semantic", filter=None) -> {"status","data"}`:
  - **health** — count, db_path, db_size_bytes (db + `-wal`/`-shm` sidecars), dim.
  - **episodic** — chronological entries (newest first), honouring `limit` / `time_range`.
  - **semantic** — entries plus a `vector_preview` (first 4 rounded dims + `"..."`).
- `forget(fact_id=None, query=None)` — delete by id, or resolve a free-text query to the
  single best `knn_search(k=1)` match and delete it. Returns `deleted_id` or
  `status:"not_found"`.
- `update(target_id, new_content)` — re-embed and `db.update_memory` (DELETE + re-INSERT,
  same rowid).
- `pin(content, importance=10.0)` — manually insert a high-importance "never forget" fact
  tagged `metadata={"pinned": True}`.

## Example (`examples/basic_usage.py`)
Demonstrates the full lifecycle through `process()`: ingest an interaction, `wait()`,
assemble a context, inspect health, and forget — using the default offline `HashingEmbedder`
(no network, no model download).
