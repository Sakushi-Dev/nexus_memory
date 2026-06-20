"""Configuration for the Nexus Memory module.

Holds the single :class:`NexusConfig` dataclass that every other component is
constructed from, plus :data:`DEFAULT_DIM`, the default embedding dimension.
"""

from __future__ import annotations

from dataclasses import dataclass

# Local-first default embedding dimension. MUST match the active embedder; the
# vector table dimension is fixed at table-creation time and cannot be changed
# without a full re-embed/migration.
DEFAULT_DIM: int = 768


@dataclass
class NexusConfig:
    """Central configuration for a Nexus Memory instance.

    All tunable parameters (scoring, writer dedup, cache, privacy, security)
    live here so they can be passed as a single object through the stack.
    """

    db_path: str = "nexus_memory.db"
    dim: int = DEFAULT_DIM

    # --- scoring ---
    decay_lambda: float = 0.01          # exp(-lambda * days_passed)
    min_score: float = 0.6
    default_top_k: int = 5

    # --- transparency / forget ---
    # Relevance floor for forget(query=...): the best KNN match is only deleted
    # when its cosine similarity (1 - distance) is >= this value. Without it
    # forget() would delete the nearest row on a non-empty store no matter how
    # unrelated the query is — a silent, irreversible data loss. Kept moderate so
    # genuine paraphrases still match while clearly unrelated queries do not.
    forget_min_similarity: float = 0.6

    # --- writer ---
    redundancy_threshold: float = 0.90  # cosine SIMILARITY above which a fact is a duplicate
    # When False (default), only the USER's turns become semantic facts; the
    # assistant's prose still goes to the episodic diary but does not flood the
    # vector store with conversational filler. Set True to also mine assistant
    # statements into semantic memory.
    semantic_include_assistant: bool = False

    # --- cache ---
    cache_size: int = 128
    cache_threshold: float = 0.95

    # --- privacy ---
    # OFF by default: on the local-first path nothing leaves the machine, so
    # masking would only destroy useful memory (e.g. the user's own name). Turn
    # ON only when embedding via an EXTERNAL API (OpenAI etc.). See privacy.py.
    pii_filter_enabled: bool = False

    # --- security (optional path) ---
    encryption_key: bytes | None = None

    # --- working memory (Layer I, volatile RAM) ---
    working_memory_max_turns: int = 50  # volatile RAM buffer capacity (turns)

    # --- episodic (Layer II, diary) ---
    episodic_recent_turns: int = 6      # how many recent turns assemble injects
    episodic_enabled: bool = True

    # --- history accessor (NexusMemory.history) ---
    history_truncation: str = "turns"   # "turns" | "tokens"
    history_max_turns: int = 20         # default cap for "turns" mode
    history_token_budget: int = 2000    # default budget for "tokens" mode

    # --- procedural (Layer IV, behavioral rules) ---
    procedural_max_directives: int = 12  # cap active directives injected into context
    procedural_enabled: bool = True

    # --- consolidation (inter-layer transfer) ---
    auto_consolidate: bool = True       # ingest also logs episodic + detects rules

    def __post_init__(self) -> None:
        """Validate the history-accessor truncation mode."""
        if self.history_truncation not in {"turns", "tokens"}:
            raise ValueError(
                "history_truncation must be 'turns' or 'tokens', "
                f"got {self.history_truncation!r}"
            )
