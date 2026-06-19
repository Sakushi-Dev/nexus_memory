"""Tests for the unified ``NexusMemory.tokens()`` accessor.

``tokens()`` counts the **actual LLM round-trip** — the request ``messages`` array
plus the model ``response`` — and splits it by *section* (not storage layer):

* ``system``  — all ``role == "system"`` content (base prompt + injected facts);
* ``input``   — all ``role in ("user", "assistant")`` content (history + turn);
* ``output``  — the ``response`` text;
* ``full``    — ``system`` + ``input`` + ``output``.

Covered: each scope, the default ``len(s)//4`` heuristic + a custom ``counter``,
a list returning a ``{scope: int}`` dict + ``"total"``, empty/missing inputs, and
the unknown-scope error.

Offline/deterministic: temp-file SQLite + the default HashingEmbedder.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus_memory.core.orchestrator import NexusMemory
from nexus_memory.core.xml_format import estimate_tokens


@pytest.fixture
def memory():
    """A NexusMemory on a unique temp-file db, closed on teardown."""
    with tempfile.TemporaryDirectory() as tmp:
        mem = NexusMemory(db_path=str(Path(tmp) / "tokens_test.db"))
        try:
            yield mem
        finally:
            mem.close()


# A small OpenAI-style request: one system message, a 2-turn history, the live
# user turn. ``response`` is the model's reply (the output).
MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant. Known fact: keys in bowl."},
    {"role": "user", "content": "where are my keys?"},
    {"role": "assistant", "content": "In the blue bowl."},
    {"role": "user", "content": "remind me again"},
]
RESPONSE = "They are in the blue bowl on the counter."


# --------------------------------------------------------------------------- #
# section scopes under the default heuristic
# --------------------------------------------------------------------------- #
def test_system_counts_only_system_messages(memory):
    """system sums the role=='system' content, nothing else."""
    want = estimate_tokens(MESSAGES[0]["content"])
    assert memory.tokens("system", messages=MESSAGES) == want


def test_input_counts_user_and_assistant(memory):
    """input sums every non-system (user + assistant) message."""
    want = sum(
        estimate_tokens(m["content"])
        for m in MESSAGES
        if m["role"] in ("user", "assistant")
    )
    assert memory.tokens("input", messages=MESSAGES) == want


def test_output_counts_the_response(memory):
    """output is the response text only."""
    assert memory.tokens("output", response=RESPONSE) == estimate_tokens(RESPONSE)


def test_full_is_system_plus_input_plus_output(memory):
    """full == system + input + output."""
    full = memory.tokens("full", messages=MESSAGES, response=RESPONSE)
    parts = (
        memory.tokens("system", messages=MESSAGES)
        + memory.tokens("input", messages=MESSAGES)
        + memory.tokens("output", response=RESPONSE)
    )
    assert full == parts


# --------------------------------------------------------------------------- #
# list breakdown + custom counter
# --------------------------------------------------------------------------- #
def test_list_returns_dict_with_total(memory):
    """A list of scopes returns {scope: int} plus a summed 'total'."""
    out = memory.tokens(
        ["system", "input", "output"], messages=MESSAGES, response=RESPONSE
    )
    assert set(out) == {"system", "input", "output", "total"}
    assert out["total"] == out["system"] + out["input"] + out["output"]


def test_partial_list_total_excludes_unlisted_scopes(memory):
    """'total' sums ONLY the listed scopes (here: not system)."""
    out = memory.tokens(["input", "output"], messages=MESSAGES, response=RESPONSE)
    assert set(out) == {"input", "output", "total"}
    assert out["total"] == out["input"] + out["output"]
    # MESSAGES has a non-empty system message, so a total that wrongly folded in
    # all sections would exceed input+output — guard against that regression.
    assert out["total"] < memory.tokens("full", messages=MESSAGES, response=RESPONSE)


def test_custom_counter_one_per_message(memory):
    """A counter of 1/string makes system/input the message counts."""
    one = lambda s: 1  # noqa: E731 - terse deterministic counter
    assert memory.tokens("system", messages=MESSAGES, counter=one) == 1  # 1 system msg
    assert memory.tokens("input", messages=MESSAGES, counter=one) == 3   # 3 user/assistant
    assert memory.tokens("output", response=RESPONSE, counter=one) == 1


# --------------------------------------------------------------------------- #
# empty / missing inputs + errors
# --------------------------------------------------------------------------- #
def test_missing_inputs_are_zero(memory):
    """No messages -> system/input 0; no response -> output 0."""
    assert memory.tokens("system") == 0
    assert memory.tokens("input") == 0
    assert memory.tokens("output") == 0
    assert memory.tokens("full") == 0


def test_unknown_scope_raises(memory):
    """An unknown scope is a programming error -> ValueError."""
    with pytest.raises(ValueError):
        memory.tokens("bogus", messages=MESSAGES)
