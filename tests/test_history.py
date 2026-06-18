"""Tests for the unified ``NexusMemory.history()`` accessor (Phase 5).

Covers every case from ``.plans/history-feature.md`` Phase 5:

* turns truncation: cap respected + chronological order;
* tokens truncation: default ``len(s)//4`` heuristic + a custom ``token_counter``;
* role filter: user-only, assistant-only, both (``None``);
* formats: ``messages`` / ``turns`` / ``string`` (+ a custom template);
* durability: write with one instance, reopen a fresh instance on the SAME
  temp ``db_path``, ``history()`` still returns the turns (episodic-backed);
* working fallback: ``NexusConfig(episodic_enabled=False)`` reads the volatile
  buffer within the session;
* empty store: ``[]`` for messages/turns, ``""`` for string;
* precedence: ``max_tokens`` overrides ``max_turns``; explicit args override
  config defaults.

Everything is offline/deterministic: temp-file SQLite ``db_path`` and the default
:class:`HashingEmbedder`. ``memory.wait()`` is called after each ingest so the
async episodic write has landed before we read history.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus_memory.core.config import NexusConfig
from nexus_memory.core.orchestrator import NexusMemory


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db_path():
    """A unique temp-file SQLite path, removed (with WAL siblings) on teardown."""
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "history_test.db")


def _ingest(memory: NexusMemory, query: str, response: str) -> None:
    """Ingest a single interaction and block until the async write lands."""
    resp = memory.process(
        {"action": "ingest", "interaction": {"query": query, "response": response}}
    )
    assert resp["status"] == "processing"
    memory.wait()


def _seed(memory: NexusMemory, n: int = 3) -> None:
    """Seed ``n`` user/assistant interaction pairs with predictable content."""
    for i in range(n):
        _ingest(memory, f"user message {i}", f"assistant reply {i}")


# --------------------------------------------------------------------------- #
# turns truncation: cap respected + chronological order
# --------------------------------------------------------------------------- #
def test_turns_truncation_cap_and_chronological_order(temp_db_path):
    """max_turns keeps exactly the last N turns, newest-last."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=3)  # 6 turns total (u0,a0,u1,a1,u2,a2)

        msgs = memory.history(max_turns=3)
        assert [m["content"] for m in msgs] == [
            "assistant reply 1",
            "user message 2",
            "assistant reply 2",
        ]
        # The most recent turn is last (chronological order preserved).
        assert msgs[-1]["content"] == "assistant reply 2"
        assert len(msgs) == 3
    finally:
        memory.close()


def test_turns_truncation_cap_larger_than_history(temp_db_path):
    """A cap above the available turn count returns the whole history."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=2)  # 4 turns
        msgs = memory.history(max_turns=100)
        assert len(msgs) == 4
        assert msgs[0]["content"] == "user message 0"
        assert msgs[-1]["content"] == "assistant reply 1"
    finally:
        memory.close()


# --------------------------------------------------------------------------- #
# tokens truncation: default heuristic + custom token_counter
# --------------------------------------------------------------------------- #
def test_tokens_truncation_default_heuristic(temp_db_path):
    """max_tokens keeps the newest suffix that fits the len(s)//4 budget."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        # Each content is "user message N"/"assistant reply N": ~14-15 chars
        # -> ~3 tokens each under len(s)//4. A budget of 7 fits 2 turns (3+3=6)
        # but not a 3rd (would be 9 > 7).
        _seed(memory, n=3)
        msgs = memory.history(max_tokens=7)
        assert [m["content"] for m in msgs] == [
            "user message 2",
            "assistant reply 2",
        ]
    finally:
        memory.close()


def test_tokens_truncation_custom_token_counter(temp_db_path):
    """A custom token_counter (1 token per turn) governs the budget."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=4)  # 8 turns

        # One "token" per turn -> budget of 3 keeps the last 3 turns.
        msgs = memory.history(max_tokens=3, token_counter=lambda s: 1)
        assert len(msgs) == 3
        assert [m["content"] for m in msgs] == [
            "assistant reply 2",
            "user message 3",
            "assistant reply 3",
        ]
    finally:
        memory.close()


# --------------------------------------------------------------------------- #
# role filter: user-only, assistant-only, both
# --------------------------------------------------------------------------- #
def test_role_filter_user_only(temp_db_path):
    """role='user' keeps only user turns."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=3)
        msgs = memory.history(role="user", max_turns=10)
        assert all(m["role"] == "user" for m in msgs)
        assert [m["content"] for m in msgs] == [
            "user message 0",
            "user message 1",
            "user message 2",
        ]
    finally:
        memory.close()


