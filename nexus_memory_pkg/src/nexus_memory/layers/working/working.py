"""Layer I — Working Memory (volatile, in-RAM ring buffer).

The working memory holds the most recent conversation turns in process memory
only. It is fast, thread-safe, and bounded: once it grows beyond ``max_turns``
the oldest turns are evicted. Nothing here is persisted — durable dialogue
history lives in the episodic (Layer II) store.

Timestamps use the same UTC ``YYYY-MM-DD HH:MM:SS`` format as the database, via
the shared :func:`nexus_memory.db._utc_now_str` helper, so working-memory turns
interleave cleanly with persisted ones.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass

from ...core.db import _utc_now_str
from ...core.xml_format import estimate_tokens

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """A single conversational turn.

    Attributes:
        role: ``"user"`` or ``"assistant"``.
        content: The turn's text.
        timestamp: UTC ``YYYY-MM-DD HH:MM:SS`` capture time.
    """

    role: str
    content: str
    timestamp: str

    def to_dict(self) -> dict:
        """Return this turn as a plain ``{role, content, timestamp}`` dict."""
        return {"role": self.role, "content": self.content, "timestamp": self.timestamp}


class WorkingMemory:
    """Thread-safe, bounded ring buffer of the most recent dialogue turns.

    Capacity is ``max_turns``; appending beyond it evicts the oldest turn. All
    mutating and reading operations are guarded by an internal lock so the
    buffer is safe to share across the orchestrator's caller thread and the
    writer's background thread.
    """

    def __init__(self, max_turns: int = 50) -> None:
        """Create an empty working memory bounded to ``max_turns`` turns."""
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self.max_turns: int = max_turns
        self._turns: deque[Turn] = deque(maxlen=max_turns)
        self._lock = threading.RLock()

    def add_turn(self, role: str, content: str) -> None:
        """Append a single turn, evicting the oldest if at capacity."""
        turn = Turn(role=role, content=content, timestamp=_utc_now_str())
        with self._lock:
            self._turns.append(turn)
        logger.debug("WorkingMemory: added %s turn (size now %d)", role, len(self._turns))

    def add_interaction(self, query: str, response: str) -> None:
        """Append a user turn followed by an assistant turn."""
        # Two discrete appends so eviction semantics match per-turn behavior.
        self.add_turn("user", query)
        self.add_turn("assistant", response)

    def recent(self, n: int | None = None) -> list[Turn]:
        """Return up to the last ``n`` turns, newest-last.

        ``n=None`` returns the full buffer (still bounded by ``max_turns``).
        """
        with self._lock:
            turns = list(self._turns)
        if n is None:
            return turns
        if n <= 0:
            return []
        return turns[-n:]

    def snapshot(self) -> list[dict]:
        """Return all turns as ``[{role, content, timestamp}]`` (newest-last)."""
        with self._lock:
            return [t.to_dict() for t in self._turns]

    def token_estimate(self) -> int:
        """Rough token count of buffered content via the shared
        :func:`~nexus_memory.core.xml_format.estimate_tokens` heuristic
        (``len(s)//4``), so it matches ``history()``/``tokens()``."""
        with self._lock:
            joined = "".join(t.content for t in self._turns)
        return estimate_tokens(joined)

    def clear(self) -> None:
        """Drop all buffered turns."""
        with self._lock:
            self._turns.clear()
        logger.debug("WorkingMemory: cleared.")

    def __len__(self) -> int:
        """Number of turns currently buffered."""
        with self._lock:
            return len(self._turns)
