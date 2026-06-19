"""Basic end-to-end usage — building a NATIVE LLM request with Nexus.

Wired for the **OpenAI Chat Completions** API (``chat.completions.create``):
everything lives in a single ``messages`` array, with the system prompt as the
first ``{"role": "system", ...}`` entry — OpenAI has no separate ``system``
parameter.

Runs fully offline with the default ``HashingEmbedder`` (no network, no model
download). It shows the recommended split when Nexus is your central memory:

* **system message**  <- standard prompt + the standing directives and recalled
  facts from ``assemble`` (Layers III/IV — what the model should *know/obey*),
  prepended as the first message;
* **chat messages**   <- the durable conversation from ``memory.history()`` as a
  native ``[{role, content}]`` array (Layer II/I — the actual turn-by-turn chat),
  plus the current user message.

That avoids duplicating the dialogue (we do NOT also inject ``<recent_dialogue>``),
keeps roles native (the model sees its own prior ``assistant`` turns), and lets
Nexus own the history — ``history()`` reads the durable episodic store, so it
survives a restart.

After each turn we also print ``memory.tokens(...)`` — token accounting over the
**actual round-trip**, by section: ``system`` = the whole system message (base
prompt + Nexus's injected directives/facts), ``input`` = the rest of the prompt
(history + this user turn), ``output`` = the model's reply. We pass the real
``messages`` array and ``response`` so it counts exactly what crossed the wire.

Nexus never calls an LLM itself, so the "assistant" here is a canned stub.

Run it::

    python examples/basic_usage.py
"""

from pathlib import Path
from pprint import pprint

from nexus_memory import NexusMemory

# Where this demo keeps its one local SQLite file. Removed on exit so every run
# starts from an empty store (otherwise history() would accumulate across runs).
DB_PATH = Path("nexus_memory.db")

# The plain, memory-less base prompt + a short framing for the injected memory.
SYSTEM_PROMPT = "You are a helpful personal assistant."
MEMORY_PREAMBLE = (
    "Use the standing directives and known facts below as trusted background.\n"
    "Apply them naturally; do not mention them. The conversation itself follows\n"
    "as normal chat messages."
)

# The simulated USER side: state a fact, set standing behavior, then ask again
# in different words to prove the fact is recalled by meaning.
USER_TURNS = [
    "I keep my house keys in the blue ceramic bowl on the counter.",
    "Always answer in English and keep answers concise.",
    "remind me where my house keys are",
]


def build_system(recall: dict) -> str:
    """Compose the system prompt from the standard prompt + Nexus's directives/facts.

    Directives (Layer IV) and recalled facts (Layer III) are *knowledge/behavior*,
    so they belong in the ``system`` message — not in the chat transcript.
    """
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
    Here we return canned, deterministic replies so the demo stays offline.
    """
    last_user = messages[-1]["content"]
    canned = {
        "I keep my house keys in the blue ceramic bowl on the counter.":
            "Good to know - I'll remember that.",
        "Always answer in English and keep answers concise.":
            "Understood.",
        "remind me where my house keys are":
            "They're in the blue ceramic bowl on the counter.",
    }
    return canned.get(last_user, "Okay.")


def main() -> None:
    memory = NexusMemory(db_path=str(DB_PATH))  # one local .db file, offline embedder
    try:
        for turn, user_msg in enumerate(USER_TURNS, start=1):
            print(f"\n########## TURN {turn} -- user: {user_msg!r} ##########")

            # 1. RECALL — directives + facts for THIS query (-> system message).
            recall = memory.process({"action": "assemble", "query": user_msg})
            system = build_system(recall)

            # 2. NATIVE HISTORY — OpenAI style: one messages array. The system
            #    prompt is the FIRST entry (OpenAI has no separate `system` arg),
            #    then the durable conversation from memory.history(), then the
            #    current user turn. memory.history() IS the native source here.
            messages = [{"role": "system", "content": system}]
            messages += memory.history(as_format="messages")
            messages.append({"role": "user", "content": user_msg})

            # 3. Show exactly what a real API call would receive.
            print("\n>>> messages (OpenAI chat.completions, system-first + history + current turn):")
            pprint(messages, sort_dicts=False, width=100)

            # 4. THINK — your model answers from the messages array.
            answer = simulated_assistant(messages)
            print(f"\n--- simulated assistant: {answer!r}")

            # 5. TOKENS — counted over the ACTUAL round-trip, by section:
            #    system  = the whole system message (base prompt + Nexus's injected
            #              directives/facts), input = the rest of the prompt
            #              (history + this user turn), output = the model's reply.
            usage = memory.tokens(
                ["system", "input", "output"], messages=messages, response=answer
            )
            print(f">>> Nexus tokens -- system:{usage['system']} input:{usage['input']} "
                  f"output:{usage['output']} total:{usage['total']}")

            # 6. WRITE — persist the exchange so the next turn's history() has it.
            memory.process({
                "action": "ingest",
                "interaction": {"query": user_msg, "response": answer},
            })
            memory.wait()  # ingest is async; wait so history() sees the new turn

        # Truncation knobs: turn-bounded or (approximate) token-bounded windows.
        # Both are done by NEXUS — it counts and trims, not the caller.
        print("\n########## history() truncation ##########")
        print("last 2 turns (turn-bounded):")
        pprint(memory.history(max_turns=2), sort_dicts=False, width=100)

        # Token-bounded: Nexus keeps the newest turns that fit the budget, using
        # its own internal len//4 counter (same as working.token_estimate).
        print("\nlast ~20 tokens (token-bounded, Nexus trims):")
        pprint(memory.history(max_tokens=20), sort_dicts=False, width=100)

        print("\nas a plain string (e.g. to embed in a system prompt):")
        print(memory.history(max_turns=2, as_format="string"))
        print("---")
        print(memory.history(max_tokens=20, as_format="string"))
    finally:
        memory.close()
        # Remove the DB (and SQLite's -wal/-shm sidecars) so each run is clean.
        for path in (DB_PATH, *(DB_PATH.with_name(DB_PATH.name + s) for s in ("-wal", "-shm"))):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
