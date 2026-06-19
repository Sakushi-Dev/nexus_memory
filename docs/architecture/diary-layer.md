# Layer V — The Hierarchical Diary (Outbox)

This page is a deep dive on Nexus Memory's optional fifth layer: a self-managing, bounded **diary** that compresses raw dialogue into a *session-pyramid* of narrative summaries and carries the conversation **across session boundaries** — **without the module ever calling an LLM itself**. It covers the session-pyramid (L0/L1/L2), the three tables, the handoff outbox protocol, the trigger state machine, context injection, and the off-by-default / fully-removable property.

The whole subsystem lives in [`src/nexus_memory/layers/diary/`](../../src/nexus_memory/layers/diary/). It is **additive and off by default**: with no `DiaryConfig` passed, the layer is never constructed — no new tables, no new context keys, no new actions. Delete that folder and Nexus runs exactly as before.

For where this layer sits among the others, see [Memory Layers](memory-layers.md); for the broader picture, see the [Architecture Overview](overview.md). Configuration knobs are documented in [Diary Configuration](../configuration/diary-config.md), and an end-to-end walkthrough lives in [Use Cases — Hierarchical Diary](../use-cases/hierarchical-diary.md).

## Why a handoff outbox (no LLM inside Nexus)

The defining design choice: **Nexus owns the prompt, the host owns the model.** When a summary is due, the diary does not call any model — it **enqueues a job** (a `prompt` + `prior_summary` + `input`) into an **outbox** table. The host drains the outbox whenever it likes, runs the job on *any* model (a hosted API such as Claude, a local model, even a human), and hands the text back via `submit_summary`.

This keeps the module:

- **provider-agnostic** — it never imports an LLM SDK ([`prompts.py`](../../src/nexus_memory/layers/diary/prompts.py) ships only text templates);
- **fully offline-testable** — a job is just data, driven manually in tests;
- **async by construction** — `ingest` only *schedules* (cheap, non-blocking); the LLM work happens out-of-band. A stale outbox merely makes the diary lag, it never loses data.

```text
ingest ──(due?)──▶ enqueue job ──▶ [ summarization_jobs (outbox) ]
                                         │  host pulls
                       pending_summaries() ──▶ host runs prompt+context on ITS model
                                         ▼
                       submit_summary(job_id, text) ──▶ Nexus persists into L1 / L2
```

## The session-pyramid

The unit of the diary is a **session** — one `NexusMemory` process run, identified by `orchestrator.session_id` (a `uuid4` assigned in `__init__`). Because uuids are not orderable, the diary store assigns each new session a monotonic `seq` (1, 2, 3, …); `seq` is what orders *current* vs *previous* and what triggers the fold. The diary maintains three levels of decreasing granularity and increasing coverage:

```text
        granularity ▲                          coverage ▼
  L0  episodic_turns        raw user/assistant turns               (Layer II, every ingest)
  L1  diary_sessions        1 rolling summary per SESSION           (updated every N=5 interactions)
  L2  persistent_summary    1 single growing summary, extended      (every 6 finalized sessions)
                            once per 6 finalized sessions
```

| Symbol | Config field ([`config.py`](../../src/nexus_memory/layers/diary/config.py)) | Meaning | Default |
| :-- | :-- | :-- | :-- |
| `N` | `update_every` | interactions between rolling session-diary updates | **5** |
| — | `diary_window` | turns (×2 rows) re-sent per rolling session job (overlap) | **20** |
| — | `max_sentences` | upper bound of the entry's `2-N` sentence range | **50** |
| — | `sessions_per_summary` | finalized session diaries folded per extension | **6** |
| — | `summary_max_sentences` | upper bound (cap) of the single growing summary | **300** |
| `K` | `inject_sessions` | **additional** previous finalized session diaries injected (`0..6`) | **1** (previous session) |

- **L0 — `episodic_turns`** (owned by [Layer II — Episodic](memory-layers.md)): the raw turns. Newest detail is served verbatim by `<recent_dialogue>`. Episodic tags each turn with its `session_id` at ingest time, which is how the scheduler scopes its window to the current session.
- **L1 — `diary_sessions`**: one **rolling** narrative per session, written as the assistant's own **first-person prose**. Every `N=5` interactions a session job is enqueued whose `prior_summary` is the session's current text and whose `input` is a rolling, **overlapping** window of up to `diary_window=20` turns (both roles) *scoped to that session*. The summary is *refined in place* (reconciling the overlap against the prior entry) rather than rewritten from scratch.
- **L2 — `persistent_summary`**: a **single, ever-growing** first-person summary (one SQLite row). It is *created* at the first 6-session mark and **extended in place** at every subsequent 6-session mark — the same row, never frozen, no ring. It is capped at `summary_max_sentences=300` sentences (configurable). There is no `section_size`/`max_sections` ring anymore.

