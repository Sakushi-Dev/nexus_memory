"""Nexus Chat — a modular console demo for the Nexus Memory Module.

This package exists to **demonstrate that Nexus is a self-contained, drop-in
module**. The application is split into single-responsibility components, and the
*entire* coupling to ``nexus_memory`` is confined to ONE file:

    config.py    — settings: .env + CLI flags          (no Nexus, no LLM)
    llm.py       — the LLM provider (OpenRouter)        (no Nexus, no UI)
    tokens.py    — token counting for the rolling context window (tiktoken)
    memory.py    — ◀ THE BOUNDARY: the only module that imports ``nexus_memory``;
                    exposes a domain facade (``MemoryService``) — including the
                    Layer V outbox drain (Nexus enqueues the summary jobs; the host
                    just runs each one on the LLM and hands the text back)
    trace.py     — observability: captures the module's internal log events
    commands.py  — slash-command dispatch + the command-output renderables
    tui.py       — the full-screen Textual TUI (default frontend)
    app.py       — composition root + the classic line frontend (--classic)
    selftest.py  — an offline, network-free end-to-end check

Swap ``memory.py`` for a different backend and nothing else in the demo changes —
that single, narrow seam is the whole point. Each module depends only on the ones
*below* it in that list, so the dependency graph is acyclic and easy to present.
"""

__all__ = [
    "config", "llm", "tokens", "memory", "trace",
    "commands", "tui", "app", "selftest",
]
