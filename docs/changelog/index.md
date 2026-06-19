# Changelog

Release notes for the Nexus Memory module, newest first. The project follows
[semantic versioning](https://semver.org): patch releases are additive and
backward compatible, minor releases add features, major releases may break APIs.

| Version | Date | Highlights |
|---------|------|------------|
| [0.4.0](0.4.0.md) | 2026-06-20 | Diary rework: session-keyed instead of day-keyed, current session always injected, single growing `<persistent_summary>` (`sessions_per_summary`, `summary_max_sentences`), `inject_sessions` (`0-6`); breaking for the diary layer only (`diary_sessions` / `persistent_summary` tables recreated), non-diary hosts unaffected |
| [0.3.5](0.3.5.md) | 2026-06-19 | Diary rework: assistant first-person prose, `2-N` sentence range, rolling overlapping window (`diary_window`), `max_sentences` knob, config validation; default cadence `update_every` 3 → 5 (pin `=3` to keep) |
| [0.3.4](0.3.4.md) | 2026-06-19 | Optional `tiktoken` counter via `tokens(config=)`, single shared token heuristic, diary `DAILY_PROMPT` reconciliation clause, examples overhaul |
| [0.3.3](0.3.3.md) | 2026-06-19 | `NexusMemory.tokens()` section-based token accountant (system/input/output/full), OpenAI-format `basic_usage.py` |
| [0.3.2](0.3.2.md) | 2026-06-19 | `NexusMemory.history()` native message-history accessor, `drain_diary` warns on silent host failures, `pip install -e .` setup |
| [0.3.1](0.3.1.md) | 2026-06-18 | `diary=True` shorthand, `drain_diary()` helper, clone-and-embed setup, docs overhaul |
| 0.3.0 | — | Baseline (five-layer memory with the optional hierarchical diary). |
