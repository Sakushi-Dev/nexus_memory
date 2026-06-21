"""The shared, layer-agnostic auxiliary-job bus (``AuxBus``).

This package lifts the diary's job-outbox storage + dispatch out of the diary
layer into a NEW, shared, layer-agnostic core component. The bus owns the single
outbox table (still named ``summarization_jobs`` for zero-DDL backward compat)
and a :class:`JobHandler` registry; each layer registers its handler(s) and the
bus routes a submitted result to the handler that owns the job's ``kind``.

The module is fully offline and deterministic; nothing under ``core/auxbus/`` ever
imports or calls any network/LLM SDK.
"""

from __future__ import annotations

from .bus import AuxBus
from .handler import JobHandler

__all__ = ["AuxBus", "JobHandler"]
