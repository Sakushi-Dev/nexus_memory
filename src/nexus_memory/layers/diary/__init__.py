"""Layer V — Diary (hierarchical, provider-agnostic narrative via a handoff outbox).

This is a fully self-contained, optional layer. It owns its own configuration
(:class:`~nexus_memory.layers.diary.config.DiaryConfig`), its own request models,
its three SQLite tables (created on construction by :class:`DiaryStore`), its
scheduler/state-machine, and its context provider. Deleting this folder leaves
the rest of Nexus working exactly as before.

The layer NEVER imports or calls any LLM SDK: when a summary is due it enqueues a
job into the outbox; the host drains it, runs the prompt on any model, and hands
the text back. See ``CONTRACT-v3-diary-outbox.md`` for the full binding spec.

"""

from __future__ import annotations

from .config import DiaryConfig
from .layer import DiaryLayer
from .scheduler import DiaryScheduler
from .store import DiaryStore

__all__ = ["DiaryConfig", "DiaryLayer", "DiaryStore", "DiaryScheduler"]
