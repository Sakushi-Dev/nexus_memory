# Request / Response Model

This page specifies the **I/O contract** of Nexus Memory: the single entry point
[`NexusMemory.process(payload)`](../../src/nexus_memory/core/orchestrator.py), the request
envelope and its pydantic validation, the rule that `process()` **never raises**, and the
two shapes that matter most on the way out — the unified `<memory_context>` XML and the
superset response dict that wraps it. For the exhaustive per-action field tables and
response variants, see the [API reference](../usage/api-reference.md); for the end-to-end
ingest/assemble path, see [Data Flow](data-flow.md).

---

## One entry point

Every interaction with the library goes through one method:

```python
def process(self, payload: dict | str) -> dict
```

- `payload` is a **dict or a JSON string**. A string is `json.loads`-decoded first.
- The `action` field selects the handler.
- The return value is always a **plain dict**.

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="agent.db")
resp = memory.process({"action": "assemble", "query": "what's my deadline?"})
if resp["status"] == "success":
    prompt_block = resp["context_xml"]
```

Convenience wrapper methods (`inspect`, `forget`, `pin`, `update`, `remember_rule`,
`diary`, `wait`, `close`, …) mirror the same handlers for direct programmatic use, but the JSON contract
described here is the canonical surface. The wrappers are catalogued in the
[API reference](../usage/api-reference.md#convenience-wrapper-methods).

---

## The request envelope

A request is a JSON object whose `action` discriminates which
[pydantic v2 model](../../src/nexus_memory/core/models.py) validates it. Every request
model is declared with `model_config = ConfigDict(extra="forbid")`, so **unknown keys are
rejected**, not silently ignored.

| `action` | Request model | Purpose |
|----------|---------------|---------|
| `assemble` | `AssembleRequest` | Build a `<memory_context>` for a query |
| `ingest` | `IngestRequest` | Store one `(query, response)` exchange |
| `forget` | `ForgetRequest` | Delete a memory by id or by semantic match (relevance-gated) |
| `pin` | `PinRequest` | Store/pin a high-importance fact (`importance` default `10.0`) |
| `update` | `UpdateRequest` | Replace an existing fact's content by `target_id` |
| `inspect` | `InspectRequest` | Read health / layer contents |
| `optimize` | `OptimizeRequest` | `VACUUM` + WAL checkpoint |
| `diary` | `DiaryRequest` | Episodic (Layer II) day summary / reconstruction |
| `rule` | `RuleRequest` | Add / list / deactivate procedural directives |
| `distill` | `DistillRequest` | Promote high-importance facts → rules |
| `pending_summaries` | `PendingSummariesRequest` *(diary on)* | Drain the diary outbox |
| `submit_summary` | `SubmitSummaryRequest` *(diary on)* | Apply a model-produced summary |

Validation is dispatched by
[`parse_request(payload)`](../../src/nexus_memory/core/models.py), which routes on the
`action` string and calls `model.model_validate(payload)`. An unknown or missing `action`
produces a `ValidationError` whose `ctx.expected` lists the valid actions.

The two **diary actions** are special: they are validated and routed by the diary layer's
own models in
[`layers/diary/models.py`](../../src/nexus_memory/layers/diary/models.py) **before** the
core dispatcher runs, and **only** when `NexusMemory(diary=True)`.
When the diary is off, those actions fall through to the normal unknown-action error. See
the [diary layer](../architecture/diary-layer.md) and
[diary configuration](../configuration/diary-config.md).

### Minimal examples

```python
{"action": "assemble", "query": "where are my keys?", "top_k": 3, "min_score": 0.0}
{"action": "ingest",   "interaction": {"query": "I prefer Python.", "response": "Noted."}, "priority": 8}
{"action": "forget",   "fact_id": 12}
{"action": "pin",      "content": "User's name is Ada.", "importance": 10.0}
{"action": "update",   "target_id": 12, "new_content": "User now lives in Munich."}
{"action": "rule",     "op": "add", "directive": "Respond in German.", "priority": 9}
```

A handful of fields are constrained at the schema level — e.g. `IngestRequest.priority`
and `RuleRequest.priority` are `Field(..., ge=1, le=10)`; `ForgetRequest` enforces
**exactly one** of `fact_id` / `query` via a model validator; `RuleRequest` requires
`directive` for `op="add"` and `rule_id` for `op="deactivate"`. The optional
`IngestRequest.priority` is an **importance floor**: every fact extracted from that
interaction is stored at *at least* `priority`, never lowering a higher heuristic
importance. The full per-field tables
live in the [API reference](../usage/api-reference.md#action-index).

---

## `process()` never raises

This is the central reliability guarantee: a host can treat the module as a black box that
**always answers with a dict**. Inside
[`process()`](../../src/nexus_memory/core/orchestrator.py), every failure mode is caught
and converted to an error dict of the form:

```python
{"status": "error", "error": "<message>"}
```

| Failure | Returned `error` |
|---------|------------------|
| Malformed JSON string | `"invalid JSON: <decoder message>"` |
| Payload is not an object/dict | `"payload must be a JSON object or dict"` |
| Unknown action or invalid/extra fields | the pydantic `ValidationError` text |
| Handler raised internally | the exception's `str(exc)` |

Because errors are returned rather than raised, the **first thing a caller should do is
branch on `response["status"]`** before reading any other key:

```python
resp = memory.process(payload)
if resp.get("status") == "error":
    handle(resp["error"])
