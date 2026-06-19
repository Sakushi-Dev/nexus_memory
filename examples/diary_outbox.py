"""Layer V — the diary as an async outbox drained on a secondary model.

Nexus **never calls an LLM itself**. When a session summary is due it just
enqueues a *job* into an outbox; the host drains it on its own model,
out-of-band. That makes the diary **asynchronous** and **provider-agnostic**:
the chat only *ingests* (fast, non-blocking), while a **secondary model**
summarizes later.

The diary is now **session-scoped**: an entry tracks the current
``NexusMemory`` process run (the orchestrator's ``session_id``), not a calendar
day. The CURRENT session's entry is always injected into ``<memory_context>``
(even before it is finalized); ``inject_sessions`` (default 1) adds that many
previous finalized sessions, and a single growing ``<persistent_summary>`` folds
every ``sessions_per_summary`` (default 6) sessions, capped at
``summary_max_sentences`` (default 300). This whole demo runs in ONE process, so
both rounds below land in the SAME session and roll its single entry.

Each job carries Nexus's OWN ``prompt`` plus the prior entry
(``prior_summary``) and the recent turns (``input``). The host just runs that
prompt — Nexus owns the instruction. The diary is the **assistant's own
first-person journal**: ``SESSION_PROMPT`` asks the model to reflect on the whole
exchange (user + assistant) in its own voice, as flowing prose of 2-N sentences.

The job re-sends a **rolling, overlapping window** — up to ``diary_window`` turns
of the session (default 20 turns = 40 rows), not a strict delta. Because the
prior entry is fed back in every time and the prompt asks the model to reconcile,
the diary **rolls**: a newer turn that corrects an earlier one revises the entry,
and turns already reflected in the prior entry are NOT restated (overlap by
design, reconciled — never naively appended).

We ingest in two rounds to show that: round 2 contradicts round 1 (the deadline
moves), and the prior entry flows into the next job.

Each step lives in its OWN function; ``main()`` runs them and prints. Runs fully
offline with a deterministic stub model; the demo DB is removed on exit.

Run it::

    python examples/diary_outbox.py
"""

from pathlib import Path

from nexus_memory import DiaryConfig, NexusMemory

DB_PATH = Path("nexus_memory.db")

# update_every=5 (the default) -> one session job per 5 interactions, so each
# round below (5 interactions) enqueues exactly one summary job that rolls the
# current session's single entry.
ROUND_1 = [
    ("My name is Chris and I'm building a memory library.", "Nice to meet you, Chris."),
    ("I prefer Python and my deadline is next Friday.", "Noted - Python, Friday."),
    ("My favorite color is purple.", "Purple it is."),
    ("I'm using SQLite for storage.", "SQLite is a solid choice."),
    ("The library has five memory layers.", "Five layers - ambitious."),
]
ROUND_2 = [
    ("Actually the deadline moved to next Monday.", "Got it - Monday."),
    ("I started writing tests today.", "Nice - tests under way."),
    ("Feeling good about the progress.", "Great to hear."),
    ("I added a diary layer.", "A diary layer - nice touch."),
    ("Wrapping up for now.", "Rest well, Chris."),
]


def secondary_model(job: dict) -> str:
    """Stand-in for the host's SECONDARY model (offline, deterministic).

    A real host forwards Nexus's own ``job["prompt"]`` together with the prior
    entry (``job["prior_summary"]``) and the recent turns (``job["input"]``) to
    its model of choice — Nexus owns the instruction, the host just runs it. The
    diary is the assistant's first-person journal, so this stub writes in the
    first person and folds in BOTH roles (what the user said and what I said).

    The window OVERLAPS the prior entry, so a faithful stub must RECONCILE rather
    than naively append: it keeps the prior entry and weaves in only the genuinely
    new developments. We approximate that here by skipping any turn whose content
    already appears in the prior entry (a real model reconciles semantically and
    also fixes contradictions per the prompt — e.g. correct "Friday" to "Monday"
    — not just add them).
    """
    items = job["input"]
    prior = job.get("prior_summary") or ""

    # Only describe turns whose content is not already reflected in the prior entry.
    fresh = [t for t in items if t["content"] not in prior]
    user_said = "; ".join(t["content"] for t in fresh if t.get("role") == "user")
    i_said = "; ".join(t["content"] for t in fresh if t.get("role") == "assistant")

    new_bits = []
    if user_said:
        new_bits.append(f"the user told me: {user_said}")
    if i_said:
        new_bits.append(f"I replied: {i_said}")
    new = "; ".join(new_bits)

    if prior:
        return f"{prior} Continuing on, {new}." if new else prior
    return f"This session {new}." if new else "Nothing notable happened this session."


def run_round(memory: NexusMemory, interactions: list[tuple[str, str]]) -> str:
    # The conversation only INGESTS — fast, non-blocking; no summarizer runs here.
    for query, response in interactions:
        memory.process({"action": "ingest", "interaction": {"query": query, "response": response}})
    memory.wait()  # finish async ingest -> the session job is now in the outbox

    # Drain the outbox out-of-band on the secondary model — one call.
    applied = memory.drain_diary(secondary_model)

    # Both rounds run in one process = one session; read the current (newest) entry.
    session = memory.inspect(type="diary")["data"]["sessions"][-1]
    turns = "\n".join(f"  user: {q}" for q, _ in interactions)
    return (
        f"{turns}\n"
        f"-> drained {applied} job(s)\n"
        f"-> diary [session {session['session_id']}, seq {session['seq']}]: "
        f"{session['summary']}"
    )


def main() -> None:
    # Opt in to Layer V. The default cadence is update_every=5; each round below
    # has exactly 5 interactions so each enqueues one session job. The current
    # session's entry is always injected; inject_sessions (default 1) adds that
    # many previous finalized sessions, and a single persistent_summary folds
    # every sessions_per_summary (default 6) sessions, capped at
    # summary_max_sentences (default 300). All knobs live on DiaryConfig.
    memory = NexusMemory(diary=DiaryConfig(enabled=True), db_path=str(DB_PATH))
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
