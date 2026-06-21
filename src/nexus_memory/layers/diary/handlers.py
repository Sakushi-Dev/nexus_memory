"""The diary's :class:`JobHandler` set — the diary rides the shared aux bus.

At 0.5.0 the diary stops being special and becomes the first registered handler
set on the :class:`~nexus_memory.core.auxbus.bus.AuxBus`. Its rich cadence logic in
:class:`~nexus_memory.layers.diary.scheduler.DiaryScheduler` is untouched — only
its job storage + apply move onto the bus. These two handlers are thin adapters
that route a submitted result back into the scheduler's existing apply methods.

``parse_result`` is identity for both kinds: a diary summary/fold is plain prose
the host model returns verbatim. The module is fully offline and deterministic;
it never imports or calls any network/LLM SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.auxbus.handler import JobHandler

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from .scheduler import DiaryScheduler


# ====================================================================== #
# session — apply a rolling session summary
# ====================================================================== #
class DiarySessionHandler(JobHandler):
    """Applies a ``session`` job: a rolling per-session narrative summary."""

    kinds = ("session",)
    output_format = "text"

    def __init__(self, scheduler: "DiaryScheduler") -> None:
        self._scheduler = scheduler

    def parse_result(self, raw: str, job: dict) -> Any:
        """Identity: diary summaries are plain prose the host returns verbatim."""
        return raw

    def apply(self, parsed: Any, job: dict) -> dict:
        """Persist the session summary via the scheduler's existing apply path."""
        self._scheduler._apply_session(job, parsed)  # noqa: SLF001 - intentional
        return {"status": "success", "applied": "session"}


# ====================================================================== #
# summary — apply a fold into the single persistent summary
# ====================================================================== #
class DiarySummaryHandler(JobHandler):
    """Applies a ``summary`` job: a fold into the single persistent summary."""

    kinds = ("summary",)
    output_format = "text"

    def __init__(self, scheduler: "DiaryScheduler") -> None:
        self._scheduler = scheduler

    def parse_result(self, raw: str, job: dict) -> Any:
        """Identity: diary summaries are plain prose the host returns verbatim."""
        return raw

    def apply(self, parsed: Any, job: dict) -> dict:
        """Extend the persistent summary via the scheduler's existing apply path."""
        self._scheduler._apply_summary(job, parsed)  # noqa: SLF001 - intentional
        return {"status": "success", "applied": "summary"}