def test_role_filter_assistant_only(temp_db_path):
    """role='assistant' keeps only assistant turns."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=3)
        msgs = memory.history(role="assistant", max_turns=10)
        assert all(m["role"] == "assistant" for m in msgs)
        assert [m["content"] for m in msgs] == [
            "assistant reply 0",
            "assistant reply 1",
            "assistant reply 2",
        ]
    finally:
        memory.close()


def test_role_filter_both_default(temp_db_path):
    """role=None (default) keeps both user and assistant turns interleaved."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=2)
        msgs = memory.history(max_turns=10)
        assert [m["role"] for m in msgs] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
    finally:
        memory.close()


def test_role_filter_invalid_raises(temp_db_path):
    """An unsupported role raises ValueError."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        with pytest.raises(ValueError):
            memory.history(role="system")
    finally:
        memory.close()


# --------------------------------------------------------------------------- #
# formats: messages / turns / string (+ custom template)
# --------------------------------------------------------------------------- #
def test_format_messages(temp_db_path):
    """messages format -> [{role, content}] with exactly those keys."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=1)
        msgs = memory.history(as_format="messages", max_turns=10)
        assert msgs == [
            {"role": "user", "content": "user message 0"},
            {"role": "assistant", "content": "assistant reply 0"},
        ]
        assert all(set(m.keys()) == {"role", "content"} for m in msgs)
    finally:
        memory.close()


def test_format_turns_includes_timestamp(temp_db_path):
    """turns format -> [{role, content, timestamp}] with a non-empty timestamp."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=1)
        turns = memory.history(as_format="turns", max_turns=10)
        assert all(set(t.keys()) == {"role", "content", "timestamp"} for t in turns)
        assert all(t["timestamp"] for t in turns)
        assert [t["content"] for t in turns] == [
            "user message 0",
            "assistant reply 0",
        ]
    finally:
        memory.close()


def test_format_string_default_template(temp_db_path):
    """string format -> newline-joined 'role: content' transcript by default."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=1)
        text = memory.history(as_format="string", max_turns=10)
        assert text == "user: user message 0\nassistant: assistant reply 0"
    finally:
        memory.close()


def test_format_string_custom_template(temp_db_path):
    """string format honours a custom per-turn template."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=1)
        text = memory.history(
            as_format="string",
            template="<{role}> {content}",
            max_turns=10,
        )
        assert text == "<user> user message 0\n<assistant> assistant reply 0"
    finally:
        memory.close()


def test_format_invalid_raises(temp_db_path):
    """An unsupported as_format raises ValueError."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        with pytest.raises(ValueError):
            memory.history(as_format="xml")
    finally:
        memory.close()


# --------------------------------------------------------------------------- #
# durability: reopen a fresh instance on the SAME db_path
# --------------------------------------------------------------------------- #
def test_durability_across_instances(temp_db_path):
    """History written by one instance is visible from a fresh one (episodic)."""
    memory1 = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory1, n=2)
    finally:
        memory1.close()

    # Brand-new instance on the SAME on-disk db.
    memory2 = NexusMemory(db_path=temp_db_path)
    try:
        msgs = memory2.history(max_turns=10)
        assert [m["content"] for m in msgs] == [
            "user message 0",
            "assistant reply 0",
            "user message 1",
            "assistant reply 1",
        ]
    finally:
        memory2.close()


# --------------------------------------------------------------------------- #
# working fallback: episodic_enabled=False reads the volatile buffer
# --------------------------------------------------------------------------- #
def test_working_fallback_within_session(temp_db_path):
    """With episodic disabled, history() reads the in-RAM working buffer."""
    config = NexusConfig(db_path=temp_db_path, episodic_enabled=False)
    memory = NexusMemory(db_path=temp_db_path, config=config)
    try:
        _seed(memory, n=2)
        msgs = memory.history(max_turns=10)
        assert [m["content"] for m in msgs] == [
            "user message 0",
            "assistant reply 0",
            "user message 1",
            "assistant reply 1",
        ]
    finally:
        memory.close()


