# MS8 — Layer V: The Hierarchical Diary (provider-agnostic, via a handoff outbox)

Nexus Memory v3 adds an **optional fifth layer**: a self-managing, bounded
**diary** that compresses the raw dialogue into a *time-pyramid* of narrative
summaries and keeps it bounded forever — **without the module ever calling an
LLM itself**. The full binding spec is
[`CONTRACT-v3-diary-outbox.md`](../CONTRACT-v3-diary-outbox.md); this doc is the
implementation walkthrough in the style of
[`ms7_multilayer.md`](ms7_multilayer.md).

Everything is **additive and off by default**. With no `DiaryConfig` passed, the
layer is *never constructed*: no new tables, no new context, no new actions —
the v2 behavior is byte-for-byte identical and the prior 141 tests stay green.
The whole subsystem lives in `src/nexus_memory/layers/diary/`; **delete that
folder and Nexus runs exactly as before.**

## Why a *handoff outbox* (no LLM inside Nexus)

The defining design choice: Nexus owns the **prompt**, the host owns the
**model**. When a summary is due, the diary does not call any model — it
**enqueues a job** (a `prompt` + `prior_summary` + `input`) into an **outbox**
table. The host drains the outbox whenever it likes, runs the job on *any* model
(OpenRouter, a local model, a human), and hands the text back via
`submit_summary`. This keeps the module:

- **provider-agnostic** — it never imports an LLM SDK;
- **fully offline-testable** — a job is just data, driven manually in tests;
- **async by construction** — `ingest` only *schedules* (cheap, non-blocking);
  the LLM work happens out-of-band; a stale outbox merely makes the diary lag,
  never loses data.

```
ingest ──(due?)──▶ enqueue job ──▶ [ summarization_jobs (outbox) ]
                                         │  host pulls
                       pending_summaries() ──▶ host runs prompt+context on ITS model
                                         ▼
                       submit_summary(job_id, text) ──▶ Nexus persists into L1 / L2
```

## The time-pyramid

```
        granularity ▲                          coverage ▼
  L0  episodic_turns        raw user/assistant turns          (Layer II, every ingest)
  L1  diary_days            1 rolling LLM summary per DAY      (updated every N=3 interactions)
  L2  persistent_sections   1 LLM summary per 7 daily diaries  (ring of M=8 sections ≈ 56 days)
```

| Symbol | Meaning | Value |
| :-- | :-- | :-- |
| `N` | interactions between rolling daily-diary updates | **3** |
| `SECTION_SIZE` | daily diaries folded into one persistent section | **7** |
| `M` | persistent sections kept (ring; oldest overwritten) | **8** |
| `K` | finalized daily diaries injected into context | **1** (previous day) |

- **L0 — `episodic_turns`** (already owned by Layer II): the raw turns. Newest
  detail is served by `<recent_dialogue>`.
- **L1 — `diary_days`**: one **rolling** narrative per UTC day. Every `N=3`
  interactions a daily job is enqueued whose `prior_summary` is the day's current
  text, so the summary is *refined in place* rather than rewritten from scratch.
- **L2 — `persistent_sections`**: one coarser summary per `SECTION_SIZE=7`
  finalized daily diaries, held in a **ring of `M=8` slots** (≈ 56 days). When
  the ring is full, the **oldest section is overwritten** — deliberate, bounded
  *deep forgetting*.

Beyond the ≈ 56-day window the oldest epoch is dropped. (An optional "lifetime"
roll-up before overwrite is noted as future work in the contract; not built.)

## The three tables (`DiaryStore`, `layers/diary/store.py`)

`DiaryStore` owns all diary SQL and creates its three tables `IF NOT EXISTS` on
construction — and it is **only ever constructed when `DiaryConfig.enabled`**, so
nothing is created when the layer is off. The DDL (full version in the contract §2):

- **`diary_days`** (L1) — PK `period` (`YYYY-MM-DD`, UTC), `summary`,
  `covered_through` (max `episodic_turns.id` already folded in),
  `interaction_count`, `finalized`, `folded`, `updated_at`.
- **`persistent_sections`** (L2 ring) — PK `slot` (`0..M-1`), monotonic `seq`
  (higher = newer), `summary`, `diary_count` (`0..SECTION_SIZE`),
  `first_day`/`last_day` coverage, `frozen`, `updated_at`.
- **`summarization_jobs`** (the outbox) — PK `job_id` (uuid4), `kind`
  (`daily`|`section`), `target`, `status` (`pending`|`done`|`superseded`),
  `prompt`, `input_json` (`{prior_summary, items:[...]}`), `advance_to`,
  timestamps. Invariant: **at most one `pending` job per `(kind, target)`**.

## The handoff protocol (`pending_summaries` / `submit_summary`)

Two new `process()` actions (validated by the layer's own
`PendingSummariesRequest` / `SubmitSummaryRequest` in `layers/diary/models.py` —
core `models.py` is untouched):

