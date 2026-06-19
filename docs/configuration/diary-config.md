# DiaryConfig

`DiaryConfig` is the settings object for the optional **hierarchical diary** (Layer V). It is **layer-owned** — it is *not* part of [`NexusConfig`](nexus-config.md) — and it is the single switch that activates Layer V. This page documents all seven fields, how passing it to the `NexusMemory` constructor builds the layer, and why that activation can only happen at construction time.

## At a glance

```python
from nexus_memory import NexusMemory, DiaryConfig

# Activate Layer V with defaults (N=5, diary_window=20, max_sentences=50,
# sessions_per_summary=6, inject_sessions=1, summary_max_sentences=300)
memory = NexusMemory(db_path="agent.db", diary=True)

# ...or pass a DiaryConfig to tune the knobs
memory = NexusMemory(db_path="agent.db", diary=DiaryConfig(enabled=True, update_every=3))
```

The `diary=True` shorthand is equivalent to `diary=DiaryConfig(enabled=True)` — it builds the layer with the default knobs. Reach for an explicit `DiaryConfig` only when you want to change `update_every`, `diary_window`, `max_sentences`, `sessions_per_summary`, `inject_sessions`, or `summary_max_sentences`.

`DiaryConfig` is a plain `@dataclass` defined in [`src/nexus_memory/layers/diary/config.py`](../../src/nexus_memory/layers/diary/config.py). It lives entirely inside the `layers/diary/` package: deleting that folder removes the type and leaves the rest of Nexus byte-for-byte identical. The default `DiaryConfig()` has `enabled=False`, so simply importing or constructing the dataclass does nothing on its own — the layer is built only when `enabled is True`.

## Fields

| Field | Type | Default | Symbol | Meaning |
| :-- | :-- | :-- | :-- | :-- |
| `enabled` | `bool` | `False` | — | Master switch. When `False`, the layer is **never built**: no diary tables, no context provider, no `pending_summaries` / `submit_summary` routing. |
| `update_every` | `int` | `5` | `N` | Interactions between rolling **session** summary jobs (L1). Every `N`-th ingested interaction enqueues one session job whose `prior_summary` is the session's current text. |
| `diary_window` | `int` | `20` | — | **Turns** (1 turn = a user message + the assistant reply = 2 rows) re-sent in each rolling session job. The job carries a rolling, **overlapping** window — at least the last `diary_window` turns of the session (≈ `diary_window * 2 = 40` rows) for reconciliation, and never drops anything ingested since the last applied drain (completeness). The window is now **session-bound** (filtered by `session_id`), not day-bound. Distinct from `NexusConfig.history_max_turns`, which caps the chat-history accessor. |
| `max_sentences` | `int` | `50` | — | Upper bound of the session entry's `2-max_sentences` sentence range, formatted into `SESSION_PROMPT` at enqueue time. The floor is always 2; the entry is sized to how much actually happened. |
| `sessions_per_summary` | `int` | `6` | — | Number of finalized session diaries folded into the **single growing persistent summary** (L2) per fold. When `sessions_per_summary` finalized-unfolded sessions accumulate, one `summary` job is enqueued; applying it **extends the same** `<persistent_summary>` row (no ring, no freeze). |
| `inject_sessions` | `int` | `1` | `K` | How many **additional previous** finalized session diaries are injected into `<memory_context>` as `<diary>` sections. The **current** session diary is **always** injected; with the default `K=1`, exactly the previous finalized session is injected as well. `K=0` injects only the current session. Range `0 ≤ K ≤ 6`. |
| `summary_max_sentences` | `int` | `300` | — | Upper bound (cap) of the single growing persistent summary, formatted into `SUMMARY_PROMPT` at enqueue time. The floor is always 2. |

These defaults — `N=5`, `diary_window=20`, `max_sentences=50`, `sessions_per_summary=6`, `inject_sessions=1`, `summary_max_sentences=300` — are the canonical parameters of the diary's session pyramid. See [Diary Layer architecture](../architecture/diary-layer.md) for how they drive the L0→L1→L2 cadence, and [Tuning](tuning.md) for guidance on changing them.

`DiaryConfig.__post_init__` validates the knobs (regardless of `enabled`): `update_every`, `diary_window`, `sessions_per_summary` must be `>= 1`; `max_sentences >= 2`; `summary_max_sentences >= 2`; and `inject_sessions` must be in `0..6`. An out-of-range value raises `ValueError`.

> **Migration (0.4.0, breaking for the diary only):** Layer V moved from **day**-logic to **session**-logic. The old diary tables (`diary_days`, `persistent_sections`) are not migrated — an existing diary user restarts the diary history; the new tables (`diary_sessions`, `persistent_summary`) are created fresh. The `section_size`/`max_sections`/`inject_days` knobs were replaced by `sessions_per_summary`/`inject_sessions`/`summary_max_sentences`. (The earlier 0.3.5 change of the default cadence from `update_every=3` to `update_every=5` still applies; pin `update_every=3` to keep the prior cadence.)

