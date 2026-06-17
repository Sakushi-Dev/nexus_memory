"""Layer V — using the diary asynchronously with a secondary model (offline demo).

Nexus **never calls an LLM itself**. When a summary is due it just enqueues a
*job* into an outbox; the host drains that outbox on its own model, out-of-band.
That makes the diary naturally **asynchronous**: your conversation only *ingests*
(fast, non-blocking), while a **secondary model** summarizes in the background —
so a slow or expensive summarizer never blocks the chat.

This demo wires exactly that: a background worker thread drains the outbox with a
secondary model while the main thread keeps ingesting. The model here is a tiny
deterministic stub (no network/key); a real host would forward ``job["prompt"]``
and ``job["input"]`` to its summarization model of choice.

Run it::

    python examples/diary_outbox.py
"""

from __future__ import annotations

import threading
import time

from nexus_memory import NexusMemory


def secondary_model(job: dict) -> str:
    """The SECONDARY model — a stand-in summarizer (offline, deterministic)."""
    said = "; ".join(t["content"] for t in job["input"] if t.get("role") == "user")
    return f"The user mentioned: {said}" if said else "Nothing notable."


def diary_worker(memory: NexusMemory, stop: threading.Event) -> None:
    """Background drain loop: summarize any pending job with the secondary model.

    Runs OFF the conversation path — ingest never waits on the summarizer.
    """
    while not stop.is_set():
        for job in memory.pending_summaries():
            memory.submit_summary(job["job_id"], secondary_model(job))
        stop.wait(0.1)  # poll the outbox; a real host could use any cadence


def main() -> None:
    # Opt in to Layer V (off by default). N=3 → a daily job every 3 interactions.
    memory = NexusMemory(diary=True)  # db_path defaults to "nexus_memory.db"

    # Start the secondary model draining the outbox asynchronously.
    stop = threading.Event()
    worker = threading.Thread(target=diary_worker, args=(memory, stop), daemon=True)
    worker.start()

    try:
        # The conversation only ingests — fast, non-blocking. The background worker
        # summarizes whenever a job appears; the main loop never blocks on it.
        interactions = [
            ("My name is Chris and I'm building a memory library.", "Nice to meet you, Chris."),
            ("I prefer Python and my deadline is next Friday.", "Noted — Python, Friday."),
            ("My favorite color is purple.", "Purple it is."),
        ]
        for query, response in interactions:
            memory.process({"action": "ingest", "interaction": {"query": query, "response": response}})
        memory.wait()  # finish async ingest → the daily job is now in the outbox

        # Give the background worker a moment to drain it (bounded).
        for _ in range(50):
            if not memory.pending_summaries():
                break
            time.sleep(0.05)

        for day in memory.inspect(type="diary")["data"]["days"]:
            print(f"diary {day['period']}: {day['summary']}")
    finally:
        stop.set()
        worker.join(timeout=1)
        memory.close()


if __name__ == "__main__":
    main()
