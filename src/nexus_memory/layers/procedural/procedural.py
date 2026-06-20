"""Layer IV — Procedural Memory (persistent behavioral directives).

Procedural memory stores *standing behavioral rules* the agent should apply to
every future response — e.g. "Respond in German.", "Keep answers concise." or
"Address the user as Sam.". Unlike semantic facts (what is true) or episodic
turns (what was said), procedural rules encode *how to behave*.

Two pieces live here:

* :class:`DirectiveDetector` — an abstract strategy that inspects an interaction
  and returns any standing directives it implies. :class:`MockDirectiveDetector`
  is a deterministic, offline (DE + EN) rule-based implementation.
* :class:`ProceduralStore` — owns the ``procedural_rules`` SQLite table (created
  ``IF NOT EXISTS`` on construction via the shared :class:`~nexus_memory.db.NexusDB`
  connection) and provides upsert / list / deactivate / directive-injection APIs.

The store reuses the database connection and write lock owned by ``NexusDB``: all
writes happen inside ``with db.lock:`` so it shares one serialized writer with the
semantic and episodic layers. Timestamps use the shared UTC helper so rows
interleave cleanly with the rest of the system.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ...core.config import NexusConfig
from ...core.db import _utc_now_str

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .db import NexusDB

logger = logging.getLogger(__name__)

# Allowed rule categories. Anything else is normalized to ``"other"``.
_VALID_CATEGORIES: frozenset[str] = frozenset(
    {"language", "tone", "format", "persona", "other"}
)

# DDL for this layer's own table. Idempotent so construction is safe to repeat.
_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS procedural_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    directive TEXT NOT NULL,
    category TEXT,
    priority INTEGER DEFAULT 5,
    active INTEGER DEFAULT 1,
    source TEXT,
    timestamp TEXT NOT NULL,
    UNIQUE(directive)
);
CREATE INDEX IF NOT EXISTS idx_procedural_active
    ON procedural_rules(active, priority DESC);
"""


class DirectiveDetector(ABC):
    """Strategy that derives standing behavioral directives from an interaction."""

    @abstractmethod
    def detect(self, query: str, response: str) -> list[dict]:
        """Return the directives implied by an interaction.

        Args:
            query: The user's text (the only text a detector should act on).
            response: The assistant's reply (provided for context; the mock
                implementation deliberately ignores it).

        Returns:
            A list of ``{"directive": str, "category": str, "priority": int}``
            dicts, or ``[]`` when nothing standing is detected.
        """
        raise NotImplementedError


