"""Tests for Layer I — Working Memory (CONTRACT-v2 §3).

Validates the volatile, in-RAM ring buffer :class:`WorkingMemory`:
``add_turn`` / ``add_interaction``, newest-last ``recent`` ordering, eviction at
a small ``max_turns``, ``snapshot`` shape, ``token_estimate``, ``clear``, and
thread-safety under concurrent appends.

Everything here is pure RAM (no DB, no embedder, no network), so the tests are
offline and deterministic. The one DB-touching check (orchestrator wiring) uses
``tmp_path`` via the shared ``config`` fixture and the offline HashingEmbedder
plus mock summarizer/detector defaults.
"""

from __future__ import annotations

import re
import threading

import pytest

from nexus_memory.layers.working.working import Turn, WorkingMemory


# --------------------------------------------------------------------------- #
# Construction / validation
# --------------------------------------------------------------------------- #
def test_starts_empty() -> None:
    wm = WorkingMemory(max_turns=10)
    assert len(wm) == 0
    assert wm.recent() == []
    assert wm.snapshot() == []
    assert wm.token_estimate() == 0


def test_max_turns_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WorkingMemory(max_turns=0)
    with pytest.raises(ValueError):
        WorkingMemory(max_turns=-3)


# --------------------------------------------------------------------------- #
# add_turn / add_interaction
# --------------------------------------------------------------------------- #
def test_add_turn_appends_with_role_and_content() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_turn("user", "hello")
    assert len(wm) == 1
    turn = wm.recent()[0]
    assert isinstance(turn, Turn)
    assert turn.role == "user"
    assert turn.content == "hello"
    # Timestamp uses the shared UTC "YYYY-MM-DD HH:MM:SS" format.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", turn.timestamp)


def test_add_interaction_adds_user_then_assistant() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_interaction("what is 2+2?", "4")
    turns = wm.recent()
    assert len(turns) == 2
    assert turns[0].role == "user" and turns[0].content == "what is 2+2?"
    assert turns[1].role == "assistant" and turns[1].content == "4"


# --------------------------------------------------------------------------- #
# recent() ordering
# --------------------------------------------------------------------------- #
def test_recent_is_newest_last() -> None:
    wm = WorkingMemory(max_turns=10)
    for i in range(5):
        wm.add_turn("user", f"msg-{i}")
    contents = [t.content for t in wm.recent()]
    assert contents == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]


def test_recent_n_returns_last_n_in_order() -> None:
    wm = WorkingMemory(max_turns=10)
    for i in range(6):
        wm.add_turn("user", f"msg-{i}")
    last_two = [t.content for t in wm.recent(2)]
    assert last_two == ["msg-4", "msg-5"]


def test_recent_none_returns_full_buffer() -> None:
    wm = WorkingMemory(max_turns=10)
    for i in range(3):
        wm.add_turn("user", str(i))
    assert len(wm.recent(None)) == 3


def test_recent_non_positive_returns_empty() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_turn("user", "x")
    assert wm.recent(0) == []
    assert wm.recent(-1) == []


def test_recent_n_larger_than_buffer_returns_all() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_turn("user", "a")
    wm.add_turn("assistant", "b")
    assert len(wm.recent(99)) == 2


# --------------------------------------------------------------------------- #
# Eviction at small max_turns
# --------------------------------------------------------------------------- #
def test_eviction_keeps_only_most_recent() -> None:
    wm = WorkingMemory(max_turns=3)
    for i in range(6):
        wm.add_turn("user", f"m{i}")
    # Buffer is bounded; only the 3 newest survive, oldest-first within window.
    assert len(wm) == 3
    contents = [t.content for t in wm.recent()]
    assert contents == ["m3", "m4", "m5"]


def test_add_interaction_eviction_with_odd_capacity() -> None:
    # Capacity 1 + two-turn interaction: only the assistant turn must remain.
    wm = WorkingMemory(max_turns=1)
    wm.add_interaction("hi", "hello there")
    assert len(wm) == 1
    only = wm.recent()[0]
    assert only.role == "assistant"
    assert only.content == "hello there"


# --------------------------------------------------------------------------- #
# snapshot() shape
# --------------------------------------------------------------------------- #
def test_snapshot_shape_is_list_of_dicts() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_interaction("q", "a")
    snap = wm.snapshot()
    assert isinstance(snap, list)
    assert all(isinstance(d, dict) for d in snap)
    for d in snap:
        assert set(d.keys()) == {"role", "content", "timestamp"}
    assert [d["role"] for d in snap] == ["user", "assistant"]
    assert [d["content"] for d in snap] == ["q", "a"]


