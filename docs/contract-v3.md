# Nexus Memory v3 — Hierarchical Diary via Handoff Outbox (PLAN / BINDING CONTRACT)

Status: **PLAN — not yet implemented.** This document specifies an optional, provider-
agnostic diary subsystem that maintains an LLM-written, hierarchical, bounded long-term
narrative of the conversation. It extends `CONTRACT-v2-multilayer.md`; everything here is
**additive and fully backward compatible** (the 141 existing tests MUST stay green; when the
feature is off, nothing changes).

---

## 0. Goal, activation, ground rules

Give Nexus an optional **diary** that compresses the raw dialogue into a *time-pyramid* and
keeps it bounded forever — without the module ever calling an LLM itself.

- **Provider-agnostic by construction (handoff outbox).** Nexus NEVER imports or calls any
  LLM SDK. When a summary is due it **enqueues a job** (`prompt` + `context`) into an
  **outbox**. The host drains the outbox, runs the job on *any* model it likes, and hands the
  text back. Nexus owns the *prompt*; the host owns the *model*. This works synchronously,
  asynchronously, across a queue, or with a human in the loop, and keeps the module **fully
  offline-testable** (a job is just data).
- **Separated from `ingest`.** Summarization is decoupled from the write path: `ingest` only
  *schedules* jobs (cheap, non-blocking); the actual LLM work happens out-of-band whenever the
  host drains. Async by design.
- **A self-contained layer (Layer V).** The whole subsystem lives in **`layers/diary/`** and
  owns *everything* it needs — its tables, scheduler, jobs, prompts, **its own `DiaryConfig`**,
  and **its own request models**. It plugs into the existing system through the extension
  points that already exist (see §8): the writer's `consolidators` list (ingest), a single
  generic `context_providers` seam (assemble), and orchestrator routing. **You could delete the
  `layers/diary/` folder and Nexus would run exactly as before.** The core (`writer`, `reader`,
  `scoring`, `extraction`, `db`, the four existing layers, `config.py`, `models.py`) is **not
  modified**.
- **Activation:** off by default. The layer is enabled by passing
  `NexusMemory(diary=DiaryConfig(enabled=True, …))`. When `diary` is omitted/`enabled=False`,
  the layer is **never constructed** — zero behavior change, no new tables, empty everything,
  the 141 existing tests stay green. (Throughout this doc, "diary layer active" ≡
  `DiaryConfig.enabled`.) Activation is **construction-time only** (no per-`ingest` toggle).
- **Env unchanged:** Python 3.13, `nexus-memory/.venv/Scripts/python.exe`, deps already
  installed (sqlite-vec, pydantic, numpy, pytest). Offline/deterministic defaults.

### Chosen parameters (this plan)
| Symbol | Meaning | Value |
| :-- | :-- | :-- |
| `N` | interactions between daily-diary updates (rolling) | **3** |
| `SECTION_SIZE` | daily diaries folded into one persistent section | **7** |
| `M` | persistent sections kept (ring; oldest overwritten) | **8** |
| `K` | finalized daily diaries injected into context | **1** (previous day only) |

---

## 1. Mental model — the time-pyramid

```
        granularity ▲                         coverage ▼
  L0  episodic_turns        raw user/assistant turns          (exists, every ingest)
  L1  diary_days            1 rolling LLM summary per DAY      (updated every N=3 interactions)
  L2  persistent_sections   1 LLM summary per 7 daily diaries  (ring of M=8 sections)
```

- **Newest** = fine detail (raw turns + today via `recent_dialogue`).
- **Yesterday** = one daily diary (`<diary>`).
- **Older** = coarse epoch sections (`<persistent_summary>`), bounded to M=8 × 7 ≈ **56 days**
  of coarse history; beyond that the **oldest section is overwritten** (deliberate deep
  forgetting — see §12 for the tradeoff).

### The handoff loop (no LLM inside Nexus)
```
ingest ──(due?)──▶ enqueue job ──▶ [ outbox table ]
                                        │  host pulls
                          pending_summaries() ──▶ host runs prompt+context on ITS model
                                        ▼
                          submit_summary(job_id, text) ──▶ Nexus persists into L1/L2
```

---

## 2. Data model (3 new SQLite tables, owned by `DiaryStore`, created IF NOT EXISTS when the diary layer is active)

