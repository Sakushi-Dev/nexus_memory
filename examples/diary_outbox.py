"""Layer V — using the diary asynchronously with a secondary model (offline demo).

Nexus **never calls an LLM itself**. When a summary is due it just enqueues a
*job* into an outbox; the host drains that outbox on its own model, out-of-band.
That makes the diary naturally **asynchronous** and **provider-agnostic**: your
conversation only *ingests* (fast, non-blocking), while a **secondary model**
summarizes whenever you get around to it — a slow or expensive summarizer never
blocks the chat.

Draining is a single call, :meth:`NexusMemory.drain_diary`, which hands each
pending job to your model and folds the results back in. Because it is just a
method, you can run it wherever suits you: in a background worker, a cron job, or
simply a later process. This demo keeps it straight-line — drain inline, no
threading. The model here is a tiny deterministic stub (no network/key); a real
host would forward ``job["prompt"]`` and ``job["input"]`` to its model of choice.

Run it::

    python examples/diary_outbox.py
"""

from __future__ import annotations

from nexus_memory import NexusMemory


def secondary_model(job: dict) -> str:
    """The SECONDARY model — a stand-in summarizer (offline, deterministic)."""
    said = "; ".join(t["content"] for t in job["input"] if t.get("role") == "user")
    return f"The user mentioned: {said}" if said else "Nothing notable."


def main() -> None:
    # Opt in to Layer V (off by default). N=3 → a daily job every 3 interactions.
    memory = NexusMemory(diary=True)  # db_path defaults to "nexus_memory.db"

    try:
        # The conversation only ingests — fast, non-blocking. No summarizer runs yet.
        interactions = [
            ("My name is Chris and I'm building a memory library.", "Nice to meet you, Chris."),
            ("I prefer Python and my deadline is next Friday.", "Noted — Python, Friday."),
            ("My favorite color is purple.", "Purple it is."),
        ]
        for query, response in interactions:
            memory.process({"action": "ingest", "interaction": {"query": query, "response": response}})
        memory.wait()  # finish async ingest → the daily job is now in the outbox

        # Drain the outbox out-of-band on the secondary model — one call.
        applied = memory.drain_diary(secondary_model)
        print(f"drained {applied} summary job(s)")

        for day in memory.inspect(type="diary")["data"]["days"]:
            print(f"diary {day['period']}: {day['summary']}")
    finally:
        memory.close()


if __name__ == "__main__":
    main()
