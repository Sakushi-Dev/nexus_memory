"""Pydantic request models for the diary layer's two ``process()`` actions.

These live HERE, in the diary layer, not in ``core/models.py`` — the orchestrator
validates them via this module *before* delegating to ``core.models.parse_request``
when the layer is active. When the layer is off, the actions are unknown and the
normal validation error is returned.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PendingSummariesRequest(BaseModel):
    """Request for the ``pending_summaries`` action (drain the outbox)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["pending_summaries"]
    limit: int | None = None


class SubmitSummaryRequest(BaseModel):
    """Request for the ``submit_summary`` action (hand model output back)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["submit_summary"]
    job_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