```sql
-- L1: one rolling diary per day
CREATE TABLE IF NOT EXISTS diary_days (
    period            TEXT PRIMARY KEY,      -- 'YYYY-MM-DD' (UTC day, matches turn timestamps)
    summary           TEXT DEFAULT '',       -- latest narrative for the day
    covered_through   INTEGER DEFAULT 0,     -- max episodic_turns.id already folded in
    interaction_count INTEGER DEFAULT 0,     -- interactions seen this day
    finalized         INTEGER DEFAULT 0,     -- 1 once the day is closed (rollover/close)
    folded            INTEGER DEFAULT 0,     -- 1 once folded into a persistent section
    updated_at        TEXT
);

-- L2: ring of persistent sections (each = summary of up to SECTION_SIZE daily diaries)
CREATE TABLE IF NOT EXISTS persistent_sections (
    slot        INTEGER PRIMARY KEY,         -- 0 .. M-1 (physical ring slot)
    seq         INTEGER,                     -- monotonic logical order (higher = newer)
    summary     TEXT DEFAULT '',
    diary_count INTEGER DEFAULT 0,           -- daily diaries folded so far (0..SECTION_SIZE)
    first_day   TEXT,                        -- coverage range
    last_day    TEXT,
    frozen      INTEGER DEFAULT 0,           -- 1 once diary_count == SECTION_SIZE
    updated_at  TEXT
);

-- Outbox: pending/answered summarization handoff jobs
CREATE TABLE IF NOT EXISTS summarization_jobs (
    job_id          TEXT PRIMARY KEY,        -- uuid4
    kind            TEXT NOT NULL,           -- 'daily' | 'section'
    target          TEXT NOT NULL,           -- daily: the 'YYYY-MM-DD'; section: the seq as text
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'superseded'
    prompt          TEXT NOT NULL,           -- Nexus-owned instruction (host forwards verbatim)
    input_json      TEXT NOT NULL,           -- JSON: {prior_summary, items:[...]}
    advance_to      INTEGER,                 -- daily: covered_through to set on apply; section: day folded
    created_at      TEXT NOT NULL,
    answered_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON summarization_jobs(status, created_at);
```

Invariant: **at most one `pending` job per `(kind, target)`**. Enqueuing a newer job for the
same target marks the older one `superseded`.

---

## 3. The job object (handoff protocol)

### Emitted by `pending_summaries()` (host receives)
```json
{
  "job_id": "uuid",
  "kind": "daily" | "section",
  "period": "2026-06-16",            // present for daily
  "prompt": "Write a concise diary entry ...",   // forward verbatim to the model
  "prior_summary": "…or null…",       // current L1/L2 summary, for ROLLING refinement
  "input": [                          // daily: new turns; section: finalized day summaries
    {"role": "user", "content": "...", "timestamp": "..."},
    ...
  ]
}
```
The host composes its own model call however it wants, e.g. messages =
`[{system: prompt}, {user: prior_summary + "\n---\n" + render(input)}]`, and produces text.

### Submitted by the host
```json
{ "action": "submit_summary", "job_id": "uuid", "summary": "…model output…" }
```
Apply is **idempotent**: submitting a `done`/`superseded`/unknown job is a safe no-op that
returns a status note (never an error to the caller).

---

## 4. Trigger state machine (the heart)

All of this runs **inside the existing consolidation step** of `ingest` (a new
`DiaryConsolidator`, only active when the diary layer is enabled), and **inside `submit_summary`**. It only
*enqueues/dequeues jobs and updates rows* — it never calls a model.

### 4.1 On each ingested interaction (diary layer active)
Let `today = UTC date of this interaction`, `last = the max(period) in diary_days`.

1. **Day rollover:** if `last` exists and `today > last` and `diary_days[last].finalized == 0`:
   - mark `diary_days[last].finalized = 1`;
   - enqueue a **final daily job** for `last` if it has turns past `covered_through`
     (so the closing day is fully summarized). (The fold into L2 happens later — see §4.3.)
2. **Upsert** `diary_days[today]` (create with `interaction_count = 0` if missing) and
   `interaction_count += 1`.
3. **Daily cadence:** if `interaction_count % N == 0`:
   - enqueue a **daily job** for `today` (rolling): `prior_summary = diary_days[today].summary`,
     `input = episodic.turns(id > covered_through, that day)`, `advance_to = max(input.id)`.
   - supersede any previous pending daily job for `today`.

### 4.2 On `submit_summary(job_id, text)` — kind = daily
- Set `diary_days[target].summary = text`, `covered_through = job.advance_to`,
  `updated_at = now`, mark job `done`.