The persistent summary grows without bound in *coverage* (every finalized session eventually folds into it) but stays bounded in *size* by the `summary_max_sentences` cap — the model is asked to extend the prior text and drop redundancy, not append. (Older predecessor versions used a fixed ring of dated sections; that is gone.)

## The three tables

`DiaryStore` ([`store.py`](../../src/nexus_memory/layers/diary/store.py)) owns all diary SQL and creates its three tables `IF NOT EXISTS` on construction. It is **only ever constructed when `DiaryConfig.enabled`**, so nothing is created when the layer is off. The DDL lives in the store module (not in `schema.sql`), following the same shared-connection / shared-write-lock pattern as `EpisodicStore`.

### `diary_sessions` (L1)

| Column | Type | Meaning |
| :-- | :-- | :-- |
| `session_id` | TEXT PK | the `orchestrator.session_id` (`uuid4`) of the session |
| `seq` | INTEGER UNIQUE | monotonic order (`1,2,3…`); orders current/previous + the 6-fold |
| `summary` | TEXT | latest rolling narrative for the session |
| `covered_through` | INTEGER | max `episodic_turns.id` already applied (high-water mark) |
| `interaction_count` | INTEGER | interactions seen this session |
| `finalized` | INTEGER | `1` once the session is closed (rollover/close) |
| `folded` | INTEGER | `1` once folded into the persistent summary |
| `created_at` / `updated_at` | TEXT | UTC `YYYY-MM-DD HH:MM:SS` |

### `persistent_summary` (L2 — one singleton row)

| Column | Type | Meaning |
| :-- | :-- | :-- |
| `id` | INTEGER PK CHECK(id=1) | the singleton row — there is only ever one |
| `summary` | TEXT | the single growing summary |
| `session_count` | INTEGER | sessions folded so far |
| `first_session` / `last_session` | TEXT | covered range (`session_id`) |
| `updated_at` | TEXT | UTC timestamp |

### `summarization_jobs` (the outbox)

| Column | Type | Meaning |
| :-- | :-- | :-- |
| `job_id` | TEXT PK | `uuid4` |
| `kind` | TEXT | `session` \| `summary` |
| `target` | TEXT | session: the `session_id`; summary: constant `'1'` (the singleton) |
| `status` | TEXT | `pending` \| `done` \| `superseded` (default `pending`) |
| `prompt` | TEXT | Nexus-owned instruction (host forwards verbatim) |
| `input_json` | TEXT | JSON `{prior_summary, items:[...]}` |
| `advance_to` | INTEGER | session: `covered_through` to set on apply; summary: `NULL` |
| `created_at` / `answered_at` | TEXT | UTC timestamps |

An index `idx_jobs_status(status, created_at)` backs oldest-first draining. **Invariant: at most one `pending` job per `(kind, target)`** — `enqueue_job` first marks any existing pending job for the same `(kind, target)` as `superseded` before inserting the new one.

These tables survive a process restart: a brand-new `NexusDB` on the same path re-runs the idempotent `CREATE TABLE IF NOT EXISTS` and finds the existing rows.

## The handoff protocol

Two `process()` actions are added by the layer, validated by the layer's own request models (`PendingSummariesRequest` / `SubmitSummaryRequest` in [`models.py`](../../src/nexus_memory/layers/diary/models.py) — the core models are untouched). See also the [API Reference](../usage/api-reference.md).

| action | input | output |
| :-- | :-- | :-- |
| `pending_summaries` | `{ "limit"?: int }` | `{status, jobs:[job, ...]}` (oldest-first) |
| `submit_summary` | `{ job_id, summary }` | `{status:"success" \| "superseded" \| "not_found", applied?:"session" \| "summary"}` |

`NexusMemory.drain_diary(run_job)` is the one-call host-side drain: it wraps the `pending_summaries()` + `submit_summary()` loop, calling `run_job(job)` per job and applying each non-empty result (returns the count applied, `0` when the layer is off). The module still never calls an LLM — `run_job` is the host's model.

### The job object the host receives

Built by `DiaryLayer._to_handoff` ([`layer.py`](../../src/nexus_memory/layers/diary/layer.py)):

