# Design: Unified Aux-LLM Seam ("AuxBus")

> **Status:** Approved design, not yet implemented. Forward-looking — this
> document describes the target architecture, not the current code. Two core
> decisions are locked (see [Locked decisions](#locked-decisions)); a handful of
> details remain open (see [Open decisions](#open-decisions)).

## Goal

Give Nexus **one** always-on aux-LLM interface that handles *all* background LLM
work — the Layer V diary summaries it already does, the Layer IV procedural
directive extraction we want, and any future job (semantic dedup, fact merge) —
instead of today's diary-specific, `diary=True`-gated drain. The host wires up an
aux model **once**; everything else is internal.

### Why

The regex `MockDirectiveDetector` is structurally too weak: it cannot tell
*"reply in German"* (a reply-language instruction, host's job) from *"cite German
sources"* (a genuine standing rule). An adversarial review proved a regex guard
both **leaks** (misses phrasings) and **over-reaches** (suppresses real rules).
The field standard (Mem0, LangMem, Letta "sleep-time compute") forms behavioral
memory with an **LLM in the background**, not synchronous heuristics. Nexus's
diary already does exactly this via an outbox — so generalize that proven pattern
to every layer.

## Locked decisions

1. **PULL, not push.** Nexus stays 100 % network-free: it only *enqueues* jobs
   (a cheap local `INSERT`) and **never** holds an LLM client, stores an I/O
   callable, or spawns an aux thread. Push (a constructor `run_job` / auto-drain
   on `close()`) was rejected — it would originate the network call *inside* the
   module and break the load-bearing local-first invariant (verified: `src/
   nexus_memory` imports no `requests`/`httpx`/`openai`/`socket`), plus introduce
   writer-lock thread hazards, break `ingest_sync` test determinism, and remove
   host control over paid calls.
   - **"Always-on" is redefined structurally:** the AuxBus and its default
     handlers are constructed regardless of `diary=True/False`, and procedural
     extraction enqueues by default. The *"wire once"* ergonomic lives in the
     **host facade** (capture the model once, pass it to `drain_aux` each drain —
     exactly today's `drain_diary` pattern).
2. **Procedural-via-aux defaults ON at 0.6.0** (procedural is a default task). A
   host that never drains still gets rules via a regex bridge (observable, see
   [Offline & fallback](#offline--fallback)); a host wanting pure legacy regex
   sets `aux=False`.

## The seam

### Host-facing API (on `NexusMemory`)

Three methods + one observability call. **No new constructor I/O argument.**

```python
drain_aux(
    run_job: Callable[[dict], str] | Mapping[str, Callable[[dict], str]],
    kind: str | Iterable[str] | None = None,
    limit: int | None = None,
) -> dict
# The single drain loop. `run_job` is EITHER one callable (used for every kind)
# OR a {kind: callable} map (route procedural's strict JSON to a JSON-reliable
# model without a second outbox). `kind` filters which kinds drain (None = all).
# Per job: pick callable for job['kind'] -> call -> submit non-empty result;
# empty -> WARNING + stays pending. Returns
# {"status", "applied": int, "skipped": int, "by_kind": {...}, "parse_failures": int}.

pending_aux_jobs(kind=None, limit=None) -> list[dict]
# Uniform handoff dicts across all kinds; [] when aux disabled.

submit_aux_job(job_id: str, result: str) -> dict
# Idempotent registry dispatch under db.lock; unknown kind -> skip+WARN, NEVER raises.

inspect(type="aux") -> {
    "pending": [...], "by_kind": {...}, "oldest": ts,
    "aux_connected": bool,            # True once any aux job reached status='done'
    "procedural_via": "aux" | "regex-fallback",
    "parse_failures": int,
}
```

**Host callable contract:** `run_job(job: dict) -> str` where
`job = {job_id, kind, target, prompt, prior_summary, input, input_text}`.
`prompt` is Nexus-owned and already encodes the required output format (prose for
diary, strict JSON for procedural). `input_text` is a **mandatory** Nexus-
pre-rendered string for *every* kind, so the host stops switching on `kind` — the
host's `run_job` collapses to `lambda job: aux.complete(job['prompt'], job['input_text'])`.
Nexus never retains `run_job` between calls; passing it each drain *is* the
registration.

### Internal extension contract (the one seam for all growth)

```python
class JobHandler(ABC):
    kinds: tuple[str, ...]                 # discriminators this handler owns
    output_format: str = "text"           # "text" | "json"; routing/doc hint

    def build_input(self, ctx: dict) -> tuple[str, str | None, dict, str]:
        # -> (prompt, prior_summary, input_payload, input_text); called at enqueue

    def parse_result(self, raw: str, job: dict):
        # DEFENSIVE: never raises; malformed -> sentinel (e.g. [] = all-NOOP)

    def apply(self, parsed, job: dict) -> dict:
        # commit under db.lock; may enqueue cascade jobs; returns {"applied": kind}
```

`AuxBus` owns the `dict[str, JobHandler]` registry **and** the outbox store
methods (enqueue / pending / get / mark). `submit_aux_job` replaces the diary
scheduler's hardcoded `if kind == 'session'/'summary'` branch with
`registry.get(job['kind'])` dispatch — the orchestrator never grows a per-kind
if/elif chain. Each layer registers its handler(s) at construction.

## Job model

One outbox table, dispatch-by-kind. A job row is **exactly** today's
`summarization_jobs` columns, unchanged:
`{job_id (uuid4 PK), kind, target, status, prompt, input_json, advance_to, created_at, answered_at}`.

Lifecycle (reused verbatim from the diary, now the contract for all kinds):
`pending -> {done | superseded}`.

Invariants (already implemented for the diary, generalized to all kinds):

1. **One-pending-per-`(kind, target)`** — enqueue marks any prior pending row
   `superseded`, then inserts the new one → free burst-coalescing / backpressure.
2. **Idempotent submit under `db.lock`** — `get_job` + status-check + `apply` in
   one lock acquisition (re-entrant lock makes nested writes safe);
   `pending_aux_jobs` is a lock-free snapshot; a job superseded between snapshot
   and submit is a safe no-op.
3. **Cascade** — `apply` may enqueue follow-on jobs (e.g. diary summary refold
   after a session apply).
4. **`advance_to`** — monotonic high-water mark / generic state-commit hint
   (diary session uses it as `covered_through`; procedural leaves it `NULL`).
5. **Unknown-kind safety** — a pending job whose `kind` has no registered handler
   (e.g. a `session` job left over when a host reopens with `diary=False`) is
   **skipped with a WARNING and left pending — never a `KeyError`**.

Kinds at 0.6.0: `session`, `summary` (diary), `procedural_extract`
(`target='procedural'` singleton). Future kinds (`semantic_dedup`, `fact_merge`,
`session_title`, …) each add a `JobHandler` — zero orchestrator / table / host
change. Enqueue is always a cheap local `INSERT` (no regex, no LLM), so procedural
enqueue is actually **cheaper** than today's inline regex → lower ingest latency.

## Configuration

Aux is ON by default, configured once. New self-contained dataclass (mirrors
`DiaryConfig`; **not** folded into `NexusConfig`, to avoid config sprawl):

```python
@dataclass
class AuxConfig:
    enabled: bool = True                  # master switch: seam/handlers always present
    procedural_extraction: bool = True    # procedural rides aux by default (diary-decoupled)
    retry_on_empty: bool = False          # preserve today's skip-and-WARN behavior
    max_pending_per_kind: int | None = None  # backpressure hint; None = unbounded
```

Wiring: `NexusMemory(..., aux: AuxConfig | bool | None = None)`. **No** `run_job`
constructor param, **no** auto-drain (dropped per the local-first decision).

- `None` / `True` → `AuxConfig()` defaults (enabled, procedural via aux).
- `False` → no aux handlers; procedural falls back to the inline regex
  `MockDirectiveDetector` (byte-identical to 0.4.2, deterministic, immediate).

`diary=True` no longer gates aux — the AuxBus is constructed regardless of diary.
Diary cadence/window knobs stay in `DiaryConfig`; `procedural_max_directives`
stays in `NexusConfig`.

## Diary on the seam

The diary stops being special and becomes the first registered handler set; its
rich cadence logic is **untouched** — only its job storage + apply move onto the
bus.

1. Move all job-table SQL into the shared `AuxBus` store — including
   `pending_summary_job()` (re-expressed generically as `pending_job(kind, target)`)
   and `_job_row_to_dict()`, not just the four obvious CRUD methods. The diary's
   **narrative** tables (`diary_sessions`, `persistent_summary`) and their methods
   stay in `DiaryStore`.
2. `DiaryScheduler` keeps `on_interaction` / `_enqueue_session` / `_enqueue_summary`
   / `_recent_turns` / the cadence+fold state machine unchanged — it just calls
   `bus.enqueue(...)` instead of `store.enqueue_job(...)`.
3. Apply logic becomes two handlers: `DiarySessionHandler` (`set_session_summary`
   + `mark_job_done` + cascade-fold) and `DiarySummaryHandler` (`upsert_summary`
   + `mark_folded` + cascade). `parse_result` is identity (model returns prose);
   `build_input` wraps the existing prompt formatting **and** produces the
   mandatory `input_text` (relocating the demo's `_render_job_input`).
4. `DiaryContextProvider` and `inspect(type="diary")` untouched.
5. Diary registers its handlers only when `diary.enabled`; a non-diary host
   draining aux simply sees no diary kinds. Leftover diary jobs after a
   `diary=True → False` reopen hit the unknown-kind safety rule (skip + WARN).

## Procedural on the seam

A third handler, `DirectiveExtractHandler` (`kinds=('procedural_extract',)`,
`output_format='json'`, `target='procedural'` singleton), default-on, diary-decoupled.

- **Enqueue:** `ProceduralConsolidator.consolidate` (runs after episodic on the
  writer thread) enqueues `procedural_extract` with `input={query, response,
  prior_directives}` instead of calling the sync regex (except during the bridge
  phase, see below). The singleton target auto-coalesces bursts.
- **build_input:** emits the Nexus-owned **Mem0-style** prompt instructing a JSON
  array of `{directive, category, priority, op: ADD|UPDATE|DELETE|NOOP}`; passes
  `prior_directives` so the model can emit `UPDATE`/`DELETE`/`NOOP` (the dedup /
  conflict resolution the regex never could); **bakes in the explicit reply-
  language exclusion** (host owns language) → fixes the proven "reply in German"
  vs "cite German sources" leak semantically.
- **parse_result:** defensive `json.loads`; malformed / empty / non-list → `[]`
  (all-NOOP, never raises), increments `parse_failures` (surfaced in `inspect`) +
  WARNING.
- **apply (op dispatch):** `ADD`/`UPDATE` → `add_rule(..., source='aux')` (the
  existing upsert collapses both); `DELETE` → new code-level
  `deactivate_by_directive()` (`UPDATE active=0 WHERE directive=?`, no schema
  change); `NOOP` → skip; then `mark_job_done`. The `procedural_rules` schema and
  the `directives()` injection path are untouched. `source='aux'` distinguishes it
  from regex `source='auto'`.
- **`distill()`** (the third procedural-write path) stays an intentionally
  separate, user-triggered offline utility (the explicit `/distill` action), not a
  background task — optionally foldable later as a `procedural_distill` kind (open
  decision).
- The regex `MockDirectiveDetector` is **reframed as the offline fallback** (same
  class, demoted role; its reply-language guard stays as a second line of defense
  for the `aux=False` path).

## Offline & fallback

No aux model (tests / offline / `aux=False`) → fully functional and
deterministic; **no network is ever required.** Two explicit, separately-tested
regimes:

- **Regime 1 — aux DISABLED** (`aux=False` or `procedural_extraction=False`): the
  inline regex runs synchronously inside `ingest_sync`, exactly as 0.4.2; rules
  appear immediately, `source='auto'`. Permanent deterministic offline path; all
  existing unit tests run here (set `aux=AuxConfig(enabled=False)` up front).
- **Regime 2 — aux ENABLED, regex BRIDGE until first real drain:** inline regex
  runs until a `procedural_extract` job reaches `status='done'`. Disable
  criterion is a cheap `SELECT` cached in memory as a set-once-true boolean (no
  new state column, restart-safe). **No overlap window** — the first successful
  drain flips the flag and regex stops on the next consolidation. To avoid double-
  stores (regex `"Standing rule: <verbatim>"` ≠ aux clean imperative, so the
  `UNIQUE(directive)` upsert would not collapse them), the aux prompt instructs the
  model to emit `DELETE` ops for any pre-existing regex-form `"Standing rule:"`
  directives it supersedes — the aux path actively cleans up the bridge's rows.

**Loud not-connected state:** if aux is enabled but no drain ever supplies a
working model, `inspect(type="aux")` reports `aux_connected=False` /
`procedural_via='regex-fallback'` + a one-time WARNING — "always-on" stays honest:
smart with a model, dumb-but-working without, never silently no-op.

**Test determinism:** `ingest_sync` stays deterministic (no Nexus aux thread;
`wait()` stays network-free). Async-procedural tests use a deterministic mock
`run_job` drained explicitly after ingest.

## Extension recipe

Adding a future background job type — three local steps, **zero** host plumbing,
zero orchestrator dispatch, zero schema:

1. Write a Nexus-owned prompt next to the owning layer; set `output_format`.
2. Implement a `JobHandler` subclass (`build_input` / defensive `parse_result` /
   `apply`).
3. Register it in the owning layer's constructor and enqueue from the trigger
   (a consolidator tick, a counter, or an explicit user action).

The host's existing `drain_aux(run_job)` drains the new kind automatically;
`inspect(type="aux").by_kind` surfaces it for free.
**Caveat:** "zero host code" holds for *plumbing*, not output-format reliability —
a new `json` kind should set `output_format='json'`; hosts on one weak model route
it via the `{kind: run_job}` map to a JSON-reliable model.

## Migration & backward compatibility

Strict backward compat via kind-**pinned** thin facades + zero-DDL:

- `drain_diary(run_job) -> int` stays, reimplemented as
  `drain_aux(run_job, kind=('session','summary'))`. **Pinned to diary kinds —
  never `kind=None`** — so a legacy diary-shaped callable never receives a
  procedural job (it would crash on the dict `input`). This is the key correction
  from review.
- `pending_summaries()` / `submit_summary()` stay as facades over the pinned
  `('session','summary')` subset.
- `diary=True` + `DiaryConfig` untouched; the demo's existing
  `MemoryService.drain_diary(...)` keeps working unchanged.
- **Data:** zero-DDL keeps `summarization_jobs` on disk → no data migration;
  in-flight diary jobs keep flowing.
- **Intended behavioral change (0.6.0):** adopting `drain_aux` with
  `procedural_extract` makes procedural rules come from the aux LLM
  (`source='aux'`) instead of regex (`source='auto'`); regex demoted to the
  `aux=False` path + the bridge.
- **Tests** asserting inline procedural rules right after `ingest_sync` migrate to
  `aux=AuxConfig(enabled=False)` at 0.5.0 (explicit, deterministic); a new test
  pins the bridge transition `source 'auto' -> 'aux'`.
- A soft `DeprecationWarning` on `drain_diary` may point to `drain_aux` later; the
  facade is **never** removed.

## Phased rollout

| Version | Scope | DDL | Behavior change |
|---|---|---|---|
| **0.5.0** | Lift diary job-SQL → `core/aux/bus.py` + `JobHandler` ABC; convert apply into `DiarySessionHandler`/`DiarySummaryHandler`; add `drain_aux`/`pending_aux_jobs`/`submit_aux_job` with unknown-kind safety; `drain_diary` etc. become facades pinned to `('session','summary')`. Keep table name `summarization_jobs`. | none | none — gated by the full suite passing + a byte-identical diary-lifecycle regression test |
| **0.5.1** | `inspect(type="aux")`; `{kind: run_job}` map routing; offline-contract tests (aux-disabled = permanent regex; bridge transition). No constructor `run_job`, no auto-drain. | none | additive |
| **0.6.0** | `AuxConfig` (defaults on); `DirectiveExtractHandler` + Mem0 prompt (language excluded, regex-form cleanup DELETEs); `deactivate_by_directive()`; consolidator enqueues; regex demoted to fallback + bridge; pre-render `input_text` for all kinds; update demo to consume `input_text` + call `drain_aux`; adversarial tests; live-test in demo. | none | procedural rules now from aux by default |
| **0.7.0+** | *(optional, only on approval)* `idx_jobs_kind` if volume warrants; cosmetic rename `summarization_jobs → aux_jobs`; retry columns; first net-new handler kind; optionally fold `/distill` onto the bus. | flagged | — |

## Risks & mitigations

- **Unreliable JSON from the procedural model** → defensive `parse_result`
  (malformed → all-NOOP, never raises) + `parse_failures` in `inspect` + WARNING;
  job still `mark_done` so it never wedges; `aux=False` keeps deterministic regex.
- **One weak model must emit both prose and JSON** → `drain_aux` `{kind: run_job}`
  map routes procedural to a JSON-reliable model without a second outbox.
- **Legacy `drain_diary` receiving procedural jobs** → facade pinned to diary kinds.
- **Orphaned diary jobs on `diary=False` reopen** → unknown-kind skip + WARN.
- **Regex-bridge double-stores** → no overlap window (cached has-done flag) + aux
  emits `DELETE` for pre-existing regex-form directives.
- **"Always-on" silently no-ops** when enabled but never drained → `inspect`
  exposes `aux_connected` / `procedural_via` + one-time WARNING.
- **Default-on flip breaks tests** asserting `source=='auto'` right after ingest →
  migrate those to `aux=AuxConfig(enabled=False)`; add a bridge-transition test.

## Approvals needed

- **None** to ship the MVP (0.5.0–0.6.0): zero-DDL Option A keeps
  `summarization_jobs` unchanged; only new `kind` values + a code-level
  `deactivate_by_directive()` (an `UPDATE` on the existing `active` column).
- **Approval required** before any of: `idx_jobs_kind` (perf index); cosmetic
  rename to `aux_jobs`/`state_commit`; retry columns (`attempts`/`last_error`/
  `failed` status). All recommended **deferred**.

## Open decisions

- **Schema:** ship zero-DDL Option A (keep `summarization_jobs` name) — *recommended* —
  vs. schedule the cosmetic rename (needs approval, buys only cosmetics).
- **Perf index:** accept the unindexed `kind` filter for MVP (*recommended*; the
  one-pending-per-target invariant bounds the pending set) vs. approve
  `idx_jobs_kind` now.
- **Per-kind model routing:** single `run_job` **plus** the `{kind: run_job}` map
  (*recommended*) vs. single callable only.
- **`distill()`:** keep `/distill` as a separate offline utility (*recommended*)
  vs. fold it onto the bus as a `procedural_distill` kind.
- **Procedural dedup depth:** rely on `prior_directives` + `add_rule` upsert +
  aux-emitted `DELETE`s (simple v1, *recommended*) vs. add Mem0-style similarity
  dedup against existing rules before `ADD`.
- **Retry semantics:** keep silent-skip-with-WARNING + `parse_failures` (zero-DDL,
  *recommended*) vs. approval-gated `attempts`/`last_error`/`failed` columns.
