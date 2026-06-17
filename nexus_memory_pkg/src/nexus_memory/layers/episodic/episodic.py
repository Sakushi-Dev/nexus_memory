"""Layer II — Episodic Memory (the diary / dialogue history).

:class:`EpisodicStore` persists the **raw** dialogue (every user/assistant turn)
plus narrative day summaries. It is the durable counterpart to the volatile
Layer I :class:`~nexus_memory.working.WorkingMemory`: where working memory keeps
only the last N turns in RAM, the episodic store keeps the full history on disk
so a past interaction can be reconstructed verbatim and a day can be summarized.

Per the v2 multi-layer contract, this store does **not** own the connection
lifecycle — :class:`~nexus_memory.db.NexusDB` does. The store creates *its own*
tables with ``CREATE TABLE IF NOT EXISTS`` on construction, using the shared
connection (``db.conn``) and the shared write lock (``with db.lock:``). All
timestamps use the same UTC ``YYYY-MM-DD HH:MM:SS`` format as the rest of the
system via :func:`nexus_memory.db._utc_now_str`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ...core.config import NexusConfig
from ...core.db import _utc_now_str
from .summarization import MockSummarizer, Summarizer

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from .db import NexusDB

logger = logging.getLogger(__name__)

# Idempotent DDL for this layer's own tables. Created on construction.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT NOT NULL,            -- 'user' | 'assistant'
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,       -- UTC 'YYYY-MM-DD HH:MM:SS'
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS episodic_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,          -- e.g. a day 'YYYY-MM-DD'
    summary TEXT NOT NULL,
    turn_count INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_turns_ts ON episodic_turns(timestamp);
"""