```json
{
  "job_id": "uuid",
  "kind": "session",
  "session": "5f1c…-uuid4",
  "prompt": "You are the assistant. Keep a personal diary of this session, written in your own voice in the first person ('I'). ...",
  "prior_summary": "…the session's/persistent current summary, or \"\" if none yet…",
  "input": [
    {"id": 1, "role": "user", "content": "...", "timestamp": "..."},
    {"id": 2, "role": "assistant", "content": "...", "timestamp": "..."}
  ]
}
```

- `session` is the `session_id` for `session` jobs and `null` for `summary` jobs (built by `_to_handoff`).
- `prompt` is forwarded **verbatim** to the model (the `2-N` sentence range is already filled in from `max_sentences`; for `summary` jobs the cap from `summary_max_sentences`).
- `prior_summary` is the current L1/L2 summary (an empty string `""` when there is none yet) — it drives **rolling refinement** (the model edits the prior text rather than starting over). (It can be `null` only via raw JSON when no prior exists, but the scheduler always passes `""`.)
- `input` for `session` jobs is a rolling, **overlapping** window of up to `diary_window` turns (**both roles**, ascending by `id`) **scoped to that session** — not a strict delta. It always includes the last `diary_window` turns (overlap, for reconciliation) and everything ingested since the last applied drain (completeness). For `summary` jobs it is the list of the finalized session diaries being folded (`[{session_id, summary}, ...]`, up to `sessions_per_summary` of them).

The host composes its own model call however it wants — e.g. `system = prompt`, `user = prior_summary + render(input)` — produces text, then calls `submit_summary(job_id, text)`.

**Apply is idempotent.** Submitting a `done` / `superseded` / unknown job is a safe no-op returning a status note, never an error or a raise:

| Submitted job state | Result |
| :-- | :-- |
| `pending` session | `{"status":"success","applied":"session"}` |
| `pending` summary | `{"status":"success","applied":"summary"}` |
| already `done` | `{"status":"success", ...}` (no-op, summary unchanged) |
| already `superseded` | `{"status":"superseded", ...}` |
| unknown `job_id` | `{"status":"not_found"}` |

### The two shipped prompts

Nexus ships two prompt templates in [`prompts.py`](../../src/nexus_memory/layers/diary/prompts.py):

- **`SESSION_PROMPT`** (a template — `{max_sentences}` is filled at enqueue time) — *"You are the assistant. Keep a personal diary of this session, written in your own voice in the first person ('I'). Given your prior entry and the recent turns of the conversation — both what the user said and what you said in reply — produce an updated entry of 2-{max_sentences} sentences … Write it as flowing prose … never use bullet points, numbered lists, headings, or any categorical structure … The recent turns may include turns already reflected in your prior entry; do not restate them, only incorporate genuinely new developments. When a newer turn corrects or contradicts your prior entry, treat the newer turn as authoritative …"*
- **`SUMMARY_PROMPT`** (a template — `{summary_max_sentences}` is filled at enqueue time) — *"You are the assistant, keeping one growing persistent summary of everything across your sessions, written in your own first-person voice. Given your prior persistent summary and these new session entries, extend it into a single coherent first-person prose summary of up to {summary_max_sentences} sentences — never lists or headings. Preserve the throughline across all sessions, weave in the genuinely new developments, and drop redundancy rather than restating what the prior summary already covered."*

Because the session window **overlaps** the prior entry (it re-sends up to `diary_window` turns rather than a strict delta), overlap-correctness is a model-quality dependency: the prompt instructs the model to reconcile (merge/revise) rather than append. A naive append-style host would double-count the overlap — see the reconciling stub in `examples/diary_outbox.py`.

## The trigger state machine

`DiaryScheduler` ([`scheduler.py`](../../src/nexus_memory/layers/diary/scheduler.py)) is the heart of the layer. It runs **inside the existing consolidation step** of `ingest` (via `DiaryConsolidator`, which runs on the writer's background thread *after* the episodic consolidator, so the current interaction's turns are already in `episodic_turns`) and **inside `submit_summary`**. It only enqueues/dequeues jobs and updates rows — **it never calls a model.** The scheduler reads the session's turns directly from `episodic_turns` via the shared connection, never importing `EpisodicStore`. The current `session_id` reaches the scheduler through a zero-arg `session` callable (the orchestrator injects `lambda: self.session_id`; tests inject a stub).

### On each ingested interaction — `on_interaction()`

Let `current = session()` (the current `session_id`) and `last = max_seq_session()` (the highest-`seq` row in `diary_sessions`).

