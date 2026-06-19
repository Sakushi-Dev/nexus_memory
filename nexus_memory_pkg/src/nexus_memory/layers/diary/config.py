"""Configuration for the diary layer (Layer V).

The diary owns its own config dataclass. Nothing here is added to
:class:`~nexus_memory.core.config.NexusConfig`; the host opts in explicitly by
passing ``NexusMemory(diary=DiaryConfig(enabled=True, ...))``. When the layer is
off (``enabled=False``, the default), the layer is never constructed and no diary
tables are created and existing behavior is unchanged.

Parameters: N=update_every=3, SECTION_SIZE=7, M=max_sections=8,
K=inject_days=1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiaryConfig:
    """Settings for the optional hierarchical diary subsystem.

    Attributes:
        enabled: Master switch; when ``False`` the layer is never built.
        update_every: ``N`` — interactions between rolling daily updates.
        section_size: Daily diaries folded into one persistent section.
        max_sections: ``M`` — ring capacity (oldest section overwritten).
        inject_days: ``K`` — finalized daily diaries injected into context.
    """

    enabled: bool = False        # master switch; when False the layer is never built
    update_every: int = 3        # N: interactions between rolling daily updates
    section_size: int = 7        # daily diaries per persistent section
    max_sections: int = 8        # M: ring capacity (oldest section overwritten)
    inject_days: int = 1         # K: finalized daily diaries injected into context
