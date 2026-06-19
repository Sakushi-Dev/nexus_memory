"""Focused tour of ``NexusMemory.tokens()`` — token accounting, every option.

``tokens()`` counts the ACTUAL LLM round-trip — the request ``messages`` array
plus the model's ``response`` — split by SECTION (not storage layer)::

    system  = all role == "system" content   (base prompt + Nexus-injected facts)
    input   = all role in {"user","assistant"}  (history + the current user turn)
    output  = the response text
    full    = system + input + output         (the default scope)

It reads nothing from storage: you hand it the array you actually sent, so it
counts exactly what crossed the wire. ``config=`` picks HOW tokens are counted —
the offline ``len//4`` default, your own ``(str) -> int`` callable, or the
optional **tiktoken** backend for exact counts.

Each variation lives in its OWN function below; ``main()`` runs them one after
another. Runs fully offline; the tiktoken part is skipped if the extra is absent.

Run it::

    python examples/token_accounting.py
"""

from pathlib import Path

from nexus_memory import NexusMemory

DB_PATH = Path("nexus_memory.db")

# A representative OpenAI request: a system message (base prompt + a Nexus-
# injected fact), two history turns, and the current user turn — plus the reply.
MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are a helpful personal assistant.\n\n"
            "Known facts:\n- User keeps house keys in the blue ceramic bowl."
        ),
    },
    {"role": "user", "content": "where do I keep my keys?"},
    {"role": "assistant", "content": "In the blue ceramic bowl on the counter."},
    {"role": "user", "content": "remind me again, please"},
]

RESPONSE = "Your house keys are in the blue ceramic bowl on the counter."


# --------------------------------------------------------------------------- #
# Variation 1 — a single scope returns a plain int. Each section on its own.
# --------------------------------------------------------------------------- #

def demo_single_scope(memory: NexusMemory) -> None:

    scope = "system"
    usage = memory.tokens(scope, messages=MESSAGES, response=RESPONSE)

    return f"{scope}: {usage}"


# --------------------------------------------------------------------------- #
# Variation 2 — a LIST of scopes returns a {scope: int} dict plus a summed
# 'total' (the sum of exactly the scopes you asked for).
# --------------------------------------------------------------------------- #
def demo_list_scopes(memory: NexusMemory) -> None:

    usage = memory.tokens(
        ["system", "input", "output"], messages=MESSAGES, response=RESPONSE
    )

    return f"{usage}"  # 'total' == system + input + output


# --------------------------------------------------------------------------- #
# Variation 3 — config= picks HOW tokens are counted (counter injection):
# the offline default, your own callable, or the optional tiktoken backend.
# --------------------------------------------------------------------------- #
def demo_config_methods(memory: NexusMemory) -> None:

    # Two defaults at once: scope is OMITTED here (it defaults to "full"), and
    # config=None -> the built-in len(s)//4 heuristic. The calls below pass the
    # "full" scope explicitly so the difference (config) is the only variable.
    default = memory.tokens(messages=MESSAGES, response=RESPONSE)

    # A callable -> your own (str) -> int is injected and used as-is.
    def word_counter(text: str) -> int:
        """A trivial CUSTOM counter (one token per whitespace word)."""
        return len(text.split())

    words = memory.tokens("full", messages=MESSAGES, response=RESPONSE, config=word_counter)

    lines = [
        f"None  (len//4)                     : {default}",
        f"callable (words)                   : {words}",
    ]

    # A string / model name / dict -> the optional tiktoken backend (exact).
    # Skipped gracefully when the extra is not installed.
    for spec in ("tiktoken", "gpt-4o", {"encoding": "cl100k_base"}):
        try:
            n = memory.tokens("full", messages=MESSAGES, response=RESPONSE, config=spec)
            lines.append(f"config={spec!r:26}  : {n}")
        except ImportError:
            lines.append(f"config={spec!r:26}  : not installed (pip install nexus-memory[tiktoken])")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Variation 4 — count one side only: the prompt you send (system + input)
# versus the completion you get back (output).
# --------------------------------------------------------------------------- #
def demo_prompt_vs_completion(memory: NexusMemory) -> None:

    prompt = memory.tokens(["system", "input"], messages=MESSAGES)  # what you send
    completion = memory.tokens("output", response=RESPONSE)         # what you get back

    return f"prompt (system+input): {prompt['total']}\ncompletion (output)  : {completion}"


# --------------------------------------------------------------------------- #
# Variation 5 — edge cases & errors: nothing passed counts as 0; an unknown
# scope raises ValueError; a config of the wrong type raises TypeError.
# --------------------------------------------------------------------------- #
def demo_edge_cases(memory: NexusMemory) -> None:

    empty = memory.tokens("full")  # nothing passed -> 0
    lines = [f"empty (no args)  : {empty}"]

    try:
        memory.tokens("bogus", messages=MESSAGES)
    except ValueError:
        lines.append("unknown scope    : ValueError raised")

    try:
        memory.tokens("full", messages=MESSAGES, config=123)
    except TypeError:
        lines.append("bad config type  : TypeError raised")

    return "\n".join(lines)


def main() -> None:
    memory = NexusMemory(db_path=str(DB_PATH))  # offline; tokens() needs no state
    try:
        # Run each variation in turn — each returns its text, main() prints it.
        print("########## 1. single scope (-> int) ##########")
        print(demo_single_scope(memory))
        print("\n########## 2. list of scopes (-> dict + total) ##########")
        print(demo_list_scopes(memory))
        print("\n########## 3. config= counting methods ##########")
        print(demo_config_methods(memory))
        print("\n########## 4. prompt vs completion ##########")
        print(demo_prompt_vs_completion(memory))
        print("\n########## 5. edge cases ##########")
        print(demo_edge_cases(memory))
    finally:
        memory.close()
        # Remove the DB (and SQLite's -wal/-shm sidecars) so each run is clean.
        for path in (DB_PATH, *(DB_PATH.with_name(DB_PATH.name + s) for s in ("-wal", "-shm"))):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