1. **Session rollover** — if `last` exists, `last.session_id != current`, and `last` is not finalized: mark it `finalized = 1` and enqueue a **final session job** for `last` (with `force=True`, so a finalized-but-unfolded session is never stranded by the empty-tick guard below). This replaces the old day-rollover (`today > last`): a new process run seen while the previous session is still open closes the previous session.
2. **Upsert** `diary_sessions[current]` (`INSERT OR IGNORE`, assigning `seq = max(seq) + 1` only when new) and `interaction_count += 1`.
3. **Session cadence** — if `interaction_count % N == 0`: enqueue a **rolling session job** for `current` with `prior_summary = current.summary`, `input =` a rolling, **overlapping** window of up to `diary_window` turns of that session (both roles, ascending by `id`, scoped via `episodic_turns.session_id`), and `advance_to = max(input.id)`. The window's lower edge is `min(covered_through + 1, newest_id - diary_window*2 + 1)` — so it always carries the last `diary_window` turns (overlap, for reconciliation) and never drops anything ingested since the last applied drain (completeness). If the first selected row is an orphaned `role=='assistant'` (its paired user row fell outside the window) it is dropped, so the window starts on a turn boundary. Enqueuing **supersedes** any earlier pending session job for `current` (the one-pending invariant), so two N-ticks before a submit leave exactly one `pending` job and one `superseded`.
4. **Fold trigger** — if `len(finalized_unfolded_sessions()) >= sessions_per_summary`: enqueue a **summary fold job** (respecting the one-pending-summary invariant below).

A rolling session job short-circuits on the **empty-tick guard**: if the session has no turns, or nothing new was ingested since the last *applied* summary (`advance_to == covered_through`), no job is enqueued. The rollover and `finalize()` paths bypass this guard (`force=True`) so a closing session always gets its final job.

### On `submit_summary` with `kind = "session"` — `_apply_session`

Set `summary` and `covered_through = advance_to` (a monotonic last-applied high-water mark — it no longer *gates* the window, the overlapping window does that; it only powers the empty-tick guard and lets finalize/fold terminate), mark the job `done`. Then re-check the fold trigger: if `len(finalized_unfolded_sessions()) >= sessions_per_summary`, enqueue a summary fold job (respecting the one-pending-summary invariant).

### Folding finalized sessions into the single summary — `_enqueue_summary` (`kind = "summary"`)

Keep **at most one pending summary job** (`target = '1'`, the singleton). If one is already pending, return. Otherwise take the oldest `sessions_per_summary` finalized-unfolded sessions (chronological by `seq`) as the batch, and enqueue one summary job: `input = [{session_id, summary}, ...]` for the batch, `prior_summary =` the current persistent summary text (`""` if none yet), `advance_to = NULL`.

### On `submit_summary` with `kind = "summary"` — `_apply_summary`

Resolve the batch's session rows, then **extend the single `persistent_summary` row** in place via `upsert_summary(text, folded_sessions)`: it creates the row on the first fold or updates the same row thereafter, sets `summary = text`, bumps `session_count` by the batch size, sets `first_session` (only when not already set) and `last_session`. Each folded session is marked `folded = 1`, and the job is marked `done`. There is **no ring, no freeze, no slot allocation** — just one growing row. Then:

- **Drain the fold queue**: if another full batch of finalized-unfolded sessions remains, call `_enqueue_summary` again so they fold next, strictly in order.

This *one-pending + chronological fold queue* invariant guarantees finalized sessions fold strictly in order, with no mid-batch races, even if the host is offline across many sessions. Driving many sessions keeps the single `persistent_summary` row growing in coverage while the `summary_max_sentences` cap (asked of the model) keeps its size bounded.

### On `finalize()` — `NexusMemory.close()`

Take the highest-`seq` session; if it is not already finalized, mark it `finalized`; then enqueue its final session job (with `force=True`, bypassing the empty-tick guard so the closing session is never stranded). **Nothing is run** — jobs simply persist in SQLite for the next session's host to drain.

## Context injection

When the layer is active, `DiaryContextProvider` ([`provider.py`](../../src/nexus_memory/layers/diary/provider.py)) is registered on the generic `context_providers` seam that the `ContextAssembler` iterates after its three built-in sections (see [Retrieval & Scoring](retrieval-and-scoring.md) for assembly and the [Data Flow](../io/data-flow.md) page for the request lifecycle). It appends bounded fragments inside `<memory_context>`, after `<recent_dialogue>`:

```xml
<memory_context>
  <procedural>...</procedural>
  <semantic>... <fact id=".."/> ...</semantic>
  <recent_dialogue>... the current session's raw turns ...</recent_dialogue>
  <diary session="current" seq="7">...the CURRENT session's narrative (always injected)...</diary>
  <diary session="…uuid4…" seq="6">...the PREVIOUS finalized session's narrative (K=1)...</diary>
  <persistent_summary>...the single growing summary...</persistent_summary>
</memory_context>
```