### How the fields map to the session pyramid

```
        granularity ▲                                          coverage ▼
  L0  episodic_turns       raw user/assistant turns            (every ingest)
  L1  diary_sessions       1 rolling summary per SESSION       (updated every update_every interactions)
  L2  persistent_summary   1 single growing summary            (extended every sessions_per_summary folds)
```

- `update_every` (`N`) controls the **cadence** of L1 session jobs.
- `sessions_per_summary` controls how many finalized L1 sessions fold into the single L2 summary per extension.
- `summary_max_sentences` caps the single growing L2 summary.
- `inject_sessions` (`K`) controls how many **additional previous** finalized session diaries are surfaced (on top of the always-injected current session).

## Activating Layer V

Layer V is activated by passing `diary=True` (or a `DiaryConfig` with `enabled=True`) to the `NexusMemory` constructor:

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

The `diary` kwarg is `None` by default. A `bool` is a shorthand — `True` is normalized to `DiaryConfig(enabled=True)`, `False` to `None`. The layer is built **only** when the resolved config is not `None` and its `enabled` is `True`. Concretely, opting in triggers all diary wiring inside the orchestrator:

- The `DiaryStore` is constructed, which creates its three tables (`diary_sessions`, `persistent_summary`, `summarization_jobs`) with `CREATE TABLE IF NOT EXISTS`.
- The diary's consolidator is appended to the `ingest` consolidation step.
- The `DiaryContextProvider` is registered on the generic `context_providers` seam, so `assemble` emits `<diary>` and `<persistent_summary>` sections.
- The two diary actions, `pending_summaries` and `submit_summary`, are routed.

When the layer is **off** (no `diary`, `diary=False`, or `enabled=False`), none of this happens: `self._diary is None`, the three tables are never created, the two actions are unknown (a normal validation error), and the convenience wrappers return `{"status": "error", "error": "diary layer not enabled"}`.

```python
# Off (default): the diary is invisible
m = NexusMemory(db_path="agent.db")
m.pending_summaries()                       # {"status": "error", "error": "diary layer not enabled"}

# On: the full handoff protocol is available
m = NexusMemory(db_path="agent.db", diary=True)
jobs = m.pending_summaries()                # list[dict] of handoff job objects
```

Once active, the diary participates in `ingest` (scheduling summary jobs), `assemble` (injecting context), and `close` (finalizing the current session). It never calls an LLM itself — the host drains jobs via `pending_summaries()` and answers them via `submit_summary()`. See the [Diary Layer architecture](../architecture/diary-layer.md) and the [Hierarchical Diary use case](../use-cases/hierarchical-diary.md) for the full handoff protocol.

## Why activation is construction-time only

Layer V can be turned on **only** by the constructor — there is no runtime "enable the diary" call. This is a deliberate consequence of how activation works:

- **Activation creates schema.** Building the layer constructs the `DiaryStore`, which creates the three diary tables on the open SQLite connection. The connection and schema are established during construction (alongside the working/episodic/procedural layers); the diary tables are created at the same point, guarded by the `diary` flag.
- **Wiring is assembled once.** The consolidator chain, the `context_providers` list, and the action routing for `pending_summaries` / `submit_summary` are all built when the orchestrator is assembled. There is no public seam to splice a new layer into an already-running `NexusMemory`.
- **Off means truly absent.** Because the entire subsystem is gated at build time, an off diary is not merely dormant — its tables, provider, and actions never exist. This is what keeps the legacy behavior byte-for-byte identical and the prior test suite green when the diary is unused.

To change whether the diary is active, construct a new `NexusMemory` with (or without) `diary=True`. The diary's data is durable: jobs, `diary_sessions`, and `persistent_summary` survive a reopen on the same `db_path` (the `IF NOT EXISTS` DDL finds the existing rows), so re-enabling on an existing database resumes exactly where it left off.

> The tuning fields (`update_every`, `diary_window`, `max_sentences`, `sessions_per_summary`, `inject_sessions`, `summary_max_sentences`) are likewise read when the layer is built. To change cadence, window size, fold size, injection depth, or the summary cap on an existing database, pass the new values at construction time.

## Related pages

- [Diary Layer architecture](../architecture/diary-layer.md) — the session pyramid, the outbox handoff, and the trigger state machine.
- [Hierarchical Diary use case](../use-cases/hierarchical-diary.md) — an end-to-end walkthrough of draining the outbox against a real model.
- [NexusConfig](nexus-config.md) — the core configuration object (the diary config is intentionally separate).
- [Tuning](tuning.md) — choosing values for `update_every`, `sessions_per_summary`, `inject_sessions`, and `summary_max_sentences`.
- [API reference](../usage/api-reference.md) — the `process` actions and convenience wrappers, including `pending_summaries` and `submit_summary`.
- Source: [`src/nexus_memory/layers/diary/config.py`](../../src/nexus_memory/layers/diary/config.py).
