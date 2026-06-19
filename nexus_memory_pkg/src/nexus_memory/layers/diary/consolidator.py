"""The diary's ingest hook.

:class:`DiaryConsolidator` plugs into the writer's ``consolidators`` list. It runs
on the writer's background thread AFTER the
:class:`~nexus_memory.core.consolidation.EpisodicConsolidator`, so the current
interaction's turns are already in ``episodic_turns`` by the time it advances the
diary state machine.

It reuses the :class:`~nexus_memory.core.consolidation.Consolidator` ABC (never
editing it) and never imports or calls any LLM SDK.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...core.consolidation import Consolidator

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from .scheduler import DiaryScheduler

logger = logging.getLogger(__name__)


class DiaryConsolidator(Consolidator):
    """Advance the diary state machine once per ingested interaction.

    Args:
        scheduler: The :class:`DiaryScheduler` driving the §4 state machine.
    """

    def __init__(self, scheduler: "DiaryScheduler") -> None:
        self._scheduler = scheduler

    def consolidate(
        self,
        interaction: dict,
        metadata: dict | None,
        written_ids: list[int],
    ) -> None:
        """Run one §4.1 tick (the current turns are already in episodic)."""
        self._scheduler.on_interaction()
        logger.debug("DiaryConsolidator: on_interaction advanced the diary state machine")
