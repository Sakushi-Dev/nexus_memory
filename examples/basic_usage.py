"""Basic end-to-end usage — building a NATIVE LLM request with Nexus.

Runs fully offline with the default ``HashingEmbedder`` (no network, no model
download). It shows the recommended split when Nexus is your central memory:

* **system prompt**  <- standard prompt + the standing directives and recalled
  facts from ``assemble`` (Layers III/IV — what the model should *know/obey*);
* **messages**       <- the durable conversation from ``memory.history()`` as a
  native ``[{role, content}]`` array (Layer II/I — the actual turn-by-turn chat),
  plus the current user message.

That avoids duplicating the dialogue (we do NOT also inject ``<recent_dialogue>``),
keeps roles native (the model sees its own prior ``assistant`` turns), and lets
Nexus own the history — ``history()`` reads the durable episodic store, so it
survives a restart.

Nexus never calls an LLM itself, so the "assistant" here is a canned stub.

Run it::

    python examples/basic_usage.py
"""

from pprint import pprint

from nexus_memory import NexusMemory

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
    so they belong in ``system`` — not in the chat transcript.
    """
    parts = [SYSTEM_PROMPT, MEMORY_PREAMBLE]
    if recall["directives"]:
        parts.append("Standing directives:\n" + "\n".join(f"- {d}" for d in recall["directives"]))
    if recall["raw_facts"]:
        parts.append("Known facts:\n" + "\n".join(f"- {f['content']}" for f in recall["raw_facts"]))
    return "\n\n".join(parts)


def simulated_assistant(system: str, messages: list[dict]) -> str:
    """Stand-in for YOUR model. A real call would be e.g. Anthropic:

        client.messages.create(model=..., system=system, messages=messages)

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
    memory = NexusMemory()  # minimal setup: one local .db file, offline embedder
    try:
        for turn, user_msg in enumerate(USER_TURNS, start=1):
            print(f"\n########## TURN {turn} -- user: {user_msg!r} ##########")

            # 1. RECALL — directives + facts for THIS query (-> system prompt).
            recall = memory.process({"action": "assemble", "query": user_msg})
            system = build_system(recall)

            # 2. NATIVE HISTORY — the durable conversation as role messages, plus
            #    the current user turn. memory.history() IS the native source here.
            messages = memory.history(as_format="messages")
            messages.append({"role": "user", "content": user_msg})

            # 3. Show exactly what a real API call would receive.
            print("\n>>> system (standard prompt + Nexus directives/facts):")
            print("    " + system.replace("\n", "\n    "))
            print("\n>>> messages (native, from memory.history() + current turn):")
            pprint(messages, sort_dicts=False, width=100)

            # 4. THINK — your model answers from (system, messages).
            answer = simulated_assistant(system, messages)
            print(f"\n--- simulated assistant: {answer!r}")

            # 5. WRITE — persist the exchange so the next turn's history() has it.
            memory.process({
                "action": "ingest",
                "interaction": {"query": user_msg, "response": answer},
            })
            memory.wait()  # ingest is async; wait so history() sees the new turn

        # Truncation knobs: turn-bounded or (approximate) token-bounded windows.
        print("\n########## history() truncation ##########")
        print("last 2 turns:")
        pprint(memory.history(max_turns=2), sort_dicts=False, width=100)
        print("\nas a plain string (e.g. to embed in a system prompt):")
        print(memory.history(max_turns=2, as_format="string"))
    finally:
        memory.close()


if __name__ == "__main__":
    main()
