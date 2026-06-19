"""The diary's assemble hook.

:class:`DiaryContextProvider` plugs into the generic ``context_providers`` seam
the :class:`~nexus_memory.core.context.ContextAssembler` iterates after its three
built-in sections. It renders bounded fragments inside ``<memory_context>``
(after ``<recent_dialogue>``):

* ``<diary session="current" seq="...">`` — the CURRENT session's narrative,
  injected even when the session is not finalized,
* up to ``inject_sessions`` previous finalized ``<diary session=... seq=...>``
  entries (chronological, newest-last), and
* a single ``<persistent_summary>`` — the one growing summary, if present.

No element carries ``id="..."``, so the needle invariant (``<fact id="(\\d+)"``)
is preserved. Text is escaped via :func:`xml.sax.saxutils.escape` and attributes
via :func:`xml.sax.saxutils.quoteattr`, exactly like ``core/context.py``.

The module is fully offline and never imports or calls any LLM SDK.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable
from xml.sax.saxutils import escape, quoteattr

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from .config import DiaryConfig
    from .store import DiaryStore

logger = logging.getLogger(__name__)


class DiaryContextProvider:
    """Render the diary's bounded context fragments + response/meta superset.

    Args:
        store: The diary persistence (read-only here).
        config: The diary layer's own :class:`DiaryConfig` (``inject_sessions``).
        session: Zero-arg callable returning the current ``session_id``.
    """

    def __init__(
        self,
        store: "DiaryStore",
        config: "DiaryConfig",
        session: Callable[[], str],
    ) -> None:
        self.store = store
        self.config = config
        self._session = session

    def provide(self, request: dict) -> dict:
        """Build the diary context fragment + response keys + meta (§5).

        Returns:
            ``{"xml": str, "response": dict, "meta": dict}``. The XML fragment is
            appended inside ``<memory_context>`` after ``<recent_dialogue>``.
        """
        current_id = self._session()
        current = self.store.get_session(current_id)
        previous = self.store.previous_finalized_sessions(
            current_id, self.config.inject_sessions
        )
        summary_row = self.store.get_summary()

        diary_lines: list[str] = []
        diary_resp: list[dict] = []
        diary_chars = 0

        # --- current session diary (injected even when NOT finalized) ---
        if current is not None and (current["summary"] or ""):
            summary = current["summary"] or ""
            diary_chars += len(summary)
            diary_resp.append(
                {"session": current_id, "seq": current["seq"], "current": True,
                 "summary": summary}
            )
            diary_lines.append(
                f"  <diary session=\"current\" seq={quoteattr(str(current['seq']))}>"
                f"{escape(summary)}</diary>"
            )

        # --- previous finalized session diaries (chronological, newest-last) ---
        for sess in previous:
            summary = sess["summary"] or ""
            diary_chars += len(summary)
            diary_resp.append(
                {"session": sess["session_id"], "seq": sess["seq"],
                 "current": False, "summary": summary}
            )
            diary_lines.append(
                f"  <diary session={quoteattr(str(sess['session_id']))} "
                f"seq={quoteattr(str(sess['seq']))}>{escape(summary)}</diary>"
            )

        diary_xml = ("\n".join(diary_lines) + "\n") if diary_lines else ""

        # --- persistent_summary (the single growing row, if present) ---
        summary_resp: dict | None = None
        summary_xml = ""
        if summary_row is not None and (summary_row["summary"] or ""):
            text = summary_row["summary"] or ""
            summary_resp = {
                "summary": text,
                "session_count": summary_row["session_count"],
                "first_session": summary_row["first_session"],
                "last_session": summary_row["last_session"],
            }
            summary_xml = (
                f"  <persistent_summary>{escape(text)}</persistent_summary>\n"
            )

        xml = diary_xml + summary_xml

        return {
            "xml": xml,
            "response": {
                "diary": diary_resp,
                "persistent_summary": summary_resp,
            },
            "meta": {
                "diary_chars": diary_chars,
                "session_diary_count": len(diary_resp),
            },
        }