else:
    use(resp)
```

> **Status caveats.** Most responses carry a `status` key, but two actions break the
> pattern: `optimize` returns a bare stats dict with **no** `status`, and `ingest` returns
> `status="processing"` (not `"success"`). See [Status values](#status-values).

---

## The `<memory_context>` output shape

The `assemble` action is the workhorse, and its `context_xml` is the prompt-ready artifact
the host forwards to its LLM. It is built by
[`ContextAssembler.assemble`](../../src/nexus_memory/core/context.py), which delegates the
semantic block to
[`MemoryReader.assemble_context`](../../src/nexus_memory/layers/semantic/reader.py) and
**re-nests the rendered `<fact .../>` lines verbatim** into one document.

```xml
<memory_context>
  <procedural>
    <directive priority="2">Respond in German.</directive>
    <directive priority="1">Be concise.</directive>
  </procedural>
  <semantic>
    <fact id="12" importance="7" score="0.83" timestamp="2026-06-16 09:14:02">User: ...</fact>
  </semantic>
  <recent_dialogue>
    <turn role="user" timestamp="2026-06-17 08:01:55">...</turn>
  </recent_dialogue>
  <!-- the following fragments appear ONLY when Layer V (diary) is enabled: -->
  <diary session="current" seq="7">...this session so far...</diary>
  <diary session="sess-0006" seq="6">...the previous session...</diary>
  <persistent_summary>...the one growing cross-session summary...</persistent_summary>
</memory_context>
```

### Section anatomy

| Section | Element | Source layer | Notes |
|---------|---------|--------------|-------|
| `<procedural>` | `<directive priority="..">` | IV — Procedural | Priority-desc, capped at `procedural_max_directives`. The `priority` attribute is a **synthetic list-rank** of the rendered directives (top item = highest), *not* the rule's stored 1–10 priority. |
| `<semantic>` | `<fact id=".." importance=".." score=".." timestamp="..">` | III — Semantic | KNN + multi-signal re-rank, filtered by `min_score`, capped at `top_k`. |
| `<recent_dialogue>` | `<turn role=".." timestamp="..">` | II — Episodic (or I — Working fallback) | Last `episodic_recent_turns` turns; falls back to the working buffer when `episodic_enabled` is `False`. |
| `<diary session=".." seq="..">` | session narrative | V — Diary *(optional)* | The current session (`session="current"`) plus up to `inject_sessions` prior finalized sessions; spliced in by the diary context provider, after `<recent_dialogue>`. |
| `<persistent_summary>` | session-folded prose | V — Diary *(optional)* | The single growing cross-session summary (no `<section>` children). |

The diary fragments are produced through the generic **context-provider seam**: with no
providers (the default, diary off), the output is **byte-identical** to the three built-in
sections. See [retrieval & scoring](../architecture/retrieval-and-scoring.md) for how the
semantic block is ranked, and the [diary layer](../architecture/diary-layer.md) for the
diary fragments.

### The `<fact id=...>` needle invariant

> **Only `<fact>` elements carry an `id="..."` attribute.** Directives, turns, diary
> entries, and persistent sections deliberately omit `id`.

This is load-bearing. A backward-compatibility test greps the document with the regex
`<fact id="(\d+)"` and asserts there are **`≤ top_k`** matches. Any element that needs a
ranking or grouping attribute uses a *different* attribute name — `priority`, `role`,
`session`, `seq` — precisely so the needle count stays exact regardless of how many
directives, turns, or diary sections are present. Integrators that parse fact ids out of
`context_xml` can rely on this invariant.

All free text is XML-escaped via `xml.sax.saxutils.escape`, and all attribute values via
`quoteattr`, so the document is safe to embed directly in a prompt.

---

## The `assemble` superset response

`context_xml` is just one key. The full `assemble` response is a **superset** that also
exposes the structured ingredients for introspection, plus any keys merged in by context
providers:

```python
{
    "status": "success",
    "context_xml": "<memory_context>...</memory_context>",
    "raw_facts": [{"id": int, "content": str, "score": float, "timestamp": str}, ...],
    "directives": ["Respond in German.", "Be concise."],        # Layer IV, priority desc
    "recent_dialogue": [{"role": str, "content": str, "timestamp": str}, ...],
    "meta": {
        "tokens_estimated": int,
        "source_count": int,        # == len(raw_facts)
        "directive_count": int,
        "recent_count": int,
        # ...plus provider meta keys (e.g. diary) when Layer V is enabled
    },
    "latency_ms": float,
    # ...plus any top-level response keys merged from context_providers
}
```

| Key | Type | Meaning |
|-----|------|---------|
| `status` | `str` | `"success"` on the normal path. |
| `context_xml` | `str` | The prompt-ready `<memory_context>` block. **Send this to the LLM.** |
| `raw_facts` | `list[dict]` | The same facts that appear in `<semantic>`, structured for introspection (id / content / score / timestamp). |
| `directives` | `list[str]` | Active behavioral rules, priority-descending. |
| `recent_dialogue` | `list[dict]` | Recent turns as `{role, content, timestamp}`. |
| `meta` | `dict` | Counts + a token estimate; extended with provider meta when the diary is on. |
| `latency_ms` | `float` | Wall-clock assemble time. |

The `directives`, `recent_dialogue`, and `meta` counts (`directive_count`,
`recent_count`, `source_count`) mirror what is rendered into the XML, so a host can score
or threshold without re-parsing `context_xml`. When the diary layer is active, its provider
**merges** additional top-level keys and `meta` entries into this dict (see
[`ContextAssembler.assemble`](../../src/nexus_memory/core/context.py) step 4 and the
[diary layer](../architecture/diary-layer.md)).

> **`raw_facts` vs `context_xml`.** Forward `context_xml` to the model; use `raw_facts`
> for logging, debugging, transparency UIs, or to drive a `forget`. They describe the same
> facts.

---

## Status values

`status` is a string, not a fixed enum, and its value depends on the action. The values you
will encounter:

| `status` | Where it appears |
|----------|------------------|
| `"success"` | `assemble`, `forget` (deleted), `pin`, `update`, `inspect`, `diary`, `rule` (add/list, deactivate that changed a row), `distill`, `pending_summaries`, `submit_summary` (applied) |
| `"processing"` | `ingest` — the durable write is dispatched on a background thread (`{status, task_id, estimated_completion_ms}`) |
| `"error"` | any caught failure (`{status, error}`) |
| `"not_found"` | `forget` (no id match, or a `query` whose nearest match is below `forget_min_similarity`), `update` (unknown `target_id`), `rule` deactivate (no row changed), `submit_summary` (unknown job) |
| `"superseded"` | `submit_summary` for a job that was already overtaken by a newer one |
| *(no `status` key)* | `optimize` — returns `{before_bytes, after_bytes, facts}` only |

Two behaviors deserve emphasis because they affect read-after-write code:

- **`ingest` is asynchronous.** It returns `status="processing"` immediately and dispatches
  the durable semantic/episodic/procedural (and diary) writes to a background thread. A new
  fact is **not** visible to an `assemble`/`inspect` issued right after until
  [`wait()`](../usage/api-reference.md#convenience-wrapper-methods) (or `close()`, which
  waits internally) returns. `estimated_completion_ms` is a coarse, non-binding hint
  (always `50`), not a measurement. See [Data Flow](data-flow.md) for the full ingest path.
- **`optimize` omits `status`.** Branch on its keys, not on a status string.

### Mutation response shapes

The three direct-edit actions return small, action-specific dicts:

```python
# forget — delete by id, or by relevance-gated semantic match
{"status": "success",   "deleted_id": 12}
{"status": "not_found", "deleted_id": None, "query": "...", "best_similarity": 0.41}  # below forget_min_similarity (default 0.6)

