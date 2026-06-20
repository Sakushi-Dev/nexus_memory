# Transparency & Control

Nexus Memory gives the host full sovereignty over what it has stored. This page covers the four operations that let you **see and correct** your own memory — `inspect` (read-mostly views), `forget` (delete), `update` (re-embed), and `pin` (a manual "never forget" fact). Everything here runs against the local `.db` file only: [`TransparencyInterface`](../../src/nexus_memory/core/transparency.py) never opens a network socket.

All four sit on a thin, deterministic layer over [`NexusDB`](../../src/nexus_memory/core/db.py): the interface orchestrates calls and shapes the response, while all SQL lives in the DB layer.

---

## How you reach it

All four operations are exposed three ways — via `process(payload)`, via a convenience wrapper, and directly on `memory.transparency` — except `inspect`, whose direct/wrapper form serves the diary view while `process()` does not.

| Operation | Via `process(payload)` | Convenience wrapper | Direct on `memory.transparency` |
|-----------|------------------------|---------------------|---------------------------------|
| `inspect` | `{"action": "inspect", ...}` | `memory.inspect(**kw)` | `memory.transparency.inspect(...)` |
| `forget`  | `{"action": "forget", ...}` | `memory.forget(**kw)` | `memory.transparency.forget(...)` |
| `update`  | `{"action": "update", ...}` | `memory.update(target_id, new_content)` | `memory.transparency.update(target_id, new_content)` |
| `pin`     | `{"action": "pin", ...}` | `memory.pin(content, importance=10.0)` | `memory.transparency.pin(content, importance=10.0)` |

The `process()`-routed actions are validated by pydantic models (`extra="forbid"`) before reaching the interface; the direct/wrapper calls go straight through. See the [API Reference](api-reference.md) for the full `process()` action catalog, and [Getting Started](getting-started.md) for the lifecycle (`ingest` → `wait` → read → `close`).

