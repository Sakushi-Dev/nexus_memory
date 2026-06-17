# Layer V — The Hierarchical Diary (Outbox)

This page is a deep dive on Nexus Memory's optional fifth layer: a self-managing, bounded **diary** that compresses raw dialogue into a *time-pyramid* of narrative summaries and keeps it bounded forever — **without the module ever calling an LLM itself**. It covers the time-pyramid (L0/L1/L2), the three tables, the handoff outbox protocol, the trigger state machine, context injection, and the off-by-default / fully-removable property.

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

## The time-pyramid

The diary maintains three levels of decreasing granularity and increasing coverage:

```text
        granularity ▲                          coverage ▼
  L0  episodic_turns        raw user/assistant turns          (Layer II, every ingest)
  L1  diary_days            1 rolling summary per DAY          (updated every N=3 interactions)
  L2  persistent_sections   1 summary per 7 daily diaries      (ring of M=8 sections ≈ 56 days)
```

| Symbol | Config field ([`config.py`](../../src/nexus_memory/layers/diary/config.py)) | Meaning | Default |
| :-- | :-- | :-- | :-- |
| `N` | `update_every` | interactions between rolling daily-diary updates | **3** |
| `SECTION_SIZE` | `section_size` | daily diaries folded into one persistent section | **7** |
| `M` | `max_sections` | persistent sections kept (ring; oldest overwritten) | **8** |
| `K` | `inject_days` | finalized daily diaries injected into context | **1** (previous day) |

- **L0 — `episodic_turns`** (owned by [Layer II — Episodic](memory-layers.md)): the raw turns. Newest detail is served verbatim by `<recent_dialogue>`.
- **L1 — `diary_days`**: one **rolling** narrative per UTC day. Every `N=3` interactions a daily job is enqueued whose `prior_summary` is the day's current text, so the summary is *refined in place* rather than rewritten from scratch.
- **L2 — `persistent_sections`**: one coarser summary per `SECTION_SIZE=7` finalized daily diaries, held in a **ring of `M=8` slots** (≈ 56 days). When the ring is full, the **oldest section is overwritten** — deliberate, bounded *deep forgetting*.

Beyond the ≈ 56-day window the oldest epoch is dropped. (An optional "lifetime" roll-up before overwrite is noted as future work; not built.)

## The three tables

`DiaryStore` ([`store.py`](../../src/nexus_memory/layers/diary/store.py)) owns all diary SQL and creates its three tables `IF NOT EXISTS` on construction. It is **only ever constructed when `DiaryConfig.enabled`**, so nothing is created when the layer is off. The DDL lives in the store module (not in `schema.sql`), following the same shared-connection / shared-write-lock pattern as `EpisodicStore`.

### `diary_days` (L1)

| Column | Type | Meaning |
| :-- | :-- | :-- |
| `period` | TEXT PK | `YYYY-MM-DD` UTC day (matches turn timestamps) |
| `summary` | TEXT | latest narrative for the day |
| `covered_through` | INTEGER | max `episodic_turns.id` already folded in |
| `interaction_count` | INTEGER | interactions seen this day |
| `finalized` | INTEGER | `1` once the day is closed (rollover/close) |
| `folded` | INTEGER | `1` once folded into a persistent section |
| `updated_at` | TEXT | UTC `YYYY-MM-DD HH:MM:SS` |

### `persistent_sections` (L2 ring of `M` slots)

| Column | Type | Meaning |
| :-- | :-- | :-- |
| `slot` | INTEGER PK | physical ring slot `0 .. M-1` |
| `seq` | INTEGER | monotonic logical order (higher = newer) |
| `summary` | TEXT | the section narrative |
| `diary_count` | INTEGER | daily diaries folded so far (`0..SECTION_SIZE`) |
| `first_day` / `last_day` | TEXT | coverage range |
| `frozen` | INTEGER | `1` once `diary_count == SECTION_SIZE` |
| `updated_at` | TEXT | UTC timestamp |

