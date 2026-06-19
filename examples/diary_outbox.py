"""Layer V — the diary as an async outbox drained on a secondary model.

Nexus **never calls an LLM itself**. When a daily summary is due it just enqueues
a *job* into an outbox; the host drains it on its own model, out-of-band. That
makes the diary **asynchronous** and **provider-agnostic**: the chat only
*ingests* (fast, non-blocking), while a **secondary model** summarizes later.

Each job carries Nexus's OWN ``prompt`` plus the prior entry
(``prior_summary``) and the new turns (``input``). The host just runs that prompt
— Nexus owns the instruction. Because the prior entry is fed back in every time,
the diary **rolls**: a new turn that corrects an earlier one updates the entry
(see ``DAILY_PROMPT``) instead of starting over.

We ingest in two rounds to show that: round 2 contradicts round 1 (the deadline
moves), and the prior entry flows into the next job.

Each step lives in its OWN function; ``main()`` runs them and prints. Runs fully
offline with a deterministic stub model; the demo DB is removed on exit.

Run it::

    python examples/diary_outbox.py
"""

from pathlib import Path

from nexus_memory import NexusMemory

DB_PATH = Path("nexus_memory.db")

# update_every=3 (the default) -> one daily job per 3 interactions, so each round
# below enqueues exactly one summary job.
ROUND_1 = [
    ("My name is Chris and I'm building a memory library.", "Nice to meet you, Chris."),
    ("I prefer Python and my deadline is next Friday.", "Noted - Python, Friday."),
    ("My favorite color is purple.", "Purple it is."),
]
ROUND_2 = [
    ("Actually the deadline moved to next Monday.", "Got it - Monday."),
    ("I started writing tests today.", "Nice - tests under way."),
    ("Feeling good about the progress.", "Great to hear."),
]


def secondary_model(job: dict) -> str:
    """Stand-in for the host's SECONDARY model (offline, deterministic).

    A real host forwards Nexus's own ``job["prompt"]`` together with the prior
    entry (``job["prior_summary"]``) and the new turns (``job["input"]``) to its
    model of choice — Nexus owns the instruction, the host just runs it. This
    stub keeps the prior entry and folds in the new user turns, so the diary
    ROLLS. (A real model would also *reconcile* contradictions per the prompt —
    e.g. fix "Friday" to "Monday" — not just append them.)
    """
    new = "; ".join(t["content"] for t in job["input"] if t.get("role") == "user")
    prior = job.get("prior_summary") or ""
    if prior:
        return f"{prior} Later: {new}." if new else prior
    return f"The user mentioned: {new}." if new else "Nothing notable."


def run_round(memory: NexusMemory, interactions: list[tuple[str, str]]) -> str:
    # The conversation only INGESTS — fast, non-blocking; no summarizer runs here.
    for query, response in interactions:
        memory.process({"action": "ingest", "interaction": {"query": query, "response": response}})
    memory.wait()  # finish async ingest -> the daily job is now in the outbox

    # Drain the outbox out-of-band on the secondary model — one call.
    applied = memory.drain_diary(secondary_model)

    day = memory.inspect(type="diary")["data"]["days"][0]
    turns = "\n".join(f"  user: {q}" for q, _ in interactions)
    return (
        f"{turns}\n"
        f"-> drained {applied} job(s)\n"
        f"-> diary [{day['period']}]: {day['summary']}"
    )


def main() -> None:
    memory = NexusMemory(diary=True, db_path=str(DB_PATH))  # opt in to Layer V
    try:
        # Each round returns its text; main() prints it.
        print("########## 1. round 1 -- ingest + drain ##########")
        print(run_round(memory, ROUND_1))
        print("\n########## 2. round 2 -- ingest + drain (rolls in the prior entry) ##########")
        print(run_round(memory, ROUND_2))
    finally:
        memory.close()
        # Remove the DB (and SQLite's -wal/-shm sidecars) so each run is clean.
        for path in (DB_PATH, *(DB_PATH.with_name(DB_PATH.name + s) for s in ("-wal", "-shm"))):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