def test_working_fallback_not_durable_across_instances(temp_db_path):
    """The working buffer is volatile: a fresh disabled instance sees nothing."""
    config1 = NexusConfig(db_path=temp_db_path, episodic_enabled=False)
    memory1 = NexusMemory(db_path=temp_db_path, config=config1)
    try:
        _seed(memory1, n=2)
        assert memory1.history(max_turns=10)  # present in this session
    finally:
        memory1.close()

    config2 = NexusConfig(db_path=temp_db_path, episodic_enabled=False)
    memory2 = NexusMemory(db_path=temp_db_path, config=config2)
    try:
        # New process-equivalent: the RAM buffer is empty.
        assert memory2.history(max_turns=10) == []
    finally:
        memory2.close()


# --------------------------------------------------------------------------- #
# empty store -> [] / ""
# --------------------------------------------------------------------------- #
def test_empty_store_messages_and_turns(temp_db_path):
    """An empty store returns [] for messages and turns formats."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        assert memory.history(as_format="messages") == []
        assert memory.history(as_format="turns") == []
    finally:
        memory.close()


def test_empty_store_string(temp_db_path):
    """An empty store returns '' for the string format."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        assert memory.history(as_format="string") == ""
    finally:
        memory.close()


def test_non_positive_budget_is_empty(temp_db_path):
    """max_turns<=0 and max_tokens<=0 both truncate to empty."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=2)
        assert memory.history(max_turns=0) == []
        assert memory.history(max_tokens=0) == []
    finally:
        memory.close()


# --------------------------------------------------------------------------- #
# precedence: max_tokens > max_turns; explicit args > config
# --------------------------------------------------------------------------- #
def test_precedence_max_tokens_over_max_turns(temp_db_path):
    """When both are given, max_tokens governs (turns mode is ignored)."""
    memory = NexusMemory(db_path=temp_db_path)
    try:
        _seed(memory, n=4)  # 8 turns
        # max_turns=8 would keep everything, but max_tokens (1 token/turn,
        # budget 2) wins and keeps only the last 2 turns.
        msgs = memory.history(
            max_turns=8, max_tokens=2, token_counter=lambda s: 1
        )
        assert len(msgs) == 2
        assert [m["content"] for m in msgs] == [
            "user message 3",
            "assistant reply 3",
        ]
    finally:
        memory.close()


def test_precedence_explicit_turns_over_config(temp_db_path):
    """An explicit max_turns overrides the config's turns-mode default."""
    # Config default would keep 2 turns; explicit max_turns=4 overrides it.
    config = NexusConfig(
        db_path=temp_db_path, history_truncation="turns", history_max_turns=2
    )
    memory = NexusMemory(db_path=temp_db_path, config=config)
    try:
        _seed(memory, n=3)  # 6 turns
        assert len(memory.history()) == 2  # config default
        assert len(memory.history(max_turns=4)) == 4  # explicit override
    finally:
        memory.close()


def test_config_tokens_mode_default(temp_db_path):
    """With no explicit args, history_truncation='tokens' uses the token budget."""
    # 1 token per turn isn't possible without a counter, so use a tiny char
    # budget: each content ~3 tokens (len//4); budget 7 keeps the last 2 turns.
    config = NexusConfig(
        db_path=temp_db_path,
        history_truncation="tokens",
        history_token_budget=7,
    )
    memory = NexusMemory(db_path=temp_db_path, config=config)
    try:
        _seed(memory, n=3)
        msgs = memory.history()  # no explicit truncation args
        assert [m["content"] for m in msgs] == [
            "user message 2",
            "assistant reply 2",
        ]
    finally:
        memory.close()


def test_config_invalid_truncation_raises(temp_db_path):
    """NexusConfig validates history_truncation at construction."""
    with pytest.raises(ValueError):
        NexusConfig(db_path=temp_db_path, history_truncation="bytes")