### `summarization_jobs` (the outbox)

| Column | Type | Meaning |
| :-- | :-- | :-- |
| `job_id` | TEXT PK | `uuid4` |
| `kind` | TEXT | `daily` \| `section` |
| `target` | TEXT | daily: the `YYYY-MM-DD`; section: the `seq` as text |
| `status` | TEXT | `pending` \| `done` \| `superseded` (default `pending`) |
| `prompt` | TEXT | Nexus-owned instruction (host forwards verbatim) |
| `input_json` | TEXT | JSON `{prior_summary, items:[...]}` |
| `advance_to` | INTEGER | daily: `covered_through` to set on apply; section: unused |
| `created_at` / `answered_at` | TEXT | UTC timestamps |

An index `idx_jobs_status(status, created_at)` backs oldest-first draining. **Invariant: at most one `pending` job per `(kind, target)`** — `enqueue_job` first marks any existing pending job for the same `(kind, target)` as `superseded` before inserting the new one.

These tables survive a process restart: a brand-new `NexusDB` on the same path re-runs the idempotent `CREATE TABLE IF NOT EXISTS` and finds the existing rows.

## The handoff protocol

Two `process()` actions are added by the layer, validated by the layer's own request models (`PendingSummariesRequest` / `SubmitSummaryRequest` in [`models.py`](../../src/nexus_memory/layers/diary/models.py) — the core models are untouched). See also the [API Reference](../usage/api-reference.md).

| action | input | output |
| :-- | :-- | :-- |
| `pending_summaries` | `{ "limit"?: int }` | `{status, jobs:[job, ...]}` (oldest-first) |
| `submit_summary` | `{ job_id, summary }` | `{status:"success" \| "superseded" \| "not_found", applied?:"daily" \| "section"}` |

### The job object the host receives

Built by `DiaryLayer._to_handoff` ([`layer.py`](../../src/nexus_memory/layers/diary/layer.py)):

```json
{
  "job_id": "uuid",
  "kind": "daily",
  "period": "2026-06-16",
  "prompt": "You maintain a concise third-person diary of a user's day. ...",
  "prior_summary": "…the day's/section's current summary, or \"\" if none yet…",
  "input": [
    {"role": "user", "content": "...", "timestamp": "..."}
  ]
}
```

- `period` is present for `daily` jobs and `null` for `section` jobs.
- `prompt` is forwarded **verbatim** to the model.
- `prior_summary` is the current L1/L2 summary (an empty string `""` when there is none yet, never `null`) — it drives **rolling refinement** (the model edits the prior text rather than starting over).
- `input` carries new turns for `daily` jobs, or a single finalized day summary (`[{period, summary}]`) for `section` jobs.

The host composes its own model call however it wants — e.g. `system = prompt`, `user = prior_summary + render(input)` — produces text, then calls `submit_summary(job_id, text)`.

**Apply is idempotent.** Submitting a `done` / `superseded` / unknown job is a safe no-op returning a status note, never an error or a raise:

| Submitted job state | Result |
| :-- | :-- |
| `pending` daily | `{"status":"success","applied":"daily"}` |
| `pending` section | `{"status":"success","applied":"section"}` |
| already `done` | `{"status":"success", ...}` (no-op, summary unchanged) |
| already `superseded` | `{"status":"superseded", ...}` |
| unknown `job_id` | `{"status":"not_found"}` |

### The two shipped prompts

Nexus ships two prompt templates in [`prompts.py`](../../src/nexus_memory/layers/diary/prompts.py):

- **`DAILY_PROMPT`** — *"You maintain a concise third-person diary of a user's day. Given the prior entry and the new turns, produce an updated 2-5 sentence entry. Keep durable facts, decisions, mood, and open threads; drop pleasantries."*
- **`SECTION_PROMPT`** — *"You maintain a rolling multi-day summary. Given the prior section summary and a new day's diary, integrate it into a single coherent paragraph that preserves the throughline across the period."*

