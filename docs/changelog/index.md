# Changelog

Release notes for the Nexus Memory module, newest first. The project follows
[semantic versioning](https://semver.org): patch releases are additive and
backward compatible, minor releases add features, major releases may break APIs.

| Version | Date | Highlights |
|---------|------|------------|
| [0.5.1](0.5.1.md) | 2026-06-21 | Observability + routing for the AuxBus (additive): `inspect(type="aux")` snapshot (`pending` / `by_kind` / `oldest` / `aux_connected` / `kinds_registered`), and per-kind routing in `drain_aux` (a `{kind: run_job}` map with an optional `"default"`). No behavior/schema change. |
| [0.5.0](0.5.0.md) | 2026-06-21 | Structural refactor (zero behavior change): the diary's job outbox is generalized into a shared, layer-agnostic **AuxBus** + `JobHandler` registry — one drain seam for all background LLM tasks (diary today, procedural/future next). New `drain_aux` / `pending_aux_jobs` / `submit_aux_job` API; the diary now rides the bus as two handlers. Foundation for the [unified aux-LLM architecture](../design/unified-aux-bus.md). |
| [0.4.2](0.4.2.md) | 2026-06-20 | Reply language is no longer a procedural concern — Nexus stops mining/storing language directives (`"language"` category removed); the host pins the reply language out of the box. Plus a new "Multiple agents" use-case (one database per bot). |
| [0.4.1](0.4.1.md) | 2026-06-20 | Audit pass: removed inert `assemble.filters`; `ingest.priority` now an importance floor; `pin`/`update` are real `process()` actions + wrappers; semantic read cache invalidated on every mutation (fixes stale read-after-write); `forget(query=…)` relevance floor (`forget_min_similarity`, default `0.6`); namespace-aware writer dedup; read-path locking; persona-name stopword strip; dropped "graph-expanded retrieval" claims |
| [0.4.0](0.4.0.md) | 2026-06-20 | Diary rework: session-keyed instead of day-keyed, current session always injected, single growing `<persistent_summary>` (`sessions_per_summary`, `summary_max_sentences`), `inject_sessions` (`0-6`); breaking for the diary layer only (`diary_sessions` / `persistent_summary` tables recreated), non-diary hosts unaffected |
| [0.3.5](0.3.5.md) | 2026-06-19 | Diary rework: assistant first-person prose, `2-N` sentence range, rolling overlapping window (`diary_window`), `max_sentences` knob, config validation; default cadence `update_every` 3 → 5 (pin `=3` to keep) |
| [0.3.4](0.3.4.md) | 2026-06-19 | Optional `tiktoken` counter via `tokens(config=)`, single shared token heuristic, diary `DAILY_PROMPT` reconciliation clause, examples overhaul |
| [0.3.3](0.3.3.md) | 2026-06-19 | `NexusMemory.tokens()` section-based token accountant (system/input/output/full), OpenAI-format `basic_usage.py` |
| [0.3.2](0.3.2.md) | 2026-06-19 | `NexusMemory.history()` native message-history accessor, `drain_diary` warns on silent host failures, `pip install -e .` setup |
| [0.3.1](0.3.1.md) | 2026-06-18 | `diary=True` shorthand, `drain_diary()` helper, clone-and-embed setup, docs overhaul |
| 0.3.0 | — | Baseline (five-layer memory with the optional hierarchical diary). |
