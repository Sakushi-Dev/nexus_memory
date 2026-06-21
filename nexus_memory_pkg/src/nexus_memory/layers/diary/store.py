"""Persistence for the diary layer (Layer V) — the 2 narrative tables.

:class:`DiaryStore` owns the two diary NARRATIVE tables (``diary_sessions`` and
``persistent_summary``). The job outbox (``summarization_jobs``) is no longer
owned here — it was lifted into the shared, layer-agnostic
:class:`~nexus_memory.core.auxbus.bus.AuxBus`. Like the other layer stores
(see :class:`~nexus_memory.layers.episodic.episodic.EpisodicStore`), it
does NOT own the connection lifecycle — :class:`~nexus_memory.core.db.NexusDB`
does. The store creates its own tables with ``CREATE TABLE IF NOT EXISTS`` on
construction, using the shared connection (``db.conn``) under the shared,
re-entrant write lock (``with db.lock:``). The DDL is created here (not in
``schema.sql``) and only ever when the diary layer is active.

All timestamps use the same UTC ``YYYY-MM-DD HH:MM:SS`` format as the rest of the
system via :func:`nexus_memory.core.db._utc_now_str`. The module is fully offline
and deterministic; it never imports or calls any LLM SDK.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...core.db import _utc_now_str

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ...core.db import NexusDB

logger = logging.getLogger(__name__)

# Idempotent DDL for this layer's two NARRATIVE tables. Created on
# construction, only ever when the diary layer is active. The job outbox
# (summarization_jobs) lives in the shared AuxBus, not here.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS diary_sessions (
    session_id        TEXT PRIMARY KEY,      -- the orchestrator.session_id (uuid4) of the session
    seq               INTEGER UNIQUE,        -- monotonic order (1,2,3…); orders current/previous + the 6-fold
    summary           TEXT DEFAULT '',       -- the session narrative (rolling)
    covered_through   INTEGER DEFAULT 0,     -- last-APPLIED high-water mark (max episodic_turns.id folded in)
    interaction_count INTEGER DEFAULT 0,     -- interactions seen this session
    finalized         INTEGER DEFAULT 0,     -- 1 once the session is closed (rollover/close)
    folded            INTEGER DEFAULT 0,     -- 1 once folded into the persistent summary
    created_at        TEXT,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS persistent_summary (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    summary       TEXT DEFAULT '',                     -- the single growing summary
    session_count INTEGER DEFAULT 0,                   -- sessions folded so far
    first_session TEXT,                                -- covered range (session_id)
    last_session  TEXT,
    updated_at    TEXT
);
"""