## The trigger state machine

`DiaryScheduler` ([`scheduler.py`](../../src/nexus_memory/layers/diary/scheduler.py)) is the heart of the layer. It runs **inside the existing consolidation step** of `ingest` (via `DiaryConsolidator`, which runs on the writer's background thread *after* the episodic consolidator, so the current interaction's turns are already in `episodic_turns`) and **inside `submit_summary`**. It only enqueues/dequeues jobs and updates rows — **it never calls a model.** The scheduler reads new turns directly from `episodic_turns` via the shared connection, never importing `EpisodicStore`.

### On each ingested interaction — `on_interaction(day)`

Let `today = UTC day` and `last = MAX(period)` in `diary_days`.

1. **Day rollover** — if `today > last` and `diary_days[last]` is not finalized: mark it `finalized = 1` and enqueue a **final daily job** for `last` (only if it has turns past `covered_through`, so the closing day is fully summarized).
2. **Upsert** `diary_days[today]` (`INSERT OR IGNORE`) and `interaction_count += 1`.
3. **Daily cadence** — if `interaction_count % N == 0`: enqueue a **rolling daily job** for `today` with `prior_summary = today.summary`, `input =` turns with `id > covered_through` that day, and `advance_to = max(input.id)`. Enqueuing **supersedes** any earlier pending daily job for `today` (the one-pending invariant), so two N-ticks before a submit leave exactly one `pending` job and one `superseded`.

A daily job is only enqueued when there are actually new turns; an empty `input` short-circuits.

### On `submit_summary` with `kind = "daily"` — `_apply_daily`

Set `summary` and `covered_through = advance_to`, mark the job `done`. If that day is **finalized and not yet folded**, trigger a section fold (respecting the one-pending-section invariant below).

### Folding a finalized day into the ring — `_enqueue_section` (`kind = "section"`)

Keep **at most one pending section job**. Take the oldest finalized-unfolded day `D` (chronological), ensure an **open** (`frozen = 0`) section exists — allocating one if none — then enqueue a section job carrying that single day: `input = [{period: D, summary: …}]`, `target = open.seq`, `prior_summary = open.summary`.

### On `submit_summary` with `kind = "section"` — `_apply_section`

Apply to the section identified by `seq`: set `summary = text`, `diary_count += 1`, extend `first_day` / `last_day`, and mark `D.folded = 1`. Then:

- If `diary_count >= SECTION_SIZE` → **freeze** the section (`frozen = 1`) and **allocate** a fresh open section. Allocation sets `seq = max(seq) + 1` and reuses a free physical slot in `[0, M)`; if all slots are taken it **overwrites the slot with the smallest `seq`** — this is the ring.
- **Drain the fold queue**: call `_enqueue_section` again so the next finalized-unfolded day folds next, strictly in order.

This *one-pending + chronological fold queue* invariant guarantees daily diaries fold into sections strictly in order, with no mid-batch races, even if the host is offline for many days. Driving more than `M * SECTION_SIZE` days keeps only `M = 8` sections, with the oldest `seq` overwritten and coverage windows staying coherent.

### On `finalize()` — `NexusMemory.close()`

Mark the current day `finalized` and enqueue its final daily job if it has uncovered turns. **Nothing is run** — jobs simply persist in SQLite for the next session's host to drain.

## Context injection

When the layer is active, `DiaryContextProvider` ([`provider.py`](../../src/nexus_memory/layers/diary/provider.py)) is registered on the generic `context_providers` seam that the `ContextAssembler` iterates after its three built-in sections (see [Retrieval & Scoring](retrieval-and-scoring.md) for assembly and the [Data Flow](../io/data-flow.md) page for the request lifecycle). It appends **two bounded fragments** inside `<memory_context>`, after `<recent_dialogue>`:

