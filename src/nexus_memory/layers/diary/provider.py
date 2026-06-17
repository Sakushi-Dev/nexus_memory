"""The diary's assemble hook.

:class:`DiaryContextProvider` plugs into the generic ``context_providers`` seam
the :class:`~nexus_memory.core.context.ContextAssembler` iterates after its three
built-in sections. It renders two bounded fragments inside ``<memory_context>``
(after ``<recent_dialogue>``):

* ``<diary day="...">`` — the previous finalized day's narrative (K=1), and
* ``<persistent_summary>`` — the live ring sections, chronological (newest-last).

Neither element carries ``id="..."``, so the backward-compatible needle invariant
(``<fact id="(\\d+)"``) is preserved. Text is escaped via
:func:`xml.sax.saxutils.escape` and attributes via
:func:`xml.sax.saxutils.quoteattr`, exactly like ``core/context.py``.

The module is fully offline and never imports or calls any LLM SDK.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from .config import DiaryConfig
    from .store import DiaryStore

logger = logging.getLogger(__name__)


class DiaryContextProvider:
    """Render the diary's bounded context fragments + response/meta superset.

    Args:
        store: The diary persistence (read-only here).
        config: The diary layer's own :class:`DiaryConfig` (``inject_days`` = K).
    """

    def __init__(self, store: "DiaryStore", config: "DiaryConfig") -> None:
        self.store = store
        self.config = config

    def provide(self, request: dict) -> dict:
        """Build the diary context fragment + response keys + meta (§5).

        Returns:
            ``{"xml": str, "response": dict, "meta": dict}``. The XML fragment is
            appended inside ``<memory_context>`` after ``<recent_dialogue>``.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        diary_days = self.store.finalized_days_before(today, self.config.inject_days)
        sections = self.store.sections()

        # --- diary (K=1: the newest finalized previous day drives the element) ---
        diary_resp: dict | None = None
        diary_chars = 0
        diary_xml = ""
        if diary_days:
            newest = diary_days[-1]
            day = newest["period"]
            summary = newest["summary"] or ""
            diary_chars = len(summary)
            diary_resp = {"day": day, "summary": summary}
            diary_xml = (
                f"  <diary day={quoteattr(day)}>{escape(summary)}</diary>\n"
            )

        # --- persistent_summary (all live sections, chronological newest-last) ---
        section_resp: list[dict] = []
        section_xml = ""
        if sections:
            lines: list[str] = ["  <persistent_summary>"]
            for sec in sections:
                first = sec.get("first_day") or ""
                last = sec.get("last_day") or ""
                days_attr = f"{first}..{last}"
                summary = sec.get("summary") or ""
                section_resp.append(
                    {
                        "seq": sec["seq"],
                        "days": days_attr,
                        "summary": summary,
                    }
                )
                lines.append(
                    f"    <section seq={quoteattr(str(sec['seq']))} "
                    f"days={quoteattr(days_attr)}>{escape(summary)}</section>"
                )
            lines.append("  </persistent_summary>")
            section_xml = "\n".join(lines) + "\n"

        xml = diary_xml + section_xml

        return {
            "xml": xml,
            "response": {
                "diary": diary_resp,
                "persistent_summary": section_resp,
            },
            "meta": {
                "diary_chars": diary_chars,
                "section_count": len(sections),
            },
        }