class DiaryStore:
    """Owns the diary layer's 2 narrative tables and their SQL.

    Reads use the shared connection directly; every write is guarded by
    ``db.lock`` so it is safe alongside the semantic writer's background thread,
    exactly like :class:`EpisodicStore`. The job outbox lives in the shared
    :class:`~nexus_memory.core.auxbus.bus.AuxBus`, not here.
    """

    def __init__(self, db: "NexusDB") -> None:
        """Create the store and ensure its two narrative tables exist.

        Args:
            db: The shared :class:`NexusDB` (owns the connection + write lock).
        """
        self.db = db
        self._initialize()

    def _initialize(self) -> None:
        """Create this layer's tables (idempotent) under the shared write lock."""
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA)
            self.db.conn.commit()
        logger.debug("DiaryStore initialized (2 narrative tables ensured).")

    # ================================================================== #
    # diary_sessions (per-session narrative)
    # ================================================================== #
    def get_session(self, session_id: str) -> dict | None:
        """Return the ``diary_sessions`` row for ``session_id``, or ``None``."""
        row = self.db.conn.execute(
            "SELECT * FROM diary_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_session(self, session_id: str) -> None:
        """INSERT a default session row if new, assigning ``seq = max_seq+1``.

        Idempotent: an existing session is left untouched (its ``seq`` and
        counters are preserved). New sessions get the next monotonic ``seq`` so
        ``current``/``previous`` order and the fold trigger are well-defined.
        """
        with self.db.lock:
            existing = self.db.conn.execute(
                "SELECT 1 FROM diary_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is not None:
                return
            seq_row = self.db.conn.execute(
                "SELECT MAX(seq) AS s FROM diary_sessions"
            ).fetchone()
            next_seq = (int(seq_row["s"]) if seq_row["s"] is not None else 0) + 1
            now = _utc_now_str()
            self.db.conn.execute(
                "INSERT INTO diary_sessions "
                "(session_id, seq, summary, covered_through, interaction_count, "
                " finalized, folded, created_at, updated_at) "
                "VALUES (?, ?, '', 0, 0, 0, 0, ?, ?)",
                (session_id, next_seq, now, now),
            )
            self.db.conn.commit()

    def bump_interaction(self, session_id: str) -> int:
        """Increment ``interaction_count`` for the session; return the NEW count."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_sessions SET interaction_count = interaction_count + 1 "
                "WHERE session_id = ?",
                (session_id,),
            )
            self.db.conn.commit()
            row = self.db.conn.execute(
                "SELECT interaction_count FROM diary_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["interaction_count"])

    def set_session_summary(
        self, session_id: str, summary: str, covered_through: int
    ) -> None:
        """Set the session's ``summary`` + ``covered_through`` (and ``updated_at``).

        ``covered_through`` is a monotonic last-APPLIED high-water mark
        (``advance_to = max(id in window)`` from the applied job). It no longer
        gates the rolling session window (the overlapping window does that); it is
        kept so session finalization/folding still terminate and so the
        scheduler's empty-tick guard can tell when nothing new was ingested since
        the last apply.
        """
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_sessions SET summary = ?, covered_through = ?, updated_at = ? "
                "WHERE session_id = ?",
                (summary, covered_through, _utc_now_str(), session_id),
            )
            self.db.conn.commit()

    def finalize_session(self, session_id: str) -> None:
        """Mark the session ``finalized = 1``."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_sessions SET finalized = 1 WHERE session_id = ?",
                (session_id,),
            )
            self.db.conn.commit()

    def mark_folded(self, session_id: str) -> None:
        """Mark the session ``folded = 1`` (folded into the persistent summary)."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_sessions SET folded = 1 WHERE session_id = ?",
                (session_id,),
            )
            self.db.conn.commit()

    def max_seq_session(self) -> dict | None:
        """Return the session row with the highest ``seq``, or ``None`` if empty."""
        row = self.db.conn.execute(
            "SELECT * FROM diary_sessions ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def sessions(self) -> list[dict]:
        """Return all ``diary_sessions`` rows, ``seq`` ascending."""
        rows = self.db.conn.execute(
            "SELECT * FROM diary_sessions ORDER BY seq ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def finalized_unfolded_sessions(self) -> list[dict]:
        """Return finalized, not-yet-folded sessions, ``seq`` ascending."""
        rows = self.db.conn.execute(
            "SELECT * FROM diary_sessions WHERE finalized = 1 AND folded = 0 "
            "ORDER BY seq ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def previous_finalized_sessions(self, current_id: str, limit: int) -> list[dict]:
        """Return the newest ``limit`` finalized sessions before ``current_id``.

        "Before" is by monotonic ``seq`` (strictly less than the current
        session's ``seq``; if the current session has no row yet, all finalized
        sessions qualify). Only sessions with a non-empty summary are considered.
        The newest ``limit`` such sessions are selected, then returned in
        chronological (``seq`` ASC) order.
        """
        cur = self.db.conn.execute(
            "SELECT seq FROM diary_sessions WHERE session_id = ?", (current_id,)
        ).fetchone()
        cur_seq = cur["seq"] if cur is not None and cur["seq"] is not None else None
        if cur_seq is None:
            rows = self.db.conn.execute(
                "SELECT * FROM diary_sessions "
                "WHERE finalized = 1 AND summary != '' "
                "ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM diary_sessions "
                "WHERE finalized = 1 AND seq < ? AND summary != '' "
                "ORDER BY seq DESC LIMIT ?",
                (cur_seq, limit),
            ).fetchall()
        # Selected newest-first; present chronologically.
        return [dict(r) for r in reversed(rows)]

    # ================================================================== #
    # persistent_summary (single growing row)
    # ================================================================== #
    def get_summary(self) -> dict | None:
        """Return the singleton ``persistent_summary`` row, or ``None`` if empty."""
        row = self.db.conn.execute(
            "SELECT * FROM persistent_summary WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_summary(self, summary: str, folded_sessions: list[dict]) -> None:
        """Create or extend the single persistent summary row.

        Sets ``summary`` to ``summary`` (the host-supplied extension), bumps
        ``session_count`` by ``len(folded_sessions)``, sets ``first_session`` to
        the oldest folded session (only when not already set) and ``last_session``
        to the newest folded session. ``folded_sessions`` are the session rows
        being folded, ``seq`` ascending.
        """
        if not folded_sessions:
            return
        first = folded_sessions[0]["session_id"]
        last = folded_sessions[-1]["session_id"]
        count = len(folded_sessions)
        now = _utc_now_str()
        with self.db.lock:
            existing = self.db.conn.execute(
                "SELECT 1 FROM persistent_summary WHERE id = 1"
            ).fetchone()
            if existing is None:
                self.db.conn.execute(
                    "INSERT INTO persistent_summary "
                    "(id, summary, session_count, first_session, last_session, updated_at) "
                    "VALUES (1, ?, ?, ?, ?, ?)",
                    (summary, count, first, last, now),
                )
            else:
                self.db.conn.execute(
                    "UPDATE persistent_summary SET "
                    "summary = ?, "
                    "session_count = session_count + ?, "
                    "first_session = COALESCE(first_session, ?), "
                    "last_session = ?, "
                    "updated_at = ? "
                    "WHERE id = 1",
                    (summary, count, first, last, now),
                )
            self.db.conn.commit()
