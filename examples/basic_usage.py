"""Minimal end-to-end usage — Nexus as the central memory of an LLM chat.

The smallest real loop, per user turn:

  1. RECALL  — ``assemble`` returns the standing directives + recalled facts
               for this query (Layers III/IV).
  2. BUILD   — compose ONE OpenAI ``messages`` array: a ``system`` message
               (base prompt + that recalled memory) first, then the durable
               conversation from ``memory.history()``, then the user turn.
  3. ANSWER  — your model replies (here a canned offline stub).
  4. WRITE   — ``ingest`` the exchange so the next turn remembers it.

Runs fully offline with the default ``HashingEmbedder`` (no network, no model
download). The demo DB is removed on exit. For token accounting see
``token_accounting.py``.

Run it::

    python examples/basic_usage.py
"""

from pathlib import Path

from nexus_memory import NexusMemory

DB_PATH = Path("nexus_memory.db")

# The plain, memory-less base prompt + a short framing for the injected memory.
SYSTEM_PROMPT = "You are a helpful personal assistant."
MEMORY_PREAMBLE = (
    "Use the standing directives and known facts below as trusted background.\n"
    "Apply them naturally; do not mention them."
)

# The simulated USER side: state a fact, set standing behavior, then ask again
# in different words to prove the fact is recalled by meaning.
USER_TURNS = [
    "My dog Mochi is a corgi and she loves the park.",
    "Always be concise.",
    "what breed is Mochi?",
]


def build_system(recall: dict) -> str:
    """Compose the system message: base prompt + Nexus's directives + facts."""
    parts = [SYSTEM_PROMPT, MEMORY_PREAMBLE]
    if recall["directives"]:
        parts.append("Standing directives:\n" + "\n".join(f"- {d}" for d in recall["directives"]))
    if recall["raw_facts"]:
        parts.append("Known facts:\n" + "\n".join(f"- {f['content']}" for f in recall["raw_facts"]))
    return "\n\n".join(parts)


def simulated_assistant(messages: list[dict]) -> str:
    """Stand-in for YOUR model. A real call would be e.g. OpenAI:

        client.chat.completions.create(model=..., messages=messages)

    The ``messages`` array already carries the system prompt as its first entry.
    """
    canned = {
        "My dog Mochi is a corgi and she loves the park.":
            "Got it - Mochi the corgi.",
        "Always be concise.":
            "Understood.",
        "what breed is Mochi?":
            "Mochi is a corgi.",
    }
    return canned.get(messages[-1]["content"], "Okay.")


def run_turn(memory: NexusMemory, user_msg: str) -> str:
    # 1. RECALL — directives + facts for THIS query (-> system message).
    recall = memory.process({"action": "assemble", "query": user_msg})

    # 2. BUILD — one OpenAI messages array: system first, then the durable
    #    history(), then the current user turn.
    messages = [{"role": "system", "content": build_system(recall)}]
    messages += memory.history(as_format="messages")
    messages.append({"role": "user", "content": user_msg})

    # 3. ANSWER — your model replies from the messages array.
    answer = simulated_assistant(messages)

    # 4. WRITE — persist the exchange so the next turn's history() has it.
    memory.process({"action": "ingest", "interaction": {"query": user_msg, "response": answer}})
    memory.wait()  # ingest is async; wait so the next history() sees this turn

    # What Nexus surfaced for this query (grows turn by turn).
    recalled = recall["directives"] + [f["content"] for f in recall["raw_facts"]]
    return (
        f"user:      {user_msg}\n"
        f"recalled:  {', '.join(recalled) if recalled else '(nothing yet)'}\n"
        f"assistant: {answer}"
    )


def main() -> None:
    memory = NexusMemory(db_path=str(DB_PATH))  # one local .db file, offline embedder
    try:
        # Each turn returns its text; main() prints it.
        for turn, user_msg in enumerate(USER_TURNS, start=1):
            print(f"########## TURN {turn} ##########")
            print(run_turn(memory, user_msg))
            print()
    finally:
        memory.close()
        # Remove the DB (and SQLite's -wal/-shm sidecars) so each run is clean.
        for path in (DB_PATH, *(DB_PATH.with_name(DB_PATH.name + s) for s in ("-wal", "-shm"))):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