- **If that day is `finalized` and not yet `folded`** → trigger §4.3 (enqueue its section fold,
  respecting the one-pending-section invariant).

### 4.3 Folding a finalized day into the persistent ring (kind = section)
Maintain at most **one pending section job**. When a finalized, unfolded day `D` is ready and
no section job is pending:
- Ensure an **open** section exists (`frozen = 0`); if none, allocate one (see §4.4).
- Enqueue a **section job**: `prior_summary = open.summary`,
  `input = [ {period: D, summary: diary_days[D].summary} ]`, `advance_to = D`,
  `target = open.seq`.

### 4.4 On `submit_summary` — kind = section
- Apply to the open section: `summary = text`, `diary_count += 1`, extend `first_day/last_day`,
  mark `diary_days[D].folded = 1`, job `done`.
- **Freeze + ring:** if `diary_count == SECTION_SIZE (7)` → set `frozen = 1` and allocate a NEW
  open section:
  - `seq = max(seq)+1`;
  - pick a **free slot** (none used yet) if available, else the slot with the **smallest seq**
    (oldest) → **overwrite** it (reset summary/diary_count/days/frozen). Ring capacity = `M = 8`.
- **Drain the fold queue:** if more finalized-unfolded days remain, enqueue the next section
  job (keeps the one-pending invariant; days fold strictly in chronological order).

### 4.5 On `close()` (diary layer active)
- Mark the current day `finalized = 1` and enqueue its final daily job (if uncovered turns).
- Do **not** run anything; jobs persist in the outbox for the next session’s host to drain.
- All state (diary_days, sections, jobs) is in SQLite → survives restart untouched.

> Determinism note: the only subtle part is §4.3/4.4’s one-pending-section invariant + the
> fold queue (days awaiting folding). It guarantees daily diaries fold into sections strictly
> in order, with no mid-batch boundary races, even if the host is offline for many days.

---

## 5. Context injection (assemble) — bounded time-pyramid

When the diary layer is active, the `DiaryContextProvider` (via the `context_providers` seam) appends two **bounded** sections inside
`<memory_context>` (after `<recent_dialogue>`). Neither carries `id="..."`, so the
backward-compatible needle invariant (`<fact id="(\d+)"` ≤ top_k) is preserved.

```xml
<memory_context>
  <procedural>...</procedural>
  <semantic>... <fact id=".."/> ...</semantic>
  <recent_dialogue>... today's raw turns ...</recent_dialogue>
  <diary day="2026-06-15">...the PREVIOUS day's narrative (K=1)...</diary>
  <persistent_summary>
    <section seq="9" days="2026-05-26..2026-06-01">...</section>   <!-- newest-last -->
    <section seq="10" days="2026-06-02..2026-06-08">...</section>
  </persistent_summary>
</memory_context>
```
- `<diary>` = the latest **finalized** day strictly before today (the "Vortag"). Today is
  already covered by `<recent_dialogue>`; only one day is injected (K=1).
- `<persistent_summary>` = all live sections (≤ M, already bounded), chronological.
- Response superset (additive keys): `assemble` result gains
  `"diary": {"day","summary"} | null` and `"persistent_summary": [{seq,days,summary}, ...]`,
  and `meta` gains `"diary_chars"`, `"section_count"`. Existing keys unchanged.

---

## 6. DiaryConfig — owned by the layer (`layers/diary/config.py`); `NexusConfig` is NOT touched

```python
@dataclass
class DiaryConfig:
    enabled: bool = False           # master switch; when False the layer is never built
    update_every: int = 3           # N: interactions between rolling daily updates
    section_size: int = 7           # daily diaries per persistent section
    max_sections: int = 8           # M: ring capacity (oldest section overwritten)
    inject_days: int = 1            # K: finalized daily diaries injected into context
```
The host opts in explicitly: `NexusMemory(diary=DiaryConfig(enabled=True))`. No field is
added to `NexusConfig`.

---

## 7. Public API (process actions + convenience + models)

New `process()` actions:

| action | input | output |
| :-- | :-- | :-- |
| `pending_summaries` | `{ "limit"?: int }` | `{status, jobs:[job, ...]}` (oldest-first) |
| `submit_summary` | `{ job_id, summary }` | `{status:"success"\|"superseded"\|"not_found", applied?:"daily"\|"section"}` |