class MockDirectiveDetector(DirectiveDetector):
    """Deterministic, offline rule-based detector for German + English.

    It recognizes a small set of common standing requests and only ever fires on
    the **user's** text (``response`` is ignored). Detection is case-insensitive.
    Recognized patterns:

    * ``sprich/antworte/red ... deutsch``  -> ``"Respond in German."`` (language)
    * ``... in english ...`` / ``answer in english`` -> ``"Respond in English."``
    * ``fasse dich kurz`` / ``be concise``  -> ``"Keep answers concise."`` (tone)
    * ``nenn mich X`` / ``call me X``        -> ``"Address the user as X."`` (persona)
    * ``immer/always ...`` / ``nie/never ...`` -> generic standing rule (other)

    Returns ``[]`` when nothing matches. Order is stable: language, tone,
    persona, then generic always/never rules.
    """

    # --- language: German ---
    # e.g. "sprich ab jetzt deutsch", "antworte auf deutsch", "red deutsch".
    _DE_LANG = re.compile(
        r"\b(?:sprich|antworte|antwort|red|rede|schreib|schreibe)\b[^.!?\n]*\bdeutsch\b",
        re.IGNORECASE,
    )
    # --- language: English (DE trigger "auf englisch" or EN "answer in english") ---
    _EN_LANG = re.compile(
        r"\b(?:sprich|antworte|antwort|red|rede|schreib|schreibe|respond|answer|reply|speak|talk|write)\b"
        r"[^.!?\n]*\b(?:english|englisch)\b",
        re.IGNORECASE,
    )

    # --- tone: concise ---
    # The German phrasings allow words between "dich" and "kurz" (e.g.
    # "fasse dich ab jetzt bitte kurz"), mirroring how the language patterns
    # tolerate intervening words, so natural insertions don't defeat detection.
    _CONCISE = re.compile(
        r"\bfass(?:e)?\s+dich\b[^.!?\n]*\bkurz\b"   # "fass(e) dich [...] kurz"
        r"|\bhalt(?:e)?\s+dich\b[^.!?\n]*\bkurz\b"  # "halt(e) dich [...] kurz"
        r"|\bbe\s+(?:more\s+)?concise\b"
        r"|\bkeep\s+(?:it|answers?|responses?)\s+(?:short|concise|brief)\b",
        re.IGNORECASE,
    )

    # --- persona: address-as / name ---
    # Captures the name token(s) after the trigger. Allows a couple of words /
    # accented characters; stops at punctuation.
    _ADDRESS = re.compile(
        r"\b(?:nenn(?:e)?\s+mich|call\s+me|address\s+me\s+as)\s+"
        r"(?P<name>[A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ'\-]*\.?(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ'\-]*\.?){0,2})",
        re.IGNORECASE,
    )
    # Filler/stop-words the greedy name capture may absorb; stripped before use
    # (honorific titles like "Dr."/"Mr." stay — they are not in this set).
    _NAME_STOPWORDS = frozenset({
        "bitte", "mal", "doch", "einfach", "jetzt", "ab", "halt", "eben",
        "please", "the", "just", "now", "ok", "okay", "kindly",
    })

    # --- generic standing rules: always / never ---
    _ALWAYS = re.compile(r"\b(?:immer|always)\b", re.IGNORECASE)
    _NEVER = re.compile(r"\b(?:nie|niemals|never)\b", re.IGNORECASE)

    def detect(self, query: str, response: str) -> list[dict]:
        """Detect standing directives in the user's ``query`` (ignores ``response``)."""
        if not query or not query.strip():
            return []

        text = query
        found: list[dict] = []
        seen: set[str] = set()

        def _add(directive: str, category: str, priority: int) -> None:
            if directive not in seen:
                seen.add(directive)
                found.append(
                    {"directive": directive, "category": category, "priority": priority}
                )

        # Language. Check English first: an explicit "english" mention should not
        # be shadowed by a stray "deutsch" token, and vice versa they are mutually
        # specific via their own patterns.
        if self._EN_LANG.search(text):
            _add("Respond in English.", "language", 8)
        if self._DE_LANG.search(text):
            _add("Respond in German.", "language", 8)

        # Tone.
        if self._CONCISE.search(text):
            _add("Keep answers concise.", "tone", 6)

        # Persona / form of address.
        m = self._ADDRESS.search(text)
        if m:
            raw = m.group("name").strip().rstrip(".,!?;:").strip()
            # Drop filler/stop-words the greedy capture may have absorbed
            # (e.g. "nenn mich bitte Sam" -> "Sam"), keeping honorific titles
            # ("call me Dr. Sam" -> "Dr. Sam").
            tokens = [t for t in raw.split()
                      if t.lower().strip(".,") not in self._NAME_STOPWORDS]
            name = " ".join(tokens).rstrip(".,!?;:").strip()
            if name:
                _add(f"Address the user as {name}.", "persona", 7)

        # Generic standing rules. Only emit when not already captured by a more
        # specific rule above, to avoid noisy duplicates for the same sentence.
        if not found:
            if self._ALWAYS.search(text):
                _add(f"Standing rule: {self._normalize(text)}", "other", 5)
            elif self._NEVER.search(text):
                _add(f"Standing rule: {self._normalize(text)}", "other", 5)

        if found:
            logger.debug("MockDirectiveDetector: detected %d directive(s)", len(found))
        return found

    @staticmethod
    def _normalize(text: str) -> str:
        """Collapse whitespace and trim a user sentence for a generic directive."""
        return re.sub(r"\s+", " ", text).strip()


