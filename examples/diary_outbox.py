"""Layer V — the optional hierarchical diary via a handoff outbox (offline demo).

Nexus **never calls an LLM itself**. When a summary is due it enqueues a *job*
(prompt + prior summary + the new turns) into an outbox. The host drains the
outbox, runs the job on *any* model it likes, and hands the text back via
``submit_summary()`` — so the diary is fully provider-agnostic and the module
stays offline-testable (a job is just data).

This demo uses a trivial, deterministic stand-in "model" so it runs entirely
offline (no network, no API key). A real host would forward ``job["prompt"]``,
``job["prior_summary"]`` and ``job["input"]`` to its model of choice.

Run with the project venv::

    ./.venv/Scripts/python.exe examples/diary_outbox.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nexus_memory import DiaryConfig, NexusMemory


def fake_model(prompt: str, prior_summary: str | None, turns: list[dict]) -> str:
    """A deterministic stand-in for the host's LLM (no network).

    Stitches the user's statements into a third-person entry, refining the prior
    summary when one is supplied (the diary updates are *rolling*).
    """
    said = "; ".join(t["content"] for t in turns if t.get("role") == "user")
    entry = f"The user mentioned: {said}" if said else "Nothing notable."
    return f"{prior_summary} {entry}".strip() if prior_summary else entry


def drain_outbox(memory: NexusMemory) -> int:
    """Run every pending summary job on the fake model and submit the result.

    This is the host's responsibility — Nexus only *schedules* the jobs.
    """
    jobs = memory.pending_summaries()
    for job in jobs:
        text = fake_model(job["prompt"], job["prior_summary"], job["input"])
        memory.submit_summary(job["job_id"], text)
    return len(jobs)


def main() -> None:
    db_path = str(Path(tempfile.mkdtemp()) / "diary.db")
    # Opt in to Layer V (off by default). N=3 → a daily job every 3 interactions.
    memory = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        interactions = [
            ("My name is Chris and I'm building a memory library.",
             "Nice to meet you, Chris."),
            ("I prefer Python and my deadline is next Friday.",
             "Noted — Python, and a Friday deadline."),
            ("My favorite color is purple.", "Purple it is."),
        ]
        for query, response in interactions:
            memory.process({
                "action": "ingest",
                "interaction": {"query": query, "response": response},
            })
        memory.wait()  # let the async writer + diary consolidator finish

        # 1. The diary scheduled a job; the host drains the outbox (your model runs here).
        applied = drain_outbox(memory)
        print(f"drained {applied} summary job(s) from the outbox")

        # 2. Inspect the time-pyramid: L1 daily diaries + L2 persistent sections.
        state = memory.inspect(type="diary")["data"]
        for day in state["days"]:
            print(f"diary {day['period']}: {day['summary']}")
        print(f"persistent sections: {len(state['sections'])}")
        # (Section folding and the <diary>/<persistent_summary> context injection
        #  kick in across day boundaries — see docs/ms8_diary.md.)
    finally:
        memory.close()


if __name__ == "__main__":
    main()