class EpisodicStore:
    """Persistent raw dialogue history + narrative day summaries (Layer II).

    Owns the ``episodic_turns`` and ``episodic_summaries`` tables. Reads use the
    shared connection directly; writes are guarded by ``db.lock`` so they are
    safe alongside the semantic writer's background thread.
    """

    def __init__(
        self,
        db: "NexusDB",
        config: NexusConfig,
        summarizer: Summarizer | None = None,
    ) -> None:
        """Create the store and ensure its tables exist.

        Args:
            db: The shared :class:`NexusDB` (owns the connection + write lock).
            config: The active configuration.
            summarizer: Strategy used by :meth:`summarize_day`; defaults to the
                deterministic, offline :class:`MockSummarizer`.
        """
        self.db = db
        self.config = config
        self.summarizer: Summarizer = summarizer or MockSummarizer()
        self._initialize()

    def _initialize(self) -> None:
        """Create this layer's tables (idempotent) under the shared write lock."""
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA)
            self.db.conn.commit()
        logger.debug("EpisodicStore initialized (tables ensured).")

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def log_turn(
        self,
        role: str,
        content: str,
        session_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Persist a single dialogue turn and return its row id.

        Args:
            role: ``"user"`` or ``"assistant"``.
            content: The turn's text.
            session_id: Optional conversation/session identifier.
            metadata: Optional JSON-serializable metadata.

        Returns:
            The auto-incremented ``id`` of the inserted turn.
        """
        meta_json = json.dumps(metadata) if metadata is not None else None
        timestamp = _utc_now_str()
        with self.db.lock:
            cur = self.db.conn.execute(
                "INSERT INTO episodic_turns (session_id, role, content, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, timestamp, meta_json),
            )
            self.db.conn.commit()
        turn_id = int(cur.lastrowid)
        logger.debug("EpisodicStore: logged %s turn id=%d", role, turn_id)
        return turn_id

    def log_interaction(
        self,
        query: str,
        response: str,
        session_id: str | None = None,
    ) -> list[int]:
        """Persist a ``(query, response)`` pair as a user then assistant turn.

        Returns:
            The two inserted turn ids, ``[user_id, assistant_id]``.
        """
        user_id = self.log_turn("user", query, session_id=session_id)
        assistant_id = self.log_turn("assistant", response, session_id=session_id)
        return [user_id, assistant_id]

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def turns(
        self,
        time_range: tuple[str, str] | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return turns in chronological (oldest-first) order.

        Args:
            time_range: Optional inclusive ``(start, end)`` timestamp bounds.
            session_id: Optional session filter.
            limit: Maximum number of turns to return.

        Returns:
            A list of ``{id, session_id, role, content, timestamp, metadata}``
            dicts ordered oldest-first.
        """
        clauses: list[str] = []
        params: list[object] = []
        if time_range is not None:
            start, end = time_range
            clauses.append("timestamp >= ? AND timestamp <= ?")
            params.extend((start, end))
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.conn.execute(
            "SELECT id, session_id, role, content, timestamp, metadata "
            f"FROM episodic_turns {where} "
            "ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def recent_turns(self, n: int = 6) -> list[dict]:
        """Return up to the last ``n`` turns, newest-last (chronological)."""
        if n <= 0:
            return []
        rows = self.db.conn.execute(
            "SELECT id, session_id, role, content, timestamp, metadata "
            "FROM episodic_turns ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        # Fetched newest-first; reverse to present newest-last (chronological).
        return [self._row_to_dict(r) for r in reversed(rows)]

    def reconstruct(self, time_range: tuple[str, str] | None = None) -> str:
        """Return a human-readable transcript of the stored dialogue.

        Each line is ``"[timestamp] role: content"`` in chronological order.
        """
        turns = self.turns(time_range=time_range, limit=10_000)
        lines = [
            f"[{t['timestamp']}] {t['role']}: {t['content']}" for t in turns
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # summaries
    # ------------------------------------------------------------------ #
    def latest_day(self) -> str | None:
        """Return the most recent day (``YYYY-MM-DD``) that has any turns.

        ``None`` when the episodic store is empty.
        """
        row = self.db.conn.execute(
            "SELECT MAX(timestamp) AS ts FROM episodic_turns"
        ).fetchone()
        ts = row["ts"] if row is not None else None
        return ts.split(" ", 1)[0] if ts else None

    def summarize_day(self, day: str | None = None, store: bool = True) -> dict:
        """Summarize all turns from a given day into a narrative.

        Args:
            day: The day as ``YYYY-MM-DD``. When ``None`` (default), the most
                recent day that actually has turns is summarized — so asking for
                "the diary" is never empty merely because the UTC date rolled
                over since the last conversation.
            store: When ``True`` (default), persist the summary to
                ``episodic_summaries``.

        Returns:
            ``{"period": day, "summary": str, "turn_count": int}``.
        """
        if day is None:
            day = self.latest_day() or _utc_now_str().split(" ", 1)[0]
        # Day bounds in the sortable 'YYYY-MM-DD HH:MM:SS' text space.
        time_range = (f"{day} 00:00:00", f"{day} 23:59:59")
        day_turns = self.turns(time_range=time_range, limit=10_000)
        summary = self.summarizer.summarize(day_turns)
        turn_count = len(day_turns)

        if store and summary:
            with self.db.lock:
                self.db.conn.execute(
                    "INSERT INTO episodic_summaries (period, summary, turn_count, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (day, summary, turn_count, _utc_now_str()),
                )
                self.db.conn.commit()
            logger.debug(
                "EpisodicStore: stored summary for %s (%d turn(s))", day, turn_count
            )

        return {"period": day, "summary": summary, "turn_count": turn_count}

    def summaries(self, limit: int = 30) -> list[dict]:
        """Return stored day summaries, newest-first.

        Returns:
            A list of ``{id, period, summary, turn_count, created_at}`` dicts.
        """
        rows = self.db.conn.execute(
            "SELECT id, period, summary, turn_count, created_at "
            "FROM episodic_summaries ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """Return the total number of stored turns."""
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM episodic_turns"
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_dict(row) -> dict:
        """Convert a turn row to a dict, parsing the metadata JSON column."""
        d = dict(row)
        raw_meta = d.get("metadata")
        if raw_meta:
            try:
                d["metadata"] = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
        else:
            d["metadata"] = {}
        return d