class ProceduralStore:
    """Persistent store of behavioral directives backed by ``procedural_rules``.

    Owns its table (created ``IF NOT EXISTS`` on construction) and uses the
    :class:`~nexus_memory.db.NexusDB` connection and write lock so it shares a
    single serialized writer with the other layers.
    """

    def __init__(
        self,
        db: "NexusDB",
        config: NexusConfig,
        detector: DirectiveDetector | None = None,
    ) -> None:
        """Create the store and ensure its table exists.

        Args:
            db: The shared database; provides ``conn`` and the ``lock``.
            config: Global config (``procedural_max_directives`` caps injection).
            detector: Directive detector for :meth:`detect_and_store`; defaults
                to a :class:`MockDirectiveDetector`.
        """
        self.db = db
        self.config = config
        self.detector: DirectiveDetector = detector or MockDirectiveDetector()
        self._initialize()

    # ------------------------------------------------------------------ #
    # schema
    # ------------------------------------------------------------------ #
    def _initialize(self) -> None:
        """Create the ``procedural_rules`` table and index if missing."""
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA_SQL)
            self.db.conn.commit()
        logger.debug("ProceduralStore initialized (table procedural_rules ready).")

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def add_rule(
        self,
        directive: str,
        category: str = "other",
        priority: int = 5,
        source: str = "manual",
    ) -> dict:
        """Insert a directive, or reactivate/update it if it already exists.

        ``directive`` is the natural UNIQUE key. On conflict the existing row is
        re-activated and its ``category``/``priority``/``source``/``timestamp``
        refreshed, so a repeated directive yields exactly one row.

        Args:
            directive: The imperative rule text (e.g. ``"Respond in German."``).
            category: One of language/tone/format/persona/other (else ``"other"``).
            priority: 1..10, higher applied first. Clamped into range.
            source: ``"manual"`` or ``"auto"``.

        Returns:
            The stored rule as a dict (see :meth:`_row_to_dict`).
        """
        directive = (directive or "").strip()
        if not directive:
            raise ValueError("directive must be a non-empty string")
        category = category if category in _VALID_CATEGORIES else "other"
        priority = max(1, min(10, int(priority)))
        now = _utc_now_str()

        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO procedural_rules "
                "(directive, category, priority, active, source, timestamp) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(directive) DO UPDATE SET "
                "category=excluded.category, "
                "priority=excluded.priority, "
                "active=1, "
                "source=excluded.source, "
                "timestamp=excluded.timestamp",
                (directive, category, priority, source, now),
            )
            self.db.conn.commit()
            row = self.db.conn.execute(
                "SELECT id, directive, category, priority, active, source, timestamp "
                "FROM procedural_rules WHERE directive = ?",
                (directive,),
            ).fetchone()
        logger.debug("ProceduralStore.add_rule upserted %r (source=%s)", directive, source)
        return self._row_to_dict(row)

    def detect_and_store(self, query: str, response: str) -> list[dict]:
        """Run the detector on an interaction and persist any directives found.

        Each detected directive is stored via :meth:`add_rule` with
        ``source="auto"``. Returns the list of stored rule dicts (empty if the
        detector found nothing).
        """
        detected = self.detector.detect(query, response)
        stored: list[dict] = []
        for d in detected:
            stored.append(
                self.add_rule(
                    directive=d["directive"],
                    category=d.get("category", "other"),
                    priority=int(d.get("priority", 5)),
                    source="auto",
                )
            )
        if stored:
            logger.debug("ProceduralStore.detect_and_store persisted %d rule(s)", len(stored))
        return stored

    def deactivate(self, rule_id: int) -> bool:
        """Mark a rule inactive by id. Returns ``True`` if a row changed."""
        with self.db.lock:
            cur = self.db.conn.execute(
                "UPDATE procedural_rules SET active = 0 WHERE id = ? AND active = 1",
                (int(rule_id),),
            )
            self.db.conn.commit()
        changed = cur.rowcount > 0
        logger.debug("ProceduralStore.deactivate(%s) -> %s", rule_id, changed)
        return changed

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def list_rules(self, active_only: bool = True) -> list[dict]:
        """Return rules ordered by priority desc, then most-recent first.

        Args:
            active_only: When ``True`` (default) only active rules are returned.
        """
        sql = (
            "SELECT id, directive, category, priority, active, source, timestamp "
            "FROM procedural_rules "
        )
        if active_only:
            sql += "WHERE active = 1 "
        sql += "ORDER BY priority DESC, id DESC"
        rows = self.db.conn.execute(sql).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def directives(self) -> list[str]:
        """Return active directive strings, priority desc, capped at config.

        The cap is :attr:`NexusConfig.procedural_max_directives`. These are the
        strings injected into the assembled ``<procedural>`` context block.
        """
        cap = max(0, int(self.config.procedural_max_directives))
        rows = self.db.conn.execute(
            "SELECT directive FROM procedural_rules "
            "WHERE active = 1 "
            "ORDER BY priority DESC, id DESC "
            "LIMIT ?",
            (cap,),
        ).fetchall()
        return [r["directive"] for r in rows]

    def count(self, active_only: bool = True) -> int:
        """Return the number of stored rules (active by default)."""
        if active_only:
            row = self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM procedural_rules WHERE active = 1"
            ).fetchone()
        else:
            row = self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM procedural_rules"
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """Convert a result row into a plain typed dict."""
        d = dict(row)
        if "id" in d and d["id"] is not None:
            d["id"] = int(d["id"])
        if "priority" in d and d["priority"] is not None:
            d["priority"] = int(d["priority"])
        if "active" in d and d["active"] is not None:
            d["active"] = int(d["active"])
        return d
