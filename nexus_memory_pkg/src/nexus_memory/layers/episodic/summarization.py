"""Summarization for Layer II (Episodic / Diary) memory.

A real deployment would call an LLM to write a fluent narrative of a day's
dialogue. For local-first, dependency-free, deterministic operation (and for
tests), :class:`MockSummarizer` stands in for that: it extractively selects the
most informative *user* statements from the turns and joins them into a short
narrative such as ``"On 2026-06-15 the user talked about: ..."``.

No model download, no network, fully deterministic. The default summarizer used
by :class:`~nexus_memory.episodic.EpisodicStore` is :class:`MockSummarizer`.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Sentence splitter mirrors the extraction module: break on sentence-final
# punctuation or newlines so we work on atomic statements.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")

# Durable / high-value signal tokens (DE + EN). Statements containing these are
# considered more informative and are preferred for the narrative.
_HIGH_VALUE_TOKENS = frozenset(
    {
        "always", "never", "prefer", "prefers", "favorite", "favourite", "name",
        "named", "called", "use", "uses", "using", "working", "build", "building",
        "deadline", "important", "must", "need", "needs", "require", "requires",
        "live", "lives", "located", "born", "email", "phone", "address", "remember",
        "number", "want", "like", "love",
        "heiße", "heißt", "wohne", "wohnt", "lebe", "lebt", "mag", "magst",
        "bevorzuge", "arbeite", "arbeitet", "baue", "baut", "brauche", "braucht",
        "muss", "immer", "nie", "lieblings", "nummer", "zahl", "merken", "merk",
        "erinnern", "telefon", "adresse", "geboren", "deutsch", "englisch",
        "sprich", "sprechen", "möchte", "will",
    }
)

# Pure pleasantries / acknowledgements we never surface in a summary (DE + EN).
_FILLER = frozenset(
    {
        "ok", "okay", "alles klar", "thanks", "thank you", "danke", "danke dir",
        "vielen dank", "bitte", "gerne", "gern", "hi", "hello", "hey", "hallo",
        "yes", "no", "ja", "nein", "sure", "klar", "great", "cool", "nice",
        "super", "toll", "got it", "verstanden", "you're welcome", "of course",
        "perfekt", "stimmt",
    }
)

# Minimum word tokens for a statement to be worth including.
_MIN_TOKENS = 3
# How many distinct user statements we surface in the narrative.
_MAX_POINTS = 5


class Summarizer(ABC):
    """Abstract base class for episodic summarizers (pluggable backends)."""

    @abstractmethod
    def summarize(self, turns: list[dict]) -> str:
        """Summarize an ordered list of turns into a narrative string.

        Args:
            turns: ``[{"role", "content", "timestamp"}]`` in chronological
                order (oldest-first).

        Returns:
            A human-readable narrative summary. Non-empty for non-empty input.
        """
        raise NotImplementedError


class MockSummarizer(Summarizer):
    """Deterministic extractive summarizer (no LLM, no network).

    The summary is built by:

    1. selecting the **user** turns (the diary is about what the user did/said),
    2. splitting each into atomic statements and dropping filler / too-short
       ones,
    3. ranking the remaining statements (longer + high-value keywords score
       higher), deduplicating while preserving first-seen order,
    4. joining the top statements into a short narrative prefixed with the day,
       e.g. ``"On 2026-06-15 the user talked about: X; Y; Z."``.

    The output is fully deterministic and always non-empty for non-empty input.
    """

    def summarize(self, turns: list[dict]) -> str:
        """Return a deterministic extractive narrative for ``turns``."""
        if not turns:
            return ""

        day = self._derive_day(turns)
        user_statements = self._collect_user_statements(turns)

        if not user_statements:
            # Non-empty input must yield a non-empty summary even when the user
            # said only filler / very short things: fall back to a turn count.
            user_turns = sum(1 for t in turns if t.get("role") == "user")
            summary = (
                f"On {day} there were {len(turns)} dialogue turn(s) "
                f"({user_turns} from the user), with no substantive user "
                f"statements recorded."
            )
            logger.debug("MockSummarizer: fallback summary for %d turn(s)", len(turns))
            return summary

        ranked = self._rank(user_statements)[:_MAX_POINTS]
        points = "; ".join(ranked)
        summary = f"On {day} the user talked about: {points}."
        logger.debug(
            "MockSummarizer: summarized %d turn(s) into %d point(s)",
            len(turns), len(ranked),
        )
        return summary

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _derive_day(turns: list[dict]) -> str:
        """Derive the day label (``YYYY-MM-DD``) from the first turn's timestamp."""
        for turn in turns:
            ts = turn.get("timestamp") or ""
            if ts:
                # Timestamps are 'YYYY-MM-DD HH:MM:SS'; take the date portion.
                return ts.split(" ", 1)[0]
        return "the day"

    def _collect_user_statements(self, turns: list[dict]) -> list[str]:
        """Return informative, deduplicated user statements (first-seen order)."""
        seen: set[str] = set()
        statements: list[str] = []
        for turn in turns:
            if turn.get("role") != "user":
                continue
            for sentence in self._split_sentences(turn.get("content", "")):
                if not self._is_informative(sentence):
                    continue
                key = sentence.lower()
                if key in seen:
                    continue
                seen.add(key)
                statements.append(sentence)
        return statements

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split ``text`` into trimmed, non-empty atomic sentences."""
        if not text:
            return []
        parts = _SENTENCE_SPLIT_RE.split(text)
        return [p.strip().rstrip(".!?").strip() for p in parts if p and p.strip()]

    @staticmethod
    def _is_informative(sentence: str) -> bool:
        """Return ``True`` if ``sentence`` is worth surfacing in the diary."""
        normalized = sentence.strip().lower()
        if not normalized or normalized in _FILLER:
            return False
        tokens = re.findall(r"\w+", normalized, re.UNICODE)
        has_number = bool(re.search(r"\d", sentence))
        return len(tokens) >= _MIN_TOKENS or has_number

    @staticmethod
    def _rank(statements: list[str]) -> list[str]:
        """Stably rank statements by informativeness (descending).

        Ties preserve the original (chronological) order via the enumeration
        index, keeping the output fully deterministic.
        """
        def score(item: tuple[int, str]) -> tuple[int, int]:
            idx, sentence = item
            tokens = re.findall(r"\w+", sentence.lower(), re.UNICODE)
            value = 0
            if len(tokens) >= 8:
                value += 2
            elif len(tokens) >= 5:
                value += 1
            if _HIGH_VALUE_TOKENS.intersection(t.lower() for t in tokens):
                value += 2
            if re.search(r"\d", sentence):
                value += 1
            # Higher value first; for equal value, earlier index first.
            return (-value, idx)

        ordered = sorted(enumerate(statements), key=score)
        return [sentence for _, sentence in ordered]