> Ingest is asynchronous. Call [`memory.wait()`](api-reference.md#convenience-wrapper-methods) before any `inspect`/`forget`/`update` that must observe newly ingested facts.

---

## `inspect` — read-mostly views

```python
def inspect(self, type: str = "health", filter: dict | None = None) -> dict
```

Returns `{"status": "success", "data": [...]}` on success, or `{"status": "error", "error": "unknown inspect type: ...", "data": []}` for an unrecognized `type`. The shape of each `data` element depends on `type`.

`filter` is an optional dict. Recognized keys:

| Filter key | Type | Default | Applies to |
|------------|------|---------|------------|
| `limit` | `int` | `50` | `episodic`, `semantic` |
| `time_range` | `[start, end]` ISO strings | `None` | `episodic`, `semantic` |
| `active_only` | `bool` | `True` | `procedural` |

### `type="health"` (default)

A single-element list describing the store:

```python
[{"count": int, "db_path": str, "db_size_bytes": int, "dim": int}]
```

| Field | Meaning |
|-------|---------|
| `count` | Number of semantic memory rows (`NexusDB.count()`). |
| `db_path` | The configured SQLite file path (`config.db_path`). |
| `db_size_bytes` | Total on-disk size of the DB **plus its `-wal` and `-shm` sidecar files**. An in-memory DB or a not-yet-created sidecar contributes `0`. |
| `dim` | Embedding dimension (`config.dim`, default `768`). |

```python
health = memory.inspect(type="health")
print(health["data"][0])
# {"count": 12, "db_path": "agent.db", "db_size_bytes": 81920, "dim": 768}
```

### `type="episodic"`

Memory rows in chronological order (**newest first**), honoring `limit` and `time_range`:

```python
[{"id": int, "timestamp": str, "content": str, "importance": float, "metadata": dict}, ...]
```

```python
recent = memory.inspect(type="episodic", filter={"limit": 10})
for row in recent["data"]:
    print(row["id"], row["timestamp"], row["content"])
```

### `type="semantic"`

Same rows as `episodic`, **plus** a human-facing `vector_preview`:

```python
[{"id": int, "timestamp": str, "content": str, "importance": float,
  "metadata": dict, "vector_preview": [float, float, float, float, "..."]}, ...]
```

Embeddings are not human-readable, so `vector_preview` exposes only the **first 4 dimensions** (each rounded to 4 decimals) followed by an `"..."` marker. The vector is re-encoded on the fly from the row's `content` via the active embedder — it is a faithful preview of how the content embeds, not a dump of stored floats.

```python
sem = memory.inspect(type="semantic", filter={"limit": 5})
print(sem["data"][0]["vector_preview"])
# [0.0123, -0.0456, 0.0789, 0.0011, "..."]
```

### `type="working"`

The volatile Layer I buffer (RAM), newest-last:

```python
[{"role": str, "content": str, "timestamp": str}, ...]
```

Returns `[]` when no working memory is wired (e.g. semantic-only standalone usage). The same data is available via the [`memory.working_snapshot()`](api-reference.md#convenience-wrapper-methods) wrapper. See [Memory Layers](../architecture/memory-layers.md) for what Layer I holds.

### `type="procedural"`

The standing Layer IV directives (behavioral rules):

```python
[{"id": int, "directive": str, "category": str, "priority": int,
  "active": int, "source": str, "timestamp": str}, ...]
```

`filter.active_only` (default `True`) controls whether deactivated rules are included. Returns `[]` when no procedural store is wired. For managing these rules (add / list / deactivate), see [Behavioral Rules](../use-cases/behavioral-rules.md).

> **Diary view is wrapper-only.** `inspect(type="diary")` is **not** part of the `process()`/`InspectRequest` surface. Use the [`memory.inspect(type="diary")` wrapper](api-reference.md#convenience-wrapper-methods), which returns `{"status": "success", "data": {"sessions": [...], "summary": {...} | None}}` (or an error dict when the diary layer is disabled). See [Hierarchical Diary](../use-cases/hierarchical-diary.md).

---

## `forget` — delete a memory

```python
def forget(self, fact_id: int | None = None, query: str | None = None) -> dict
```

Deletes a single memory either by its `id` or by the **best semantic match** of a free-text `query`. Exactly one of `fact_id` / `query` must be supplied — passing both or neither returns an error.

When `query` is given, the text is embedded and resolved through `knn_search(k=1)`; the single closest memory is deleted — **but only if it clears a relevance floor** (see below).

**Responses:**

```python
{"status": "success", "deleted_id": 12}                          # row removed

{"status": "not_found", "deleted_id": None, "fact_id": 12}       # id had no row
{"status": "not_found", "deleted_id": None, "query": "...", "best_similarity": 0.31}  # query below the floor

{"status": "error", "error": "provide exactly one of fact_id or query"}
```

> **Relevance floor on the query path.** `knn_search(k=1)` always returns a row on a non-empty store, so without a guard an unrelated `query` would silently delete a real, irreversible memory. The query path therefore deletes only when the best match's cosine similarity (`1 - distance`) is **≥ [`config.forget_min_similarity`](../configuration/nexus-config.md) (default `0.6`)**. Below the floor it returns `{"status": "not_found", ..., "best_similarity": <score>}` and deletes nothing. The `fact_id` path is exact and bypasses the floor.

```python
# By explicit id (e.g. one you saw in inspect()):
memory.forget(fact_id=12)

# By free-text — deletes the single best KNN match:
memory.forget(query="house keys")
```

> Deletion is permanent and immediate (the row and its vector are removed). To verify, re-run `inspect(type="health")` and check `count`, or `inspect(type="semantic")` for the absent id.

---

## `update` — re-embed and overwrite

```python
def update(self, target_id: int, new_content: str) -> dict
```

> **Three ways to call it:** the [`update`](api-reference.md#action-update) `process()` action, the `memory.update(target_id, new_content)` wrapper, or directly as `memory.transparency.update(...)`.

Replaces the `content` of memory `target_id` with `new_content` and **re-embeds** it, so retrieval reflects the corrected text. At the DB layer this is a DELETE + re-INSERT that preserves the same rowid.

**Responses:**

```python
{"status": "success", "updated_id": 7, "content": "new text"}

{"status": "not_found", "updated_id": None, "target_id": 7}     # no such row
```

```python
res = memory.transparency.update(7, "My deadline moved to next Monday.")
if res["status"] == "success":
    print("re-embedded id", res["updated_id"])
```

---

## `pin` — a manual "never forget" fact

```python
def pin(self, content: str, importance: float = 10.0) -> dict
```

> **Three ways to call it:** the [`pin`](api-reference.md#action-pin) `process()` action, the `memory.pin(content, importance=10.0)` wrapper, or directly as `memory.transparency.pin(...)`.

Inserts a high-importance fact straight into semantic memory, bypassing extraction. It is tagged `metadata={"pinned": True}` and defaults to the maximum importance of `10.0`, so it stays at the top of [time-decay + importance scoring](../architecture/retrieval-and-scoring.md). The `importance` is coerced to `float` before insert.

**Response:**

```python
{"status": "success", "id": 31, "content": "User is vegetarian.", "importance": 10.0}
```

```python
pinned = memory.transparency.pin("The user is vegetarian — never suggest meat dishes.")
print("pinned id", pinned["id"], "importance", pinned["importance"])
```

You can later inspect it (it carries `metadata={"pinned": True}`), `update` its wording, or `forget` it by id like any other memory.

---

## End-to-end example

Adapted from [`examples/basic_usage.py`](../../examples/basic_usage.py) — fully offline with the default [`HashingEmbedder`](embedders.md):

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="agent.db")
try:
    memory.process({
        "action": "ingest",
        "interaction": {
            "query": "where do I keep my keys?",
            "response": "You keep your house keys in the blue bowl on the counter.",
        },
    })
    memory.wait()  # ingest is async — wait before reading

    # Pin a fact that must survive decay:
    memory.transparency.pin("The user lives alone in apartment 4B.")

    # Inspect store health and the semantic view (with vector previews):
    print(memory.inspect(type="health")["data"][0])
    for row in memory.inspect(type="semantic", filter={"limit": 5})["data"]:
        print(row["id"], row["content"], row["vector_preview"])

    # Correct a fact in place, then drop one by best query match:
    memory.transparency.update(1, "Keys live in the green bowl now.")
    print(memory.forget(query="house keys"))
finally:
    memory.close()
```

---

## Notes for integrators

- **Local-only, no network.** Every operation on this page touches only the local `.db` file. The only computation that could leave the machine is embedding text with an **external** embedder (e.g. OpenAI) during `semantic` previews, `forget(query=...)`, `update`, or `pin`. With the default `HashingEmbedder` there is no network at all. See [Privacy & Encryption](../use-cases/privacy-and-encryption.md).
- **Always branch on `status`.** `inspect` and `forget` return status-tagged dicts; `process()` never raises.
- **`wait()` before reading.** Newly ingested facts are not visible to `inspect`/`forget` until the async writer finishes.
- **Deletes are permanent.** There is no undo for `forget`; re-add via `pin` or `ingest` if needed.

## Related pages

- [API Reference](api-reference.md) — the full `process()` action catalog and wrapper table.
- [Getting Started](getting-started.md) — the ingest → assemble → inspect lifecycle.
- [Embedders](embedders.md) — what `vector_preview` is derived from, and offline vs. external models.
- [Memory Layers](../architecture/memory-layers.md) — Layers I–IV that `inspect` surfaces.
- [Retrieval & Scoring](../architecture/retrieval-and-scoring.md) — why a high `importance` (pinned) fact ranks first.
- [`core/transparency.py`](../../src/nexus_memory/core/transparency.py) — the source for everything above.
