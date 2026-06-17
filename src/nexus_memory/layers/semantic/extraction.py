"""Fact extraction for the Nexus Memory writer loop.

A real deployment would call a local small language model (Phi/Gemma) with
grammar-constrained decoding to emit strict JSON. For local-first, dependency-
free, deterministic operation (and for tests), :class:`MockFactExtractor`
stands in for that SLM: it splits an interaction into atomic sentences, keeps
the informative ones, and assigns a heuristic importance.

Every extractor validates its output against the :class:`ExtractedFacts`
Pydantic model (defined in :mod:`nexus_memory.models`), so extraction *always*
returns JSON-valid facts.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from ...core.models import ExtractedFacts, FactItem

logger = logging.getLogger(__name__)

# Sentence splitter: break on sentence-final punctuation or newlines.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")

# Tokens that signal a higher-value, durable fact about the user/world.
_HIGH_VALUE_TOKENS = frozenset(
    {
        "always",
        "never",
        "prefer",
        "prefers",
        "preferred",
        "favorite",
        "favourite",
        "name",
        "named",
        "called",
        "use",
        "uses",
        "using",
        "working",
        "build",
        "building",
        "deadline",
        "important",
        "must",
        "need",
        "needs",
        "require",
        "requires",
        "live",
        "lives",
        "located",
        "born",
        "email",
        "phone",
        "address",
    }
)

# Low-information conversational filler we drop entirely.
_FILLER_SENTENCES = frozenset(
    {
        "ok",
        "okay",
        "thanks",
        "thank you",
        "hi",
        "hello",
        "hey",
        "yes",
        "no",
        "sure",
        "great",
        "cool",
        "nice",
        "got it",
        "you're welcome",
        "of course",
    }
)

# Minimum number of word tokens for a sentence to be considered informative.
_MIN_TOKENS = 3
# Importance is clamped to the contract's 1..10 range.
_MIN_IMPORTANCE = 1
_MAX_IMPORTANCE = 10
_BASE_IMPORTANCE = 4


class FactExtractor(ABC):
    """Abstract base class for fact extractors (pluggable SLM backends)."""

    @abstractmethod
    def extract(self, query: str, response: str) -> list[dict]:
        """Extract atomic facts from an interaction.

        Returns a list of ``{"content": str, "importance": int}`` dicts where
        ``importance`` is in ``[1, 10]``. Implementations MUST return output
        that validates against :class:`ExtractedFacts`.
        """
        raise NotImplementedError


class MockFactExtractor(FactExtractor):
    """Deterministic stand-in for a local SLM (e.g. Phi-4 / Gemma).

    The extraction pipeline is intentionally simple and fully deterministic so
    that the writer loop is testable without a model download or network:

    1. Concatenate the user query and assistant response.
    2. Split into atomic sentences.
    3. Drop filler / too-short sentences.
    4. Assign a heuristic importance based on length and high-value keywords.
    5. Validate the whole batch against :class:`ExtractedFacts`.

    Output always validates against the Pydantic schema, so callers can rely on
    receiving JSON-valid facts.
    """

    def __init__(self, base_importance: int = _BASE_IMPORTANCE) -> None:
        self._base_importance = self._clamp(base_importance)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def extract(self, query: str, response: str) -> list[dict]:
        """Extract validated facts from a ``(query, response)`` pair."""
        candidates: list[FactItem] = []
        for source in (query, response):
            for sentence in self._split_sentences(source):
                if not self._is_informative(sentence):
                    continue
                candidates.append(
                    FactItem(
                        content=sentence,
                        importance=self._score_importance(sentence),
                    )
                )

        # Validate the batch through the Pydantic container. This is the
        # contract's guarantee: extraction always yields JSON-valid facts.
        extracted = ExtractedFacts(facts=candidates)
        result = [item.model_dump() for item in extracted.facts]
        logger.debug(
            "MockFactExtractor produced %d fact(s) from interaction", len(result)
        )
        return result

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split ``text`` into trimmed, non-empty atomic sentences."""
        if not text:
            return []
        parts = _SENTENCE_SPLIT_RE.split(text)
        return [p.strip().rstrip(".!?").strip() for p in parts if p and p.strip()]

    @staticmethod
    def _is_informative(sentence: str) -> bool:
        """Return ``True`` if ``sentence`` looks worth remembering."""
        normalized = sentence.strip().lower()
        if not normalized or normalized in _FILLER_SENTENCES:
            return False
        tokens = re.findall(r"[a-z0-9]+", normalized)
        return len(tokens) >= _MIN_TOKENS

    def _score_importance(self, sentence: str) -> int:
        """Heuristically score importance in ``[1, 10]`` for ``sentence``."""
        tokens = re.findall(r"[a-z0-9]+", sentence.lower())
        importance = self._base_importance
        # Longer, denser sentences tend to carry more information.
        if len(tokens) >= 8:
            importance += 2
        elif len(tokens) >= 5:
            importance += 1
        # Boost for durable/high-value signal words.
        if _HIGH_VALUE_TOKENS.intersection(tokens):
            importance += 2
        return self._clamp(importance)

    @staticmethod
    def _clamp(value: int) -> int:
        """Clamp ``value`` into the contract's ``[1, 10]`` importance range."""
        return max(_MIN_IMPORTANCE, min(_MAX_IMPORTANCE, int(value)))