The pydantic models `PendingSummariesRequest` / `SubmitSummaryRequest` (non-empty
`job_id`/`summary` validation) live in **`layers/diary/models.py`**, not in core `models.py`.
When the diary layer is active the orchestrator recognizes these two actions and validates
them via the layer's models **before** calling `core.models.parse_request` — so
`core/models.py` and its `_ACTION_MODELS` stay **untouched**. When the layer is off, the
actions are unknown and return the normal validation error.

Convenience wrappers on `NexusMemory`:
`pending_summaries(limit=None) -> list[dict]`, `submit_summary(job_id, summary) -> dict`.
`diary` / `inspect` gain read views: `inspect(type="diary")` → `{days:[...], sections:[...]}`.

`assemble`/`process` stay the single entry point; when the diary layer is off these actions
are unknown (normal validation error) and no diary sections are added to the context.

---

## 8. Module placement — a self-contained Layer V (`layers/diary/`)

### 8.1 New, self-contained (the whole feature)
```
src/nexus_memory/layers/diary/
    __init__.py        # exports DiaryConfig, DiaryLayer (+ DiaryStore for tests)
    config.py          # DiaryConfig dataclass (§6)
    store.py           # DiaryStore: owns the 3 tables (§2); all diary SQL via db.conn + db.lock
    scheduler.py       # DiaryScheduler: the state machine (§4) — enqueue/apply, fold/freeze/ring
    consolidator.py    # DiaryConsolidator(Consolidator): on each ingest, scheduler.on_interaction(...)
    provider.py        # DiaryContextProvider: renders <diary>/<persistent_summary> + response keys
    models.py          # PendingSummariesRequest / SubmitSummaryRequest
    prompts.py         # DAILY_PROMPT / SECTION_PROMPT (§9)
    layer.py           # DiaryLayer: builds store+scheduler; exposes consolidator, provider, router
tests/test_diary_outbox.py
```
`DiaryStore` creates its 3 tables (`IF NOT EXISTS`) on construction — only ever constructed
when `DiaryConfig.enabled` — via `db.conn` under `with db.lock:` (same pattern as the other
layer stores). Nothing is created when the layer is off.

### 8.2 Extension points it plugs into (already exist, except one tiny generic seam)
| Hook | Mechanism | Existing? |
| :-- | :-- | :-- |
| **ingest** | append `DiaryConsolidator` to `MemoryWriter(consolidators=[…])` | ✅ already built for this |
| **assemble** | register `DiaryContextProvider` on a generic `context_providers` list the `ContextAssembler` iterates | ➕ one-time generic seam (then any future layer reuses it) |
| **actions** | orchestrator validates+routes the 2 diary actions via the layer's models *before* `parse_request` | wiring only |
| **close** | orchestrator calls `diary.finalize()` in `close()` | wiring only |

### 8.3 File impact — what is touched vs untouched
| **Untouched (core behavior)** | **Minimal wiring (guarded by `DiaryConfig.enabled`)** |
| :-- | :-- |
| `writer.py`, `reader.py`, `scoring.py`, `extraction.py`, `xml_format.py`, `db.py` | `core/orchestrator.py` — *iff* `diary` passed: build `DiaryLayer`, append its consolidator, register its provider, route its 2 actions, `finalize()` on close |
| the 4 existing layer modules, `consolidation.py` (ABC reused, not edited) | `core/context.py` — add a generic `context_providers: list` param the assembler iterates after its 3 built-in sections (≈10 lines, diary-agnostic, future-proof) |
| **`config.py`**, **`core/models.py`** (diary brings its own config + models) | — |

Net: the only existing files that change are `orchestrator.py` (flag-guarded wiring) and a
single generic ~10-line seam in `context.py`. With the layer off, both are no-ops.

---

## 9. Prompts (Nexus-owned, in diary.py)

Two default templates shipped as constants (the host forwards them verbatim; later
configurable):
- **DAILY_PROMPT:** "You maintain a concise third-person diary of a user's day. Given the
  prior entry and the new turns, produce an updated 2–5 sentence entry. Keep durable facts,
  decisions, mood, and open threads; drop pleasantries."
- **SECTION_PROMPT:** "You maintain a rolling multi-day summary. Given the prior section
  summary and a new day's diary, integrate it into a single coherent paragraph that preserves
  the throughline across the period."

(Job carries `prompt` + `prior_summary` + `input`; the host decides how to message-format.)

---

## 10. Offline / testing strategy

