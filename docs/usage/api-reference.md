# API Reference

The complete contract for the single Nexus Memory entry point: the
[`NexusMemory`](../../src/nexus_memory/core/orchestrator.py) constructor and the
`process(payload)` action surface. Every action, its exact request fields, and its
exact response shape are documented here — copied from the pydantic models and the
orchestrator routing — alongside the convenience-wrapper methods that mirror them.

For a narrative walkthrough see [Getting Started](getting-started.md); for the
request/response envelope and data flow see [I/O: Request &
Response](../io/request-response.md); for configuration fields see
[`NexusConfig`](../configuration/nexus-config.md) and
[`DiaryConfig`](../configuration/diary-config.md).

---

## One entry point, one method

Everything goes through one object and one method:

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="agent.db")
response = memory.process({"action": "assemble", "query": "..."})
```

`process()` accepts a **dict or a JSON string**, validates it against the pydantic
models in [`core/models.py`](../../src/nexus_memory/core/models.py)
(`extra="forbid"` — unknown keys are rejected), routes on the `action` field, and
returns a plain dict. **It never raises.** Any failure — invalid JSON, unknown
action, validation error, or a handler exception — comes back as:

```python
{"status": "error", "error": "<message>"}
```

| Failure mode | `error` text |
|--------------|--------------|
| Invalid JSON string | `invalid JSON: ...` |
| Non-object payload | `payload must be a JSON object or dict` |
| Unknown action / invalid fields | pydantic `ValidationError` text |
| Handler raised | the exception text |

> **Always branch on `response["status"]` before reading other keys.** The only
> action whose success response carries **no** `status` key is
> [`optimize`](#action-optimize).

---

## The `NexusMemory` constructor

Defined in [`core/orchestrator.py`](../../src/nexus_memory/core/orchestrator.py).

```python
NexusMemory(
    db_path: str = "nexus_memory.db",
    *,
    config: NexusConfig | None = None,
    embedder: Embedder | None = None,
    extractor: FactExtractor | None = None,
    summarizer: Summarizer | None = None,
    detector: DirectiveDetector | None = None,
    diary: DiaryConfig | bool | None = None,
) -> None
```

All seven parameters and their defaults:

| Kwarg | Default | Notes |
|-------|---------|-------|
| `db_path` | `"nexus_memory.db"` | SQLite file path. **Always overrides `config.db_path`** — even when an explicit `config` is passed, the constructor assigns `config.db_path = db_path`. |
| `config` | `None` → fresh `NexusConfig` | Pre-built [`NexusConfig`](../configuration/nexus-config.md). When omitted, `NexusConfig(db_path=db_path)` is created. |
| `embedder` | `None` → `HashingEmbedder(dim=config.dim)` | Any [`Embedder`](embedders.md). The default is deterministic, dependency-free, offline. |
| `extractor` | `None` → `SpeakerAwareExtractor(include_assistant=config.semantic_include_assistant)` | Turns interactions into scored facts. Pass `MockFactExtractor()` for the naive sentence splitter. |
| `summarizer` | `None` → `MockSummarizer()` | Episodic (Layer II) day-summary backend (offline, deterministic). |
| `detector` | `None` → `MockDirectiveDetector()` | Mines standing behavioral rules from interactions (offline). |
| `diary` | `None` | Optional Layer V switch. Pass `diary=True` (shorthand for `DiaryConfig(enabled=True)`) or a [`DiaryConfig`](../configuration/diary-config.md) for custom knobs; `False`/`None` leaves it off. The layer is built **only** when the resolved config is enabled — otherwise no diary tables, provider, or routing exist. |

**Construction side effects.** Opens the SQLite connection (loads sqlite-vec,
applies the schema sized to `config.dim`), builds the semantic cache, the
working/episodic/procedural layers, the async writer, the reader, the context
assembler, and the [transparency interface](transparency.md). A per-instance
`session_id` (UUID4) tags every episodic turn written during this run.

---

## Action index

| `action` | Purpose | Handler |
|----------|---------|---------|
| [`assemble`](#action-assemble) | Build a `<memory_context>` for a query | `ContextAssembler.assemble` |
| [`ingest`](#action-ingest) | Store one user/assistant exchange (async durable write) | `MemoryWriter.ingest_async` |
| [`forget`](#action-forget) | Delete a fact by id or best query match | `TransparencyInterface.forget` |
| [`pin`](#action-pin) | Store a high-importance "never forget" fact | `TransparencyInterface.pin` |
| [`update`](#action-update) | Replace a fact's content (re-embed) | `TransparencyInterface.update` |
| [`inspect`](#action-inspect) | Health + layer contents | `TransparencyInterface.inspect` |
| [`optimize`](#action-optimize) | `VACUUM`/compact + WAL checkpoint | `MemoryWriter.optimize` |
| [`diary`](#action-diary) | Episodic (Layer II) day summary or transcript | `EpisodicStore.summarize_day` / `.reconstruct` |
| [`rule`](#action-rule) | Manage procedural (Layer IV) directives | `_route_rule` → `ProceduralStore` |
| [`distill`](#action-distill) | Promote high-importance facts into rules | `consolidation.distill` |
| [`pending_summaries`](#action-pending_summaries-diary-only) | Drain the diary (Layer V) outbox | `DiaryLayer.route` (diary only) |
| [`submit_summary`](#action-submit_summary-diary-only) | Apply a model-produced summary | `DiaryScheduler.submit` (diary only) |

The ten core actions are validated by
[`core/models.py`](../../src/nexus_memory/core/models.py) `parse_request`. The two
diary-only actions are validated by the layer's **own** models in
[`layers/diary/models.py`](../../src/nexus_memory/layers/diary/models.py),
**before** core validation, and only when the diary layer is active — otherwise
they are unknown actions and produce a validation error.

---

## Action: `assemble`

Retrieve a unified, layer-aware `<memory_context>` for a query.

**Request** (`AssembleRequest`):

| Key | Type | Default | Required |
|-----|------|---------|----------|
| `action` | `"assemble"` | — | yes |
| `query` | `str` | — | yes |
| `top_k` | `int` | `5` | no |
| `min_score` | `float` | `0.6` | no |

```python
memory.process({
    "action": "assemble",
    "query": "what's my deadline?",
    "top_k": 5,
    "min_score": 0.6,
})
```

**Response:**

```python
{
    "status": "success",
    "context_xml": "<memory_context>...</memory_context>",
    "raw_facts": [{"id": int, "content": str, "score": float, "timestamp": str}, ...],
    "directives": ["Keep answers concise.", ...],       # Layer IV, priority desc
    "recent_dialogue": [{"role": str, "content": str, "timestamp": str}, ...],
    "meta": {
        "tokens_estimated": int,
        "source_count": int,        # == len(raw_facts)
        "directive_count": int,
        "recent_count": int,
        # plus diary meta keys when Layer V is enabled
    },
    "latency_ms": float,
}
```

Send `context_xml` to your LLM; `raw_facts` is for introspection. The
`<memory_context>` nests `<procedural>`, `<semantic>`, `<recent_dialogue>`, and —
when the diary is on — `<diary>` and `<persistent_summary>` fragments. Only
`<fact>` elements carry `id="..."`. See
[Retrieval & Scoring](../architecture/retrieval-and-scoring.md) for how facts are
scored and ordered.

---

## Action: `ingest`

Store one user/assistant exchange. Working memory (Layer I) updates
**synchronously** on the caller thread; the durable semantic / episodic /
procedural (and diary) writes are dispatched on a **background thread**.

**Request** (`IngestRequest`):

| Key | Type | Default | Required |
|-----|------|---------|----------|
| `action` | `"ingest"` | — | yes |
| `interaction` | `{"query": str, "response": str}` | — | yes |
| `metadata` | `dict \| null` | `null` | no |
| `priority` | `int (1–10) \| null` | `null` | no |

`priority`, when given, acts as an **importance floor**: every fact extracted from
this interaction is stored with *at least* that importance (still clamped to
`[1, 10]`). It only ever raises importance — a higher heuristic score from the
extractor is left untouched — and `null` leaves the extractor's heuristic alone.

```python
memory.process({
    "action": "ingest",
    "interaction": {"query": "I prefer Python.", "response": "Noted — Python."},
    "metadata": {"source": "chat"},
    "priority": 9,   # floor: these facts land at importance >= 9
})
```

**Response:**

```python
{"status": "processing", "task_id": "<uuid4>", "estimated_completion_ms": 50}
```

> `estimated_completion_ms` is a coarse, non-binding heuristic (always `50`), not a
> measurement. An `assemble`/`inspect` immediately after `ingest` will **not** see
> the new facts until [`wait()`](#convenience-wrapper-methods) returns.

---

## Action: `forget`

Delete a memory by id, or by best semantic match of a free-text query (a k=1 KNN
probe). Routed to
[`TransparencyInterface.forget`](../../src/nexus_memory/core/transparency.py).

**Request** (`ForgetRequest`) — **exactly one** of `fact_id` / `query` is required:

| Key | Type | Default |
|-----|------|---------|
| `action` | `"forget"` | — |
| `fact_id` | `int \| null` | `null` |
| `query` | `str \| null` | `null` |

```python
memory.process({"action": "forget", "fact_id": 12})
memory.process({"action": "forget", "query": "house keys"})  # k=1 KNN → delete best match
```

**Response:**

```python
{"status": "success", "deleted_id": 12}
# or
{"status": "not_found", "deleted_id": None, "fact_id": 12}                          # id path, no such row
{"status": "not_found", "deleted_id": None, "query": "...", "best_similarity": 0.3} # query below the floor
{"status": "error", "error": "provide exactly one of fact_id or query"}
```

> **Relevance floor (query path).** `knn_search(k=1)` always returns a row on a
> non-empty store, so an unrelated query would otherwise delete a real, irreversible
> memory. The query path therefore deletes only when the best match's cosine
> similarity is **≥ [`config.forget_min_similarity`](../configuration/nexus-config.md)
> (default `0.6`)**. Below the floor it returns `not_found` with a `best_similarity`
> key and deletes nothing. The `fact_id` path is unaffected.

> Supplying neither or both fails validation at the model level
> (`Exactly one of 'fact_id' or 'query' must be provided.`); the
> `provide exactly one of fact_id or query` error is the wrapper-level guard.

---

## Action: `pin`

Store a high-importance "never forget" fact straight into semantic memory,
bypassing extraction. Routed to
[`TransparencyInterface.pin`](../../src/nexus_memory/core/transparency.py).

**Request** (`PinRequest`):

| Key | Type | Default | Required |
|-----|------|---------|----------|
| `action` | `"pin"` | — | yes |
| `content` | `str` | — | yes |
| `importance` | `float` | `10.0` | no |

```python
memory.process({"action": "pin", "content": "The user is vegetarian — never suggest meat."})
```

The fact is tagged `metadata={"pinned": True}` and defaults to the ceiling
importance of `10.0`, so it stays at the top of
[time-decay + importance scoring](../architecture/retrieval-and-scoring.md).

**Response:**

```python
{"status": "success", "id": int, "content": str, "importance": float}
```

The convenience wrapper [`memory.pin(content, importance=10.0)`](#convenience-wrapper-methods)
mirrors this action.

---

## Action: `update`

Replace an existing fact's `content` and **re-embed** it, so retrieval reflects
the corrected text. Routed to
[`TransparencyInterface.update`](../../src/nexus_memory/core/transparency.py).

**Request** (`UpdateRequest`):

| Key | Type | Default | Required |
|-----|------|---------|----------|
| `action` | `"update"` | — | yes |
| `target_id` | `int` | — | yes |
| `new_content` | `str` | — | yes |

```python
memory.process({"action": "update", "target_id": 7, "new_content": "My deadline moved to next Monday."})
```

At the DB layer this is a DELETE + re-INSERT that preserves the same rowid.

**Response:**

```python
{"status": "success", "updated_id": 7, "content": "new text"}
# or
{"status": "not_found", "updated_id": None, "target_id": 7}     # no such row
```

The convenience wrapper [`memory.update(target_id, new_content)`](#convenience-wrapper-methods)
mirrors this action.

---

## Action: `inspect`

Inspect health and layer contents. Routed to
[`TransparencyInterface.inspect`](../../src/nexus_memory/core/transparency.py).

**Request** (`InspectRequest`):

| Key | Type | Default |
|-----|------|---------|
| `action` | `"inspect"` | — |
| `type` | `"health" \| "episodic" \| "semantic" \| "working" \| "procedural"` | `"health"` |
| `filter` | `dict \| null` | `null` |

`filter` recognizes `limit` (int, default `50`), `time_range` (`[start, end]` ISO
strings), and — for `procedural` — `active_only` (bool, default `True`).

**Response:** always `{"status": "success", "data": [...]}`, or
`{"status": "error", "error": str, "data": []}` for an unknown `type`. The `data`
shape depends on `type`:

| `type` | `data` shape |
|--------|--------------|
| `health` | single-element list `[{"count": int, "db_path": str, "db_size_bytes": int, "dim": int}]`. `db_size_bytes` sums the `.db`, `-wal`, and `-shm` files. |
| `episodic` | newest-first rows `{id, timestamp, content, importance, metadata}` (honors `limit`, `time_range`). |
| `semantic` | episodic rows plus a `vector_preview` (first 4 rounded dims + `"..."`). |
| `working` | volatile Layer I turns `[{role, content, timestamp}, ...]` (empty `[]` if not wired). |
| `procedural` | Layer IV rules; `filter.active_only` defaults to `True`. |

> `inspect(type="diary")` is **not** part of `process()` / `InspectRequest`. Use
> the [`inspect(...)` convenience wrapper](#convenience-wrapper-methods), which the
> diary layer serves directly.

---

## Action: `optimize`

Run `VACUUM`/compact on the SQLite DB and checkpoint the WAL.

**Request** (`OptimizeRequest`): `{"action": "optimize"}`

**Response** — the one action whose response carries **no** `status` key:

```python
{"before_bytes": int, "after_bytes": int, "facts": int}
```

---

## Action: `diary`

Summarize episodic dialogue (Layer II) for a day, or reconstruct a transcript over
a time range. This is the **episodic day-summary** action, distinct from the
optional Layer V diary outbox ([`pending_summaries`](#action-pending_summaries-diary-only)
/ [`submit_summary`](#action-submit_summary-diary-only)) below.

**Request** (`DiaryRequest`):

| Key | Type | Default |
|-----|------|---------|
| `action` | `"diary"` | — |
| `day` | `str (YYYY-MM-DD) \| null` | `null` |
| `time_range` | `[start, end] \| null` | `null` |
| `store` | `bool` | `False` |

```python
memory.process({"action": "diary", "day": "2026-06-17", "store": True})
memory.process({"action": "diary", "time_range": ["2026-06-01 00:00:00", "2026-06-17 23:59:59"]})
```

**Response — `time_range` form** (reconstruction; taken when `time_range` has
exactly 2 elements):

```python
{"status": "success", "time_range": ["...", "..."], "transcript": "[ts] role: content\n..."}
```

**Response — day form** (summary; `day=None` → most recent day **with turns**, so a
late-night session survives a UTC rollover):

```python
{"status": "success", "period": "2026-06-17", "summary": "On ... the user talked about: ...", "turn_count": int}
```

---

## Action: `rule`

Manage procedural (Layer IV) standing directives. Routed via
[`_route_rule`](../../src/nexus_memory/core/orchestrator.py) to `ProceduralStore`.

**Request** (`RuleRequest`):

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `action` | `"rule"` | — | |
| `op` | `"add" \| "list" \| "deactivate"` | — | required |
| `directive` | `str \| null` | `null` | **required when `op="add"`** |
| `category` | `str` | `"other"` | normalized to tone/format/persona/other |
| `priority` | `int (1–10)` | `5` | |
| `rule_id` | `int \| null` | `null` | **required when `op="deactivate"`** |
| `active_only` | `bool` | `True` | used by `op="list"` |

```python
memory.process({"action": "rule", "op": "add", "directive": "Keep answers concise.", "category": "tone", "priority": 9})
memory.process({"action": "rule", "op": "list", "active_only": True})
memory.process({"action": "rule", "op": "deactivate", "rule_id": 3})
```

**Responses by `op`** (via `process()`, `source` is always `"manual"`):

```python
# add
{"status": "success", "rule": {id, directive, category, priority, active, source, timestamp}}

