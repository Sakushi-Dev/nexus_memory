"""Configuration for the diary layer (Layer V).

The diary owns its own config dataclass. Nothing here is added to
:class:`~nexus_memory.core.config.NexusConfig`; the host opts in explicitly by
passing ``NexusMemory(diary=DiaryConfig(enabled=True, ...))``. When the layer is
off (``enabled=False``, the default), the layer is never constructed and no diary
tables are created and existing behavior is unchanged.

Parameters: N=update_every=5, diary_window=20 (turns), max_sentences=50,
sessions_per_summary=6, inject_sessions=1, summary_max_sentences=300.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiaryConfig:
    """Settings for the optional hierarchical diary subsystem.

    Attributes:
        enabled: Master switch; when ``False`` the layer is never built.
        update_every: ``N`` — interactions between rolling session updates.
        diary_window: Turns (user + assistant = 1 turn = 2 rows) re-sent in each
            rolling session job. The window always carries at least the last
            ``diary_window`` turns of the session (overlap, for reconciliation)
            and never drops anything ingested since the last applied drain
            (completeness). ``20`` turns → up to 40 rows per job. Distinct from
            :attr:`~nexus_memory.core.config.NexusConfig.history_max_turns`
            (which caps the chat history accessor, a different subsystem).
        max_sentences: Upper bound of the session entry's ``2-max_sentences``
            sentence range, formatted into ``SESSION_PROMPT`` at enqueue time. The
            floor is always 2.
        sessions_per_summary: Finalized session diaries folded into the single
            persistent summary per fold/extension.
        inject_sessions: ``K`` — number of ADDITIONAL previous finalized session
            diaries injected into context (the current session is always
            injected). ``0 <= K <= sessions_per_summary``.
        summary_max_sentences: Upper bound (cap) of the single growing persistent
            summary, formatted into ``SUMMARY_PROMPT`` at enqueue time. Floor 2.
    """

    enabled: bool = False             # master switch; when False the layer is never built
    update_every: int = 5             # N: interactions between rolling session updates
    diary_window: int = 20            # turns re-sent per rolling session job (1 turn = 2 rows)
    max_sentences: int = 50           # upper bound of the entry's 2-N sentence range
    sessions_per_summary: int = 6     # finalized session diaries per fold/extension
    inject_sessions: int = 1          # K: additional previous session diaries injected
    summary_max_sentences: int = 300  # cap of the single growing persistent summary

    def __post_init__(self) -> None:
        """Validate the diary knobs (regardless of ``enabled``).

        Mirrors the ``raise ValueError`` style of
        :meth:`nexus_memory.core.config.NexusConfig.__post_init__`. The
        ``enabled`` bool is not range-checked.
        """
        for name in ("update_every", "diary_window", "sessions_per_summary"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"DiaryConfig.{name} must be >= 1, got {value!r}")
        if self.max_sentences < 2:
            raise ValueError(
                f"DiaryConfig.max_sentences must be >= 2, got {self.max_sentences!r}"
            )
        if self.summary_max_sentences < 2:
            raise ValueError(
                "DiaryConfig.summary_max_sentences must be >= 2, got "
                f"{self.summary_max_sentences!r}"
            )
        if not (0 <= self.inject_sessions <= 6):
            raise ValueError(
                "DiaryConfig.inject_sessions must be between 0 and 6, got "
                f"{self.inject_sessions!r}"
            )