No LLM in tests: drive the outbox manually with deterministic text.

Required tests (`tests/test_diary_outbox.py`, all via `.venv` pytest, suite stays green):
1. **off-by-default:** no `DiaryConfig` passed (layer never built) → no jobs, no new context
   sections, no new tables created; existing behavior byte-for-byte identical.
2. **daily cadence:** after `N=3` interactions a `daily` job appears with correct
   `prior_summary` (empty first), `input` (the 6 turns), `advance_to`.
3. **apply daily:** `submit_summary` sets `diary_days` summary + `covered_through`; a 2nd
   N-tick produces a rolling job whose `prior_summary` == the stored summary.
4. **supersede:** two N-ticks before a submit → first daily job `superseded`, one `pending`.
5. **rollover + fold:** simulate turns on day D then day D+1 → D finalized; after D's daily
   submit, a `section` job appears; submit → open section `diary_count == 1`, `D.folded == 1`.
6. **section freeze:** fold 7 days → section `frozen`, a fresh open section allocated.
7. **ring overwrite:** drive `> M*SECTION_SIZE` days → only `M=8` sections exist; oldest
   `seq` overwritten; coverage windows correct.
8. **context injection:** with a finalized previous day + sections, `assemble` emits
   `<diary day="...">` (K=1, previous day only) and `<persistent_summary>`; **no** `id="..."`
   inside them; needle invariant intact.
9. **persistence:** jobs + diary_days + sections survive a fresh `NexusDB` on the same path.
10. **idempotent submit:** re-submitting a `done` job is a safe no-op.

---

## 11. Milestones (build order)

- **MS8.1 Layer skeleton:** `layers/diary/` package — `DiaryConfig`, `DiaryStore` (3 tables +
  read methods), `models.py`, `prompts.py`. No edits to `config.py`/`core models.py`. Smoke +
  existing **141 green** (layer not yet wired).
- **MS8.2 Scheduler:** `DiaryScheduler.on_interaction` + daily job enqueue/cadence/supersede;
  `submit_summary` daily apply. Tests 2–4.
- **MS8.3 Persistent ring:** finalize/fold state machine, section freeze + ring overwrite,
  fold queue. Tests 5–7.
- **MS8.4 Generic seam + wiring:** add the diary-agnostic `context_providers` seam to
  `ContextAssembler`; build `DiaryLayer` in the orchestrator *iff* `diary` is passed (append
  consolidator, register provider, route the 2 actions via the layer's models, `finalize()` on
  `close()`); convenience wrappers `pending_summaries`/`submit_summary`. Tests 1, 9, 10.
- **MS8.5 Context provider:** `DiaryContextProvider` renders `<diary>`/`<persistent_summary>`
  (K=1 previous day + sections) + assemble response superset. Test 8.
- **MS8.6 Demo + docs:** sync copy; chat TUI `/diary` shows live diary + sections; example host
  drain-loop (offline Mock + optional OpenRouter); `docs/ms8_diary.md` + README; the
  `layers/diary/` folder is fully removable with zero effect when not wired.

---

## 12. Risks / open questions / tradeoffs

- **Deep forgetting:** overwriting the oldest section drops history beyond ≈ M×7 = 56 days.
  Deliberate & bounded. *Optional later:* a single "lifetime" summary fed by sections before
  they’re overwritten (one extra job kind). Not in this plan.
- **Cost/latency:** a daily job every N=3 interactions can mean frequent LLM calls. The outbox
  lets the host **batch/skip/idle-run**; rolling keeps each call small. Mock host = free.
- **Stale outbox:** if the host never drains, diary/sections simply lag (memory still fully
  works; raw turns + semantic + procedural are unaffected). No data loss.
- **Clock/timezone:** days are UTC (consistent with stored timestamps). A late-night session
  crossing UTC midnight splits across two days — acceptable; matches the rest of the module.
- **Decided — activation is construction-time only.** `DiaryConfig(enabled=True)` is passed
  once to `NexusMemory(diary=…)`; there is no per-`ingest` toggle. Rationale: a stable
  interaction counter and a single, unambiguous on/off state are less error-prone, and there is
  no use case for flipping the diary mid-stream. Changing the mode means constructing a new
  `NexusMemory` (or reopening).

---
*Plan spec for Nexus Memory v3 — to be implemented after review. Parameters: N=3, SECTION=7,
M=8, K=1, handoff = pure outbox, separated from ingest (async).*