# list   (priority desc, then newest first)
{"status": "success", "rules": [ {rule}, ... ]}

# deactivate
{"status": "success",   "rule_id": 3, "deactivated": True}    # a row changed
{"status": "not_found", "rule_id": 3, "deactivated": False}   # nothing changed
```

See [Behavioral Rules](../use-cases/behavioral-rules.md) for end-to-end usage.

---

## Action: `distill`

Promote standing-preference patterns from high-importance semantic facts
(importance ≥ 5.0, up to 200 facts scanned) into procedural rules with
`source="auto"`. Routed to
[`consolidation.distill`](../../src/nexus_memory/core/consolidation.py).

**Request** (`DistillRequest`): `{"action": "distill"}`

**Response:**

```python
{"status": "success", "promoted": [ {rule}, ... ]}   # empty list if nothing detected
```

---

## Action: `pending_summaries` (diary-only)

> Requires `NexusMemory(diary=True)`. When the diary is off,
> this action is unknown and `process()` returns a validation error.

Drain the diary outbox — return the pending summarization jobs the host must run on
its own model. Validated by `PendingSummariesRequest`
([`layers/diary/models.py`](../../src/nexus_memory/layers/diary/models.py)),
routed through
[`DiaryLayer.route`](../../src/nexus_memory/layers/diary/layer.py).

**Request:**

| Key | Type | Default |
|-----|------|---------|
| `action` | `"pending_summaries"` | — |
| `limit` | `int \| null` | `null` |

```python
memory.process({"action": "pending_summaries"})
```

**Response:**

```python
{
    "status": "success",
    "jobs": [
        {
            "job_id": "<uuid4>",
            "kind": "session" | "summary",
            "session": "<session_id>" | None,   # job["target"] for kind="session", else None
            "prompt": "<Nexus-owned prompt; forward verbatim>",
            "prior_summary": str | None,        # rolling summary to refine/extend
            "input": [ {"id": int, "role": str, "content": str}, ... ],  # session: rolling overlapping window (both roles, up to diary_window turns); summary: the session entries to fold
        },
        ...
    ],
}
```

The host forwards `prompt`, `prior_summary`, and `input` to its model, then calls
[`submit_summary`](#action-submit_summary-diary-only). See
[Hierarchical Diary](../use-cases/hierarchical-diary.md) and the
[Diary Layer architecture](../architecture/diary-layer.md).

---

## Action: `submit_summary` (diary-only)

> Requires the diary layer (as above).

Hand a model-produced summary back to the diary for application. Validated by
`SubmitSummaryRequest` (`job_id` and `summary` both `min_length=1`), routed through
[`DiaryScheduler.submit`](../../src/nexus_memory/layers/diary/scheduler.py).

**Request:**

| Key | Type | Required |
|-----|------|----------|
| `action` | `"submit_summary"` | yes |
| `job_id` | `str` (non-empty) | yes |
| `summary` | `str` (non-empty) | yes |

```python
memory.process({"action": "submit_summary", "job_id": jid, "summary": "The user is building a memory library."})
```

**Response:**

```python
{"status": "success" | "not_found" | "superseded", "applied": "session" | "summary" | None}
```

Idempotent: resubmitting a done/superseded job is a safe no-op.

---

## Convenience wrapper methods

These methods on
[`NexusMemory`](../../src/nexus_memory/core/orchestrator.py) mirror `process()`
actions for direct programmatic use, plus the lifecycle helpers.

| Method | Returns | Notes |
|--------|---------|-------|
| `inspect(**kw)` | inspect dict | Wraps `TransparencyInterface.inspect`. `inspect(type="diary")` → `{"status": "success", "data": {"sessions": [...], "summary": {...} | None}}`, or `{"status": "error", "error": "diary layer not enabled"}` when off. **(0.5.1)** `inspect(type="aux")` → `{"status": "success", "data": {pending, by_kind, oldest, aux_connected, kinds_registered}}` (the background-job outbox snapshot; `aux_connected` is `True` once any job has completed), or the `aux bus not enabled` error when off. |
| `forget(**kw)` | forget dict | Wraps `TransparencyInterface.forget` (`fact_id=` or `query=`). |
| `pin(content, importance=10.0)` | pin dict | Wraps `TransparencyInterface.pin`. Stores a high-importance pinned fact (`metadata={"pinned": True}`). Mirrors the [`pin`](#action-pin) action. |
| `update(target_id, new_content)` | update dict | Wraps `TransparencyInterface.update`. Replaces a fact's content and re-embeds it. Mirrors the [`update`](#action-update) action. |
| `remember_rule(directive, category="other", priority=5, source="manual")` | rule dict | Add/reactivate a directive. Unlike `process(rule add)`, `source` is settable. |
| `list_rules(active_only=True)` | `list[dict]` | Stored procedural rules. |
| `diary(day=None, store=False)` | summary dict | Episodic day summary (`day=None` → latest day with turns). |
| `working_snapshot()` | `list[dict]` | Volatile Layer I buffer `[{role, content, timestamp}, ...]` (`[]` if unwired). |
| `reconstruct(time_range=None)` | `str` | Human-readable episodic transcript. |
| `history(*, role=None, max_turns=None, max_tokens=None, token_counter=None, as_format="messages", template="{role}: {content}")` | `list[dict]` **or** `str` | Native LLM message history over the durable episodic layer (working buffer fallback). Three formats, two truncation modes, optional role filter. See [history()](#history--native-message-history) below. |
| `tokens(scope="full", *, messages=None, response=None, config=None)` | `int` **or** `dict[str, int]` | Token accounting over the **actual round-trip** (`messages` array + `response`), split by section: `system` (the whole system message), `input` (user/assistant messages), `output` (the reply). `config=` picks the counter (default `len(s)//4`, or optional `tiktoken`). `int` for one scope; `{scope: int}` + `"total"` for a list. See [tokens()](#tokens--token-accounting) below. |
| `distill()` | `{"status": "success", "promoted": [...]}` | Promote facts → rules. |
| `pending_summaries(limit=None)` | `list[dict]` **or** error dict | Diary outbox jobs (handoff shape above). Error dict when the diary is off. |
| `submit_summary(job_id, summary)` | `{"status", "applied"}` or error dict | Apply a model summary. Error dict when the diary is off. |
| `drain_diary(run_job)` | `int` | One-call diary drain: loops `pending_summaries()` + `submit_summary()` for you, calling the host callable `run_job(job: dict) -> str` per job and applying each non-empty result. Returns the number of jobs applied; returns `0` when the diary layer is off. Nexus still never calls an LLM — `run_job` is the host's model. |
| `drain_aux(run_job, kind=None, limit=None)` | `dict` **or** error dict | **(0.5.0)** Unified drain across *all* background-job kinds on the shared AuxBus (the diary's `session`/`summary` today; procedural and future kinds next). Loops the pending outbox, calls `run_job(job: dict) -> str` per job and applies each non-empty result via the kind's registered handler. **(0.5.1)** `run_job` may also be a `{kind: callable}` mapping for per-kind routing (optional `"default"` key); a kind with no callable is skipped (left pending). Returns `{"status", "applied", "skipped", "by_kind"}`; error dict when the aux bus is off (the bus is diary-scoped at 0.5.0). Nexus still never calls an LLM — `run_job` is the host's model. |
| `pending_aux_jobs(kind=None, limit=None)` | `list[dict]` **or** error dict | **(0.5.0)** Pending background jobs across kinds, generic handoff shape `{job_id, kind, target, prompt, prior_summary, input}`; filter with `kind` (str or iterable). Error dict when the aux bus is off. |
| `submit_aux_job(job_id, result)` | `dict` **or** error dict | **(0.5.0)** Apply a host result to any aux job; dispatched by `kind` to the registered handler (unknown kind → `{"status": "skipped"}`, never raises). Error dict when the aux bus is off. |
| `wait(timeout=None)` | `None` | Block until async ingests finish. Call before `assemble`/`close` in scripts. |
| `close()` | `None` | Flush background writers, finalize the diary (if on), close the DB. Call in `try/finally`. (`close()` waits internally, so a prior `wait()` is redundant but safe.) |

> **Three ways to edit.** `pin` and `update` are reachable via the
> [`pin`](#action-pin) / [`update`](#action-update) `process()` actions, via the
> `memory.pin(...)` / `memory.update(...)` wrappers above, **and** directly on
> [`TransparencyInterface`](../../src/nexus_memory/core/transparency.py)
> (`memory.transparency.pin(content, importance=10.0)` /
> `memory.transparency.update(target_id, new_content)`). `update()` returns
> `{"status": "success", "updated_id", "content"}` (or `not_found`); `pin()`
> returns `{"status": "success", "id", "content", "importance"}` and tags the row
> `metadata={"pinned": True}`. See [Transparency](transparency.md).

---

## `history()` — native message history

A method-only convenience accessor on
[`NexusMemory`](../../src/nexus_memory/core/orchestrator.py) (no `process()`
action) that returns the conversation history ready to feed straight into a chat
LLM. It reads the **durable episodic layer** (Layer II) — or, when
`config.episodic_enabled` is `False`, the volatile working buffer (Layer I) —
mirroring the same source selection as `assemble`'s `recent_dialogue`. Turns are
always **chronological (newest-last)**.

```python
messages = memory.history(max_turns=20)          # [{"role": ..., "content": ...}, ...]
response = your_llm.chat(messages + [user_msg])  # feed straight into a native call
```

**Parameters** (all keyword-only):

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `role` | `"user" \| "assistant" \| None` | `None` | Keep only turns with this role; `None` keeps both. Any other value raises `ValueError`. |
| `max_turns` | `int \| None` | `None` | Explicit turn cap (turns mode). |
| `max_tokens` | `int \| None` | `None` | Explicit token budget (tokens mode). **Takes precedence over `max_turns`** when both are given. |
| `token_counter` | `(str) -> int \| None` | `None` | Counter used in tokens mode. Defaults to the `len(s) // 4` heuristic (matching `WorkingMemory.token_estimate`). |
| `as_format` | `"messages" \| "turns" \| "string"` | `"messages"` | Output shape (below). Any other value raises `ValueError`. |
| `template` | `str` | `"{role}: {content}"` | Per-turn format string, used only for `as_format="string"`. |

**Return** — `list[dict]` for `"messages"`/`"turns"` (`[]` when empty), or `str`
for `"string"` (`""` when empty):

| `as_format` | Returns |
|-------------|---------|
| `"messages"` | `[{"role", "content"}, ...]` — drop-in chat history. |
| `"turns"` | `[{"role", "content", "timestamp"}, ...]` — adds the UTC timestamp. |
| `"string"` | a newline-joined transcript rendered via `template`, e.g. `"user: hi\nassistant: hello"`. |

**Truncation modes** (explicit args win over config defaults):

- **turns** — keep the last *N* turns. Used when `max_turns` is given, or when
  neither arg is given and [`config.history_truncation`](../configuration/nexus-config.md)
  is `"turns"` (default cap `config.history_max_turns`, **20**).
- **tokens** — walk newest→oldest accumulating `token_counter(content)` and keep
  the newest suffix that fits the budget, then restore chronological order. Used
  when `max_tokens` is given, or when `config.history_truncation` is `"tokens"`
  (default budget `config.history_token_budget`, **2000**).

A non-positive budget (`max_turns <= 0` / `max_tokens <= 0`) yields an empty
result.

**Role filter** — pass `role="user"` or `role="assistant"` to keep only one
side of the dialogue; the filter is applied before truncation.

> **Durability.** With the default `config.episodic_enabled=True`, `history()` is
> backed by the durable episodic store, so it **survives restart** — a fresh
> `NexusMemory` on the same `db_path` returns the same turns. The working-buffer
> fallback (episodic disabled) is in-session only. No new storage is introduced;
> `history()` is a read-only view over what Layers II/I already own. See
> [Memory Layers](../architecture/memory-layers.md#unified-history-over-working--episodic).

---

## `tokens()` — token accounting

A method-only convenience accessor on
[`NexusMemory`](../../src/nexus_memory/core/orchestrator.py) (no `process()`
action) that counts tokens over the **actual LLM round-trip** — the request
`messages` array plus the model's `response`, i.e. exactly what crosses the wire
— and splits it by *section* (not storage layer). By default it uses the offline
`len(s) // 4` heuristic; via `config=` you can switch to the optional **tiktoken**
backend (or any custom counter) for exact counts.

- **system** — the full system message(s): the host's base prompt **and**
  everything Nexus injects into it (`directives` + `facts`). Because the recalled
  facts/directives live inside the system message, they count here — not under
  `input`. Nexus doesn't need to own the base prompt to count it: you hand it the
  array you actually sent.
- **input** — the rest of the prompt: every `user`/`assistant` message (the
  conversation history plus the current user turn). Everything except `system`.
- **output** — the model's `response` (the completion).

```python
messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_msg}]
answer = your_llm.chat(messages)

usage = memory.tokens(["system", "input", "output"], messages=messages, response=answer)
# {"system": 52, "input": 35, "output": 8, "total": 95}  (default len//4 heuristic)

exact = memory.tokens("full", messages=messages, response=answer, config="gpt-4o")
# exact tiktoken count (requires: pip install nexus-memory[tiktoken])
```

**Parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `scope` | `str \| list[str]` | `"full"` | What to count (below). A list yields a per-scope breakdown. Unknown scopes raise `ValueError`. |
| `messages` | `list[dict] \| None` | `None` | The request array you send the LLM (`[{role, content}]`). Used by `system`/`input`/`full`; treated as `[]` if omitted. |
| `response` | `str \| None` | `None` | The model's reply text (the `output`); `""` if omitted. |
| `config` | `None \| callable \| str \| dict` | `None` | **How to count.** `None` → offline `len(s) // 4` heuristic; a `(str) -> int` callable → used as-is; `"tiktoken"` → tiktoken's `cl100k_base`; a model name (e.g. `"gpt-4o"`) → tiktoken's encoding for that model; `{"model"\|"encoding": ...}` → explicit tiktoken selection. Requesting tiktoken without it installed raises `ImportError`. |

**Scopes:**

| scope | counts |
|-------|--------|
| `system` | all `role == "system"` message content |
| `input` | all `role in ("user", "assistant")` message content |
| `output` | the `response` text (the model's completion) |
| `full` | `system` + `input` + `output` (**default**) |

**Return** — `int` for a single scope; for a list, a `{scope: int}` dict with an
extra `"total"` key (the sum of the listed scopes).

> **Optional tiktoken.** The default counter is offline and dependency-free. For
> exact token counts install the extra — `pip install nexus-memory[tiktoken]` —
> and pass `config="tiktoken"`, a model name, or `{"encoding": "cl100k_base"}`.

---

## End-to-end lifecycle

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="agent.db")
try:
    # Ingest is asynchronous — wait() before assembling in a script.
    memory.process({
        "action": "ingest",
        "interaction": {
            "query": "where do I keep my keys?",
            "response": "You keep your house keys in the blue bowl on the counter.",
        },
    })
    memory.wait()

    result = memory.process({
        "action": "assemble",
        "query": "where are my house keys?",
        "top_k": 3,
        "min_score": 0.0,
    })
    if result["status"] == "success":
        print(result["context_xml"])   # prompt-ready <memory_context> block

    print(memory.inspect(type="health")["data"][0])  # {count, db_path, db_size_bytes, dim}
finally:
    memory.close()
```

Runnable scripts live in [`examples/`](../../examples) (`basic_usage.py`,
`diary_outbox.py`).

---

## Related pages

- [Getting Started](getting-started.md) — install, first ingest/assemble loop.
- [I/O: Request & Response](../io/request-response.md) and [Data Flow](../io/data-flow.md) — the envelope and the path a payload takes.
- [Choosing an Embedder](embedders.md) and [Transparency](transparency.md).
- [`NexusConfig`](../configuration/nexus-config.md), [`DiaryConfig`](../configuration/diary-config.md), and [Tuning](../configuration/tuning.md).
- [Architecture Overview](../architecture/overview.md) and [Memory Layers](../architecture/memory-layers.md).