- The **current session's** diary is **always injected** — as `<diary session="current" seq="…">` — *even when the session is not finalized* (it has a non-empty summary). This is the core change of the session rework: the conversation is now persistently available in the prompt within the live session, not only after it closes.
- Then up to `K = inject_sessions` (default **1**) **additional previous** finalized session diaries are injected — selected newest-first by `seq` strictly below the current session's `seq` (non-empty summaries only), then rendered chronologically newest-last, each as `<diary session="…" seq="…">`.
- Then exactly one `<persistent_summary>` (the single growing row), if it exists and is non-empty.
- **Crucially, no element carries `id="..."`** — so the backward-compatible *needle invariant* (the count of `<fact id="(\d+)"` stays `≤ top_k`; only semantic facts have ids) is preserved. Text is XML-escaped via `xml.sax.saxutils.escape` / `quoteattr`, exactly like `core/context.py`.

### Additive response keys

`assemble`'s response gains these keys (and only these); existing keys are unchanged, and when the layer is off the keys are absent entirely:

| Key | Shape |
| :-- | :-- |
| `diary` | `[{"session", "seq", "current", "summary"}, ...]` (current first, then previous; `[]` when none) |
| `persistent_summary` | `{"summary", "session_count", "first_session", "last_session"}` or `null` |
| `meta.diary_chars` | total length of the injected diary summaries |
| `meta.session_diary_count` | number of injected `<diary>` fragments |

## Off-by-default and fully removable

The diary is a **clean bolt-on**:

- The layer is built only when the diary is opted in — `NexusMemory(diary=True)`, or an explicit `DiaryConfig(enabled=True)` for custom knobs (`diary=True` is normalized to the latter). Otherwise `self._diary is None`: no diary tables are created, the two diary actions are unknown (a normal validation error), and the convenience wrappers return `{"status":"error","error":"diary layer not enabled"}`.
- The **only** existing files that change are:
  - `core/orchestrator.py` — all diary wiring is guarded by the `diary` flag (build the layer with `session=lambda: self.session_id`, append its consolidator, register its provider, route the two actions before `core.models.parse_request`, call `finalize()` on close).
  - `core/context.py` — a single generic, diary-agnostic ~10-line seam: a `context_providers` list the `ContextAssembler` iterates after its three built-in sections. Any future layer reuses it (see [Extension Points](extension-points.md)).
- `config.py`, `core/models.py`, `db.py`, the writer / reader / extraction / scoring / xml_format, `schema.sql`, and the four existing layer modules are **untouched**. Deleting `layers/diary/` leaves Nexus byte-identical.

For how the layer wires into orchestration and persistence, see [Persistence](persistence.md) and [Extension Points](extension-points.md).

## End-to-end example (offline, a deterministic stub "model")

```python
from nexus_memory import NexusMemory

m = NexusMemory(diary=True)   # db_path defaults to "nexus_memory.db"

# Five interactions cross the N=5 cadence -> a session job is enqueued.
for i in range(5):
    m.process({"action": "ingest", "interaction": {
        "query": f"Note {i}: shipped the release and fixed the parser bug.",
        "response": "Got it, logged.",
    }})
m.wait()

# The host drains the outbox and runs each job on ITS model (here: a stub).
def fake_model(job) -> str:
    return f"[{job['kind']}] {(job['prior_summary'] or '')} + {len(job['input'])} items"

for job in m.pending_summaries():          # list of handoff job objects
    text = fake_model(job)                  # in production: a real model call
    m.submit_summary(job["job_id"], text)   # idempotent apply into L1/L2

# Inspect the pyramid: per-session diaries (L1) + the single persistent summary (L2).
print(m.inspect(type="diary")["data"])      # {"sessions": [...], "summary": {...} | None}

# assemble surfaces the current session's diary (always), up to inject_sessions
# previous ones, and the single persistent_summary (when present).
res = m.process({"action": "assemble", "query": "what did I ship?"})
print(res.get("diary"), res.get("persistent_summary"))

m.close()   # finalize() enqueues the closing session's final job for next time
```

In a real deployment the host substitutes its own model for `fake_model` — for example forwarding `job["prompt"]` as the system message and `job["prior_summary"]` plus the rendered `job["input"]` as the user message — and otherwise leaves the protocol unchanged. See [Use Cases — Hierarchical Diary](../use-cases/hierarchical-diary.md) for a runnable, model-backed walkthrough.