# pin — store a high-importance fact (importance default 10.0)
{"status": "success", "id": 37, "content": "User's name is Ada.", "importance": 10.0}

# update — replace an existing fact's content (re-embedded)
{"status": "success",   "updated_id": 12, "content": "User now lives in Munich."}
{"status": "not_found", "updated_id": None, "target_id": 99}        # unknown target_id
```

`forget(query=...)` is **relevance-gated**: when the nearest match scores below
`NexusConfig.forget_min_similarity` (default `0.6`), it returns `not_found` instead of
deleting an unrelated memory. See [transparency](../usage/transparency.md) and
[nexus configuration](../configuration/nexus-config.md).

---

## End-to-end shape

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="agent.db")
try:
    # 1. Ingest is async → wait before reading.
    memory.process({
        "action": "ingest",
        "interaction": {
            "query": "where do I keep my keys?",
            "response": "You keep your house keys in the blue bowl on the counter.",
        },
    })
    memory.wait()

    # 2. Assemble returns the superset; context_xml is the prompt block.
    resp = memory.process({
        "action": "assemble",
        "query": "where are my house keys?",
        "top_k": 3,
        "min_score": 0.0,
    })
    if resp["status"] == "success":
        send_to_llm(resp["context_xml"])
        log(resp["raw_facts"])           # introspection
finally:
    memory.close()                        # flush async writers, finalize diary, close DB
```

---

## See also

- [API reference](../usage/api-reference.md) — every action's full request/response tables.
- [Data Flow](data-flow.md) — the ingest and assemble pipelines step by step.
- [Architecture overview](../architecture/overview.md) — the five cognitive layers.
- [Retrieval & scoring](../architecture/retrieval-and-scoring.md) — how `<semantic>` is ranked.
- [Diary layer](../architecture/diary-layer.md) and [diary configuration](../configuration/diary-config.md) — the optional `<diary>` / `<persistent_summary>` fragments and outbox actions.
- [Transparency](../usage/transparency.md) — `inspect`, `forget`, and direct edit/pin helpers.