| action | input | output |
| :-- | :-- | :-- |
| `pending_summaries` | `{ "limit"?: int }` | `{status, jobs:[job, ...]}` (oldest-first) |
| `submit_summary` | `{ job_id, summary }` | `{status:"success"\|"superseded"\|"not_found", applied?:"daily"\|"section"}` |

The **job object** the host receives (`DiaryLayer._to_handoff`, `layer.py`):

```json
{
  "job_id": "uuid",
  "kind": "daily" | "section",
  "period": "2026-06-16",          // present for daily jobs
  "prompt": "Write a concise diary entry ...",   // forward verbatim to the model
  "prior_summary": "…or null…",      // current L1/L2 summary — drives ROLLING refinement
  "input": [                         // daily: new turns; section: finalized day summaries
    {"role": "user", "content": "...", "timestamp": "..."}, ...
  ]
}
```

The host composes its own model call however it wants (e.g. system = `prompt`,
user = `prior_summary + render(input)`), produces text, then calls
`submit_summary(job_id, text)`. **Apply is idempotent**: submitting a
`done` / `superseded` / unknown job is a safe no-op returning a status note
(`success` no-op / `not_found`), never an error or a raise.

The two prompts Nexus ships (`layers/diary/prompts.py`) are `DAILY_PROMPT`
("…updated 2–5 sentence entry…keep durable facts, decisions, mood, open
threads…") and `SECTION_PROMPT` ("…integrate the new day into a single coherent
paragraph preserving the throughline…").

## The trigger state machine (`DiaryScheduler`, `layers/diary/scheduler.py`)

All of this runs **inside the existing consolidation step** of `ingest` (via
`DiaryConsolidator`, only active when the layer is enabled) and **inside
`submit_summary`**. It only enqueues/dequeues jobs and updates rows — **it never
calls a model.**

**On each ingested interaction** (`on_interaction(day)`): let
`today = UTC day`, `last = max(period)` in `diary_days`.

1. **Day rollover** — if `last < today` and `diary_days[last]` is not finalized:
   mark it `finalized = 1` and enqueue a **final daily job** for `last` if it has
   turns past `covered_through` (so the closing day is fully summarized).
2. **Upsert** `diary_days[today]` and `interaction_count += 1`.
3. **Daily cadence** — if `interaction_count % N == 0`: enqueue a **rolling daily
   job** for `today` (`prior_summary = today.summary`, `input =` turns with
   `id > covered_through` that day, `advance_to = max(input.id)`) and
   **supersede** any earlier pending daily job for `today`.

**On `submit_summary` — `kind = daily`**: set `summary`, `covered_through =
advance_to`, mark `done`. If that day is **finalized and not yet folded**, trigger
the section fold (respecting the one-pending-section invariant).

**Folding a finalized day into the ring — `kind = section`**: keep at most **one
pending section job**. Ensure an **open** section exists (allocate one if none);
enqueue a section job carrying the single day `D`
(`input = [{period: D, summary: …}]`, `target = open.seq`).

**On `submit_summary` — `kind = section`**: apply to the open section
(`summary = text`, `diary_count += 1`, extend `first_day`/`last_day`, mark
`D.folded = 1`). If `diary_count == SECTION_SIZE` → **freeze** it and allocate a
new open section (`seq = max(seq)+1`; reuse a free slot, else overwrite the slot
with the **smallest seq** — the ring). Then **drain the fold queue**: if more
finalized-unfolded days remain, enqueue the next section job. This *one-pending
+ chronological fold queue* invariant guarantees daily diaries fold into sections
strictly in order, with no mid-batch races, even if the host is offline for many
days.

**On `finalize()`** (called from `NexusMemory.close()`): mark the current day
`finalized` and enqueue its final daily job if it has uncovered turns. Nothing is
run — jobs persist in SQLite for the next session's host to drain.

## Context injection (`DiaryContextProvider`, `layers/diary/provider.py`)

When the layer is active, the provider is registered on the generic
`context_providers` seam (see below) and appends **two bounded sections** inside
`<memory_context>`, after `<recent_dialogue>`:

```xml
<memory_context>
  <procedural>...</procedural>
  <semantic>... <fact id=".."/> ...</semantic>
  <recent_dialogue>... today's raw turns ...</recent_dialogue>
  <diary day="2026-06-15">...the PREVIOUS finalized day's narrative (K=1)...</diary>
  <persistent_summary>
    <section seq="9" days="2026-05-26..2026-06-01">...</section>   <!-- newest-last -->
    <section seq="10" days="2026-06-02..2026-06-08">...</section>
  </persistent_summary>
</memory_context>
```

- `<diary>` is the latest **finalized** day strictly **before today** (today is
  already covered by `<recent_dialogue>`); `K=1` so exactly one day is injected.
- `<persistent_summary>` lists all live sections (already `≤ M`), chronological.
- **Crucially, neither carries `id="..."`** — so the backward-compatible *needle
  invariant* (`<fact id="(\d+)"` count `≤ top_k`; only semantic facts have ids)
  is preserved.
- `assemble`'s response gains additive keys: `"diary": {"day","summary"} | None`,
  `"persistent_summary": [{seq,days,summary}, ...]`, and `meta` gains
  `"diary_chars"` / `"section_count"`. Existing keys are unchanged; when the
  layer is off these keys are absent.

## Off-by-default and fully removable

The contract's promise is that the diary is a **clean bolt-on**:

- The layer is built only when `NexusMemory(diary=DiaryConfig(enabled=True))` is
  passed. Otherwise `self._diary is None`, no diary tables are created, the two
  diary actions are unknown (normal validation error), and the convenience
  wrappers return `{"status":"error","error":"diary layer not enabled"}`.
- The **only** existing files that change are:
  - `core/orchestrator.py` — all diary wiring is guarded by the `diary` flag
    (build the layer, append its consolidator, register its provider, route the 2
    actions *before* `core.models.parse_request`, `finalize()` on close).
  - `core/context.py` — a single **generic, diary-agnostic ~10-line seam**: a
    `context_providers: list` the `ContextAssembler` iterates after its three
    built-in sections. Any future layer reuses it.
- `config.py`, `core/models.py`, `db.py`, the writer/reader/extraction/scoring/
  xml_format, `schema.sql`, and the four existing layer modules are **untouched**.
  Deleting `layers/diary/` leaves Nexus byte-identical.

## The 10 tests (`tests/test_diary_outbox.py`)

The ten cases map one-to-one to contract §10; all are offline/deterministic (the
outbox is driven manually with fixed text — no LLM is ever called):

1. **`test_off_by_default_no_diary_tables_or_jobs`** — no `DiaryConfig` →
   `_diary is None`, the 3 tables never exist, `assemble` emits no diary
   sections/keys, the 2 actions error, wrappers report not-enabled.
2. **`test_daily_cadence_enqueues_after_n_interactions`** — after `N=3`
   interactions exactly one `daily` job appears with empty `prior_summary`, the 6
   turns as `input`, and correct `advance_to`.
3. **`test_apply_daily_then_rolling_uses_prior_summary_and_new_turns`** — a
   daily submit sets `summary` + `covered_through`; the next N-tick's job carries
   the stored summary as `prior_summary` and only the **new** turns.
4. **`test_two_ticks_before_submit_supersede_leaves_one_pending`** — two N-ticks
   before a submit → the first daily job is `superseded`, exactly one `pending`.
5. **`test_rollover_finalizes_day_then_fold_enqueues_and_applies_section`** —
   turns on D then D+1 finalize D; D's daily submit yields a `section` job (one
   item = D); folding it gives `diary_count == 1`, `D.folded == 1`.