```xml
<memory_context>
  <procedural>...</procedural>
  <semantic>... <fact id=".."/> ...</semantic>
  <recent_dialogue>... today's raw turns ...</recent_dialogue>
  <diary day="2026-06-15">...the PREVIOUS finalized day's narrative (K=1)...</diary>
  <persistent_summary>
    <section seq="9" days="2026-05-26..2026-06-01">...</section>
    <section seq="10" days="2026-06-02..2026-06-08">...</section>
  </persistent_summary>
</memory_context>
```

- `<diary>` is the latest **finalized** day strictly **before today** with a non-empty summary (today is already covered by `<recent_dialogue>`); `K = inject_days = 1`, so exactly one day is injected.
- `<persistent_summary>` lists all live sections (those with `diary_count > 0` or `frozen = 1`, already `≤ M`), chronological, newest-last.
- **Crucially, neither element carries `id="..."`** — so the backward-compatible *needle invariant* (the count of `<fact id="(\d+)"` stays `≤ top_k`; only semantic facts have ids) is preserved. Text is XML-escaped via `xml.sax.saxutils.escape` / `quoteattr`, exactly like `core/context.py`.

### Additive response keys

`assemble`'s response gains these keys (and only these); existing keys are unchanged, and when the layer is off the keys are absent entirely:

| Key | Shape |
| :-- | :-- |
| `diary` | `{"day", "summary"}` or `null` |
| `persistent_summary` | `[{"seq", "days", "summary"}, ...]` |
| `meta.diary_chars` | length of the injected diary summary |
| `meta.section_count` | number of live sections |

## Off-by-default and fully removable

The diary is a **clean bolt-on**:

- The layer is built only when `NexusMemory(diary=DiaryConfig(enabled=True))` is passed. Otherwise `self._diary is None`: no diary tables are created, the two diary actions are unknown (a normal validation error), and the convenience wrappers return `{"status":"error","error":"diary layer not enabled"}`.
- The **only** existing files that change are:
  - `core/orchestrator.py` — all diary wiring is guarded by the `diary` flag (build the layer, append its consolidator, register its provider, route the two actions before `core.models.parse_request`, call `finalize()` on close).
  - `core/context.py` — a single generic, diary-agnostic ~10-line seam: a `context_providers` list the `ContextAssembler` iterates after its three built-in sections. Any future layer reuses it (see [Extension Points](extension-points.md)).
- `config.py`, `core/models.py`, `db.py`, the writer / reader / extraction / scoring / xml_format, `schema.sql`, and the four existing layer modules are **untouched**. Deleting `layers/diary/` leaves Nexus byte-identical.

For how the layer wires into orchestration and persistence, see [Persistence](persistence.md) and [Extension Points](extension-points.md).

## End-to-end example (offline, a deterministic stub "model")

```python
from nexus_memory import NexusMemory, DiaryConfig

m = NexusMemory(db_path="diary_demo.db", diary=DiaryConfig(enabled=True))

# Three interactions cross the N=3 cadence -> a daily job is enqueued.
for i in range(3):
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

# Inspect the pyramid: per-day diaries (L1) + persistent sections (L2).
print(m.inspect(type="diary")["data"])      # {"days": [...], "sections": [...]}

# assemble now also surfaces the diary + persistent_summary (when present).
res = m.process({"action": "assemble", "query": "what did I ship?"})
print(res.get("diary"), res.get("persistent_summary"))

m.close()   # finalize() enqueues the closing day's final daily job for next time
```

In a real deployment the host substitutes its own model for `fake_model` — for example forwarding `job["prompt"]` as the system message and `job["prior_summary"]` plus the rendered `job["input"]` as the user message — and otherwise leaves the protocol unchanged. See [Use Cases — Hierarchical Diary](../use-cases/hierarchical-diary.md) for a runnable, model-backed walkthrough.
