# Changelog

Release notes for the Nexus Memory module, newest first. The project follows
[semantic versioning](https://semver.org): patch releases are additive and
backward compatible, minor releases add features, major releases may break APIs.

| Version | Date | Highlights |
|---------|------|------------|
| [0.3.4](0.3.4.md) | 2026-06-19 | Optional `tiktoken` counter via `tokens(config=)`, single shared token heuristic, diary `DAILY_PROMPT` reconciliation clause, examples overhaul |
| [0.3.3](0.3.3.md) | 2026-06-19 | `NexusMemory.tokens()` section-based token accountant (system/input/output/full), OpenAI-format `basic_usage.py` |
| [0.3.2](0.3.2.md) | 2026-06-19 | `NexusMemory.history()` native message-history accessor, `drain_diary` warns on silent host failures, `pip install -e .` setup |
| [0.3.1](0.3.1.md) | 2026-06-18 | `diary=True` shorthand, `drain_diary()` helper, clone-and-embed setup, docs overhaul |
| 0.3.0 | — | Baseline (five-layer memory with the optional hierarchical diary). |
