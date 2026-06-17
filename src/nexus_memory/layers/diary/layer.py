"""The diary layer assembly (CONTRACT-v3 §8) — the self-contained Layer V.

:class:`DiaryLayer` wires together the diary's own pieces (store, scheduler,
consolidator, context provider) and exposes the surfaces the orchestrator plugs
into:

* ``.consolidator`` — appended to the writer's ``consolidators`` list (ingest),
* ``.provider``     — registered on the assembler's ``context_providers`` seam,
* ``.config``       — the diary's own :class:`DiaryConfig`,
* ``parse_request`` / ``route`` — validate + handle the 2 diary actions,
* ``finalize`` / ``state`` / convenience wrappers.

Deleting the ``layers/diary/`` folder leaves Nexus working exactly as before; this
layer is only ever constructed when ``DiaryConfig.enabled`` is True. It never
imports or calls any LLM SDK.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .consolidator import DiaryConsolidator
from .models import PendingSummariesRequest, SubmitSummaryRequest
from .provider import DiaryContextProvider
from .scheduler import DiaryScheduler
from .store import DiaryStore

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ...core.db import NexusDB
    from ..episodic.episodic import EpisodicStore
    from .config import DiaryConfig

logger = logging.getLogger(__name__)


class DiaryLayer:
    """The self-contained diary subsystem (Layer V).

    Args:
        db: The shared :class:`NexusDB` (owns the connection + write lock).
        episodic: The :class:`EpisodicStore` (held for parity; the scheduler reads
            ``episodic_turns`` directly via the shared connection).
        diary_config: The diary's own :class:`DiaryConfig` (``enabled`` must be
            True for this layer to be constructed).
    """

    def __init__(
        self,
        db: "NexusDB",
        episodic: "EpisodicStore",
        diary_config: "DiaryConfig",
    ) -> None:
        self.db = db
        self.episodic = episodic
        self.config = diary_config

        self.store = DiaryStore(db)
        self.scheduler = DiaryScheduler(self.store, db, diary_config)
        self.consolidator = DiaryConsolidator(self.scheduler)
        self.provider = DiaryContextProvider(self.store, diary_config)
        logger.debug("DiaryLayer constructed (diary layer active).")

    # ------------------------------------------------------------------ #
    # action routing (CONTRACT-v3 §7)
    # ------------------------------------------------------------------ #
    def parse_request(self, payload: dict):
        """Validate + return a layer model for ONLY the 2 diary actions.

        Raises:
            ValueError: If the payload's ``action`` is not a diary action.
        """
        action = payload.get("action")
        if action == "pending_summaries":
            return PendingSummariesRequest(**payload)
        if action == "submit_summary":
            return SubmitSummaryRequest(**payload)
        raise ValueError(f"Not a diary action: {action!r}")

    def route(self, action: str, request) -> dict:
        """Handle a validated diary request and return the response dict."""
        if action == "pending_summaries":
            jobs = self.store.pending_jobs(request.limit)
            return {
                "status": "success",
                "jobs": [self._to_handoff(j) for j in jobs],
            }
        if action == "submit_summary":
            r = self.scheduler.submit(request.job_id, request.summary)
            return {"status": r["status"], "applied": r.get("applied")}
        raise ValueError(f"Not a diary action: {action!r}")

    @staticmethod
    def _to_handoff(job: dict) -> dict:
        """Map a stored job row to the §3 handoff job object the host receives."""
        input_obj = job.get("input_obj") or {}
        return {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "period": job["target"] if job["kind"] == "daily" else None,
            "prompt": job["prompt"],
            "prior_summary": input_obj.get("prior_summary"),
            "input": input_obj.get("items", []),
        }

    # ------------------------------------------------------------------ #
    # convenience wrappers (CONTRACT-v3 §7)
    # ------------------------------------------------------------------ #
    def pending_summaries(self, limit: int | None = None) -> list[dict]:
        """Return the handoff job objects (the host drains these)."""
        return self.route(
            "pending_summaries",
            PendingSummariesRequest(action="pending_summaries", limit=limit),
        )["jobs"]

    def submit_summary(self, job_id: str, summary: str) -> dict:
        """Hand a model output back; returns ``{status, applied}``."""
        return self.route(
            "submit_summary",
            SubmitSummaryRequest(
                action="submit_summary", job_id=job_id, summary=summary
            ),
        )

    # ------------------------------------------------------------------ #
    # lifecycle + inspect
    # ------------------------------------------------------------------ #
    def finalize(self) -> None:
        """Close the diary on ``NexusMemory.close()`` (§4.5)."""
        self.scheduler.finalize()

    def state(self) -> dict:
        """Read view for ``inspect(type="diary")`` → ``{days, sections}``."""
        return {"days": self.store.days(), "sections": self.store.sections()}
