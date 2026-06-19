# DiaryConfig

`DiaryConfig` is the settings object for the optional **hierarchical diary** (Layer V). It is **layer-owned** — it is *not* part of [`NexusConfig`](nexus-config.md) — and it is the single switch that activates Layer V. This page documents all seven fields, how passing it to the `NexusMemory` constructor builds the layer, and why that activation can only happen at construction time.

## At a glance

```python
from nexus_memory import NexusMemory, DiaryConfig

# Activate Layer V with defaults (N=5, diary_window=20, max_sentences=50, SECTION_SIZE=7, M=8, K=1)
memory = NexusMemory(db_path="agent.db", diary=True)

# ...or pass a DiaryConfig to tune the knobs
memory = NexusMemory(db_path="agent.db", diary=DiaryConfig(enabled=True, update_every=3))
```

The `diary=True` shorthand is equivalent to `diary=DiaryConfig(enabled=True)` — it builds the layer with the default knobs. Reach for an explicit `DiaryConfig` only when you want to change `update_every`, `diary_window`, `max_sentences`, `section_size`, `max_sections`, or `inject_days`.

`DiaryConfig` is a plain `@dataclass` defined in [`src/nexus_memory/layers/diary/config.py`](../../src/nexus_memory/layers/diary/config.py). It lives entirely inside the `layers/diary/` package: deleting that folder removes the type and leaves the rest of Nexus byte-for-byte identical. The default `DiaryConfig()` has `enabled=False`, so simply importing or constructing the dataclass does nothing on its own — the layer is built only when `enabled is True`.

## Fields

| Field | Type | Default | Symbol | Meaning |
| :-- | :-- | :-- | :-- | :-- |
| `enabled` | `bool` | `False` | — | Master switch. When `False`, the layer is **never built**: no diary tables, no context provider, no `pending_summaries` / `submit_summary` routing. |
| `update_every` | `int` | `5` | `N` | Interactions between rolling **daily** summary jobs (L1). Every `N`-th ingested interaction enqueues one daily job whose `prior_summary` is the day's current text. |
| `diary_window` | `int` | `20` | — | **Turns** (1 turn = a user message + the assistant reply = 2 rows) re-sent in each rolling daily job. The job carries a rolling, **overlapping** window — at least the last `diary_window` turns of the day (≈ `diary_window * 2 = 40` rows) for reconciliation, and never drops anything ingested since the last applied drain (completeness). Distinct from `NexusConfig.history_max_turns`, which caps the chat-history accessor. |
| `max_sentences` | `int` | `50` | — | Upper bound of the daily entry's `2-max_sentences` sentence range, formatted into `DAILY_PROMPT` at enqueue time. The floor is always 2; the entry is sized to how much actually happened. |
| `section_size` | `int` | `7` | `SECTION_SIZE` | Number of finalized daily diaries folded into one **persistent section** (L2). When a section accumulates `SECTION_SIZE` days it freezes and a fresh section opens. |
| `max_sections` | `int` | `8` | `M` | Ring capacity for persistent sections. When all `M` slots are full, the **oldest section (smallest `seq`) is overwritten** — deliberate, bounded deep forgetting (≈ `M * SECTION_SIZE` = 56 days). |
| `inject_days` | `int` | `1` | `K` | How many finalized daily diaries are injected into `<memory_context>` as `<diary>` sections. With the default `K=1`, exactly the previous finalized day is injected (today is already covered by `<recent_dialogue>`). `K=0` injects nothing. |

These defaults — `N=5`, `diary_window=20`, `max_sentences=50`, `SECTION_SIZE=7`, `M=8`, `K=1` — are the canonical parameters of the diary's time-pyramid. See [Diary Layer architecture](../architecture/diary-layer.md) for how `N`, `SECTION_SIZE`, `M`, and `K` drive the L0→L1→L2 cadence, and [Tuning](tuning.md) for guidance on changing them.