6. **`test_folding_section_size_days_freezes_section_and_allocates_fresh`** —
   folding `SECTION_SIZE=7` days freezes the section and opens a fresh one with a
   higher `seq`.
7. **`test_ring_overwrites_oldest_section_beyond_capacity`** — driving
   `> M*SECTION_SIZE` days keeps only `M=8` sections; the oldest `seq` is
   overwritten and coverage windows stay coherent.
8. **`test_context_injection_emits_diary_and_persistent_summary_no_ids`** — with
   a finalized previous day + a section, `assemble` emits `<diary day="...">`
   (K=1) and `<persistent_summary>` with **no `id="..."`** inside; the needle
   invariant holds; the additive response keys are present.
9. **`test_jobs_days_sections_survive_reopen`** — jobs + `diary_days` +
   sections survive a brand-new `NexusDB` on the same path (`CREATE TABLE IF NOT
   EXISTS` finds the existing rows).
10. **`test_resubmitting_done_job_is_safe_no_op`** — re-submitting a `done` job
    is a safe no-op (summary unchanged); an unknown job id returns `not_found` —
    never a raise.

## End-to-end example (offline, a trivial deterministic "model")

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
    text = fake_model(job)                  # in the demo: an OpenRouter call
    m.submit_summary(job["job_id"], text)   # idempotent apply into L1/L2

# Inspect the pyramid: per-day diaries (L1) + persistent sections (L2).
print(m.inspect(type="diary")["data"])      # {"days": [...], "sections": [...]}

# assemble now also surfaces the diary + persistent_summary (when present).
res = m.process({"action": "assemble", "query": "what did I ship?"})
print(res.get("diary"), res.get("persistent_summary"))

m.close()   # finalize() enqueues the closing day's final daily job for next time
```

The runnable demo wires this against a real LLM: `nexus-chat-demo/chat.py` opts
in with `--diary` (or `NEXUS_DIARY=1`), drains the outbox on the existing
OpenRouter client after each turn, and shows the pyramid with `/pyramid`. The
drain is skipped offline, so `chat.py --selftest` still passes with no network.
