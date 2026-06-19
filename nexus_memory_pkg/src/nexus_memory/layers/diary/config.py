"""Configuration for the diary layer (Layer V).

The diary owns its own config dataclass. Nothing here is added to
:class:`~nexus_memory.core.config.NexusConfig`; the host opts in explicitly by
passing ``NexusMemory(diary=DiaryConfig(enabled=True, ...))``. When the layer is
off (``enabled=False``, the default), the layer is never constructed and no diary
tables are created and existing behavior is unchanged.

Parameters: N=update_every=5, diary_window=20 (turns), max_sentences=50,
SECTION_SIZE=7, M=max_sections=8, K=inject_days=1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiaryConfig:
    """Settings for the optional hierarchical diary subsystem.

    Attributes:
        enabled: Master switch; when ``False`` the layer is never built.
        update_every: ``N`` — interactions between rolling daily updates.
        diary_window: Turns (user + assistant = 1 turn = 2 rows) re-sent in each
            rolling daily job. The window always carries at least the last
            ``diary_window`` turns of the day (overlap, for reconciliation) and
            never drops anything ingested since the last applied drain
            (completeness). ``20`` turns → up to 40 rows per job. Distinct from
            :attr:`~nexus_memory.core.config.NexusConfig.history_max_turns`
            (which caps the chat history accessor, a different subsystem).
        max_sentences: Upper bound of the daily entry's ``2-max_sentences``
            sentence range, formatted into ``DAILY_PROMPT`` at enqueue time. The
            floor is always 2.
        section_size: Daily diaries folded into one persistent section.
        max_sections: ``M`` — ring capacity (oldest section overwritten).
        inject_days: ``K`` — finalized daily diaries injected into context.
    """

    enabled: bool = False        # master switch; when False the layer is never built
    update_every: int = 5        # N: interactions between rolling daily updates
    diary_window: int = 20       # turns re-sent per rolling daily job (1 turn = 2 rows)
    max_sentences: int = 50      # upper bound of the entry's 2-N sentence range
    section_size: int = 7        # daily diaries per persistent section
    max_sections: int = 8        # M: ring capacity (oldest section overwritten)
    inject_days: int = 1         # K: finalized daily diaries injected into context

    def __post_init__(self) -> None:
        """Validate the diary knobs (regardless of ``enabled``).

        Mirrors the ``raise ValueError`` style of
        :meth:`nexus_memory.core.config.NexusConfig.__post_init__`. The
        ``enabled`` bool is not range-checked.
        """
        for name in ("update_every", "diary_window", "section_size", "max_sections"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"DiaryConfig.{name} must be >= 1, got {value!r}")
        if self.max_sentences < 2:
            raise ValueError(
                f"DiaryConfig.max_sentences must be >= 2, got {self.max_sentences!r}"
            )
        if self.inject_days < 0:
            raise ValueError(
                f"DiaryConfig.inject_days must be >= 0, got {self.inject_days!r}"
            )