def test_snapshot_is_a_copy_not_live_view() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_turn("user", "first")
    snap = wm.snapshot()
    wm.add_turn("user", "second")
    # The earlier snapshot must not retroactively grow.
    assert len(snap) == 1


# --------------------------------------------------------------------------- #
# token_estimate()
# --------------------------------------------------------------------------- #
def test_token_estimate_is_quarter_of_chars() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_turn("user", "a" * 40)      # 40 chars
    wm.add_turn("assistant", "b" * 20)  # 20 chars -> total 60
    assert wm.token_estimate() == 60 // 4


def test_token_estimate_only_counts_live_turns_after_eviction() -> None:
    wm = WorkingMemory(max_turns=2)
    wm.add_turn("user", "x" * 100)  # will be evicted
    wm.add_turn("user", "y" * 4)
    wm.add_turn("user", "z" * 4)
    # Only the two surviving 4-char turns count: 8 // 4 == 2.
    assert wm.token_estimate() == 2


# --------------------------------------------------------------------------- #
# clear()
# --------------------------------------------------------------------------- #
def test_clear_empties_buffer() -> None:
    wm = WorkingMemory(max_turns=10)
    wm.add_interaction("q", "a")
    assert len(wm) == 2
    wm.clear()
    assert len(wm) == 0
    assert wm.recent() == []
    assert wm.snapshot() == []
    assert wm.token_estimate() == 0
    # Still usable after clearing.
    wm.add_turn("user", "again")
    assert len(wm) == 1


# --------------------------------------------------------------------------- #
# Thread-safety: concurrent adds must not crash and count stays bounded
# --------------------------------------------------------------------------- #
def test_concurrent_adds_do_not_crash_and_are_bounded() -> None:
    max_turns = 50
    wm = WorkingMemory(max_turns=max_turns)
    n_threads = 16
    per_thread = 200
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        try:
            barrier.wait()  # maximize contention
            for i in range(per_thread):
                wm.add_turn("user", f"t{tid}-{i}")
                # Interleave reads to exercise the lock on both paths.
                wm.recent(5)
                wm.snapshot()
                wm.token_estimate()
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"concurrent access raised: {errors!r}"
    # All writes landed but the buffer never exceeds its bound.
    assert len(wm) == max_turns
    assert len(wm.snapshot()) == max_turns
    # The surviving turns are well-formed (no torn writes).
    for d in wm.snapshot():
        assert d["role"] == "user"
        assert re.fullmatch(r"t\d+-\d+", d["content"])


def test_concurrent_add_and_clear_stay_consistent() -> None:
    wm = WorkingMemory(max_turns=20)
    errors: list[BaseException] = []
    stop = threading.Event()

    def adder() -> None:
        try:
            while not stop.is_set():
                wm.add_interaction("hello", "world")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def clearer() -> None:
        try:
            for _ in range(500):
                wm.clear()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    a = threading.Thread(target=adder)
    c = threading.Thread(target=clearer)
    a.start()
    c.start()
    c.join()
    stop.set()
    a.join()

    assert not errors, f"concurrent add/clear raised: {errors!r}"
    # Whatever the interleaving, the invariant holds.
    assert 0 <= len(wm) <= wm.max_turns


# --------------------------------------------------------------------------- #
# Orchestrator wiring: ingest feeds working memory synchronously (offline)
# --------------------------------------------------------------------------- #
def test_orchestrator_ingest_populates_working_memory(config) -> None:
    """`process(ingest)` must add the interaction to working memory synchronously.

    Uses tmp_path-backed config, the offline HashingEmbedder, and the default
    mock summarizer/detector wired by NexusMemory. We only assert the working
    layer here; semantic ingest is covered by other suites.
    """
    from nexus_memory.core.orchestrator import NexusMemory

    # Pass db_path explicitly: the ctor copies it onto the config, so relying on
    # config.db_path alone would be overwritten by the "nexus_memory.db" default.
    mem = NexusMemory(db_path=config.db_path, config=config)
    try:
        result = mem.process(
            {
                "action": "ingest",
                "interaction": {
                    "query": "remember the sky is blue",
                    "response": "noted",
                },
            }
        )
        assert result["status"] == "processing"

        # Working memory is updated on the caller thread, before the async write.
        snap = mem.working_snapshot()
        assert [d["role"] for d in snap[-2:]] == ["user", "assistant"]
        assert snap[-2]["content"] == "remember the sky is blue"
        assert snap[-1]["content"] == "noted"

        # Let the background semantic write finish so close() is clean.
        mem.wait(timeout=10)
    finally:
        mem.close()