`DiaryConfig.__post_init__` validates the knobs (regardless of `enabled`): `update_every`, `diary_window`, `section_size`, `max_sections` must be `>= 1`; `max_sentences >= 2`; `inject_days >= 0`. An out-of-range value raises `ValueError`.

> **Migration (0.3.5):** the default cadence changed from `update_every=3` to `update_every=5`. To keep the prior behavior, pin `diary=DiaryConfig(enabled=True, update_every=3)`.

### How the fields map to the time-pyramid

```
        granularity ▲                                          coverage ▼
  L0  episodic_turns       raw user/assistant turns            (every ingest)
  L1  diary_days           1 rolling summary per DAY           (updated every update_every interactions)
  L2  persistent_sections  1 summary per section_size days     (ring of max_sections slots)
```

- `update_every` (`N`) controls the **cadence** of L1 daily jobs.
- `section_size` (`SECTION_SIZE`) controls how many finalized L1 days fold into one L2 section.
- `max_sections` (`M`) bounds L2 forever via a ring buffer.
- `inject_days` (`K`) controls how much of L1 is surfaced into the assembled context.

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

- The `DiaryStore` is constructed, which creates its three tables (`diary_days`, `persistent_sections`, `summarization_jobs`) with `CREATE TABLE IF NOT EXISTS`.
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

Once active, the diary participates in `ingest` (scheduling summary jobs), `assemble` (injecting context), and `close` (finalizing the current day). It never calls an LLM itself — the host drains jobs via `pending_summaries()` and answers them via `submit_summary()`. See the [Diary Layer architecture](../architecture/diary-layer.md) and the [Hierarchical Diary use case](../use-cases/hierarchical-diary.md) for the full handoff protocol.

## Why activation is construction-time only

Layer V can be turned on **only** by the constructor — there is no runtime "enable the diary" call. This is a deliberate consequence of how activation works:

- **Activation creates schema.** Building the layer constructs the `DiaryStore`, which creates the three diary tables on the open SQLite connection. The connection and schema are established during construction (alongside the working/episodic/procedural layers); the diary tables are created at the same point, guarded by the `diary` flag.
- **Wiring is assembled once.** The consolidator chain, the `context_providers` list, and the action routing for `pending_summaries` / `submit_summary` are all built when the orchestrator is assembled. There is no public seam to splice a new layer into an already-running `NexusMemory`.
- **Off means truly absent.** Because the entire subsystem is gated at build time, an off diary is not merely dormant — its tables, provider, and actions never exist. This is what keeps the v2 behavior byte-for-byte identical and the prior test suite green when the diary is unused.

To change whether the diary is active, construct a new `NexusMemory` with (or without) `diary=True`. The diary's data is durable: jobs, `diary_days`, and `persistent_sections` survive a reopen on the same `db_path` (the `IF NOT EXISTS` DDL finds the existing rows), so re-enabling on an existing database resumes exactly where it left off.

> The tuning fields (`update_every`, `diary_window`, `max_sentences`, `section_size`, `max_sections`, `inject_days`) are likewise read when the layer is built. To change cadence, window size, or ring size on an existing database, pass the new values at construction time.

## Related pages

- [Diary Layer architecture](../architecture/diary-layer.md) — the time-pyramid, the outbox handoff, and the trigger state machine.
- [Hierarchical Diary use case](../use-cases/hierarchical-diary.md) — an end-to-end walkthrough of draining the outbox against a real model.
- [NexusConfig](nexus-config.md) — the core configuration object (the diary config is intentionally separate).
- [Tuning](tuning.md) — choosing values for `N`, `SECTION_SIZE`, `M`, and `K`.
- [API reference](../usage/api-reference.md) — the `process` actions and convenience wrappers, including `pending_summaries` and `submit_summary`.
- Source: [`src/nexus_memory/layers/diary/config.py`](../../src/nexus_memory/layers/diary/config.py).