# --------------------------------------------------------------------------- #
# Speaker-aware extractor (default)
# --------------------------------------------------------------------------- #
# An interaction is a (user query, assistant response) pair, so the extractor
# *knows* who said each sentence. Throwing that away — as a naive splitter does —
# makes the assistant's own utterances indistinguishable from the user's once
# they are stored, so a downstream model later misremembers who said what (e.g.
# attributing its own promise back to the user). The speaker-aware extractor
# keeps attribution and drops the assistant's questions/filler, which are never
# durable knowledge about the user.

# Pure pleasantries / acknowledgements we never store as facts (DE + EN).
_FILLER_BILINGUAL = frozenset(
    {
        "ok", "okay", "alles klar", "thanks", "thank you", "danke", "danke dir",
        "vielen dank", "bitte", "gerne", "gern geschehen", "gern", "hi", "hello",
        "hey", "hallo", "yes", "no", "ja", "nein", "sure", "klar", "natürlich",
        "klaro", "great", "cool", "nice", "schön", "super", "toll", "got it",
        "verstanden", "you're welcome", "of course", "freut mich", "passt",
        "perfekt", "haha", "stimmt", "ups",
    }
)

# Durable / high-value signal tokens (DE + EN) -> importance boost.
_HIGH_VALUE_BILINGUAL = _HIGH_VALUE_TOKENS | frozenset(
    {
        "remember", "number",
        "heiße", "heißt", "wohne", "wohnt", "lebe", "lebt", "mag", "magst",
        "bevorzuge", "arbeite", "arbeitet", "baue", "baut", "brauche", "braucht",
        "muss", "immer", "nie", "lieblings", "nummer", "zahl", "merken", "merk",
        "erinnern", "telefon", "adresse", "geboren", "deutsch", "englisch",
        "sprich", "sprechen", "sprichst",
    }
)


class SpeakerAwareExtractor(FactExtractor):
    """Default extractor: attribute facts to their speaker, drop noise.

    For each interaction:

    * the **user** query and the **assistant** response are processed separately,
    * every kept fact is prefixed with ``"User: "`` or ``"Assistant: "`` so the
      stored memory is unambiguous about who said it,
    * the assistant's **questions** and conversational **filler** are dropped
      (they are never durable facts about the user); user questions are kept
      because they may carry the actual information (a number, a standing
      request),
    * a short fragment is kept when it contains a **number** (so
      ``"...remember? 3658?"`` does not lose the ``3658``).

    Output always validates against :class:`ExtractedFacts`.
    """

    def __init__(self, include_assistant: bool = False) -> None:
        """Configure speaker scope.

        Args:
            include_assistant: When ``True``, the assistant's declarative
                statements are also mined into semantic memory. Default
                ``False`` — only the user's turns become facts (the assistant's
                prose still lives in the episodic diary), which keeps the vector
                store free of conversational filler.
        """
        self._include_assistant = include_assistant

    def extract(self, query: str, response: str) -> list[dict]:
        """Extract speaker-attributed, validated facts from an interaction."""
        facts: list[FactItem] = self._from(query, "User", keep_questions=True)
        if self._include_assistant:
            facts += self._from(response, "Assistant", keep_questions=False)
        extracted = ExtractedFacts(facts=facts)
        result = [item.model_dump() for item in extracted.facts]
        logger.debug(
            "SpeakerAwareExtractor produced %d fact(s) from interaction", len(result)
        )
        return result

    # ------------------------------------------------------------------ #
    def _from(self, text: str, speaker: str, keep_questions: bool) -> list[FactItem]:
        out: list[FactItem] = []
        for raw in self._split_sentences(text):
            sentence = raw.strip()
            normalized = sentence.lower().rstrip("?!.").strip()
            if not normalized or normalized in _FILLER_BILINGUAL:
                continue
            if sentence.endswith("?") and not keep_questions:
                continue
            tokens = re.findall(r"\w+", normalized, re.UNICODE)
            has_number = bool(re.search(r"\d", sentence))
            if len(tokens) < _MIN_TOKENS and not has_number:
                continue
            out.append(
                FactItem(
                    content=f"{speaker}: {sentence}",
                    importance=self._score_importance(speaker, tokens, has_number),
                )
            )
        return out

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split ``text`` into trimmed, non-empty sentences (keeps punctuation)."""
        if not text:
            return []
        return [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p and p.strip()]

    @staticmethod
    def _score_importance(speaker: str, tokens: list[str], has_number: bool) -> int:
        """Heuristic importance in ``[1, 10]`` (user statements weigh more)."""
        importance = 5 if speaker == "User" else 3
        if len(tokens) >= 8:
            importance += 2
        elif len(tokens) >= 5:
            importance += 1
        if has_number:
            importance += 2
        if _HIGH_VALUE_BILINGUAL.intersection(t.lower() for t in tokens):
            importance += 2
        return max(_MIN_IMPORTANCE, min(_MAX_IMPORTANCE, int(importance)))
