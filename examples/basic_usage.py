"""Basic end-to-end usage of the Nexus Memory module.

Runs entirely offline with the default :class:`HashingEmbedder` — no network and
no model download. Demonstrates the full lifecycle through the single
``process()`` entry point: ingest, assemble, inspect, and forget.

Run with the project venv::

    ./.venv/Scripts/python.exe examples/basic_usage.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nexus_memory import NexusMemory


def main() -> None:
    db_path = str(Path(tempfile.mkdtemp()) / "demo.db")
    memory = NexusMemory(db_path=db_path)
    try:
        # 1. Ingest an interaction (async; wait() for determinism in a script).
        memory.process(
            {
                "action": "ingest",
                "interaction": {
                    "query": "where do I keep my keys?",
                    "response": (
                        "You always keep your house keys in the blue ceramic "
                        "bowl on the kitchen counter."
                    ),
                },
            }
        )
        memory.wait()

        # 2. Assemble a prompt-ready memory context for a related query.
        result = memory.process(
            {
                "action": "assemble",
                "query": "where are my house keys?",
                "top_k": 3,
                "min_score": 0.0,
            }
        )
        print("status:", result["status"])
        print(result["context_xml"])
        print("latency_ms:", round(result["latency_ms"], 3))

        # 3. Inspect store health.
        health = memory.inspect(type="health")
        print("health:", health["data"][0])

        # 4. Forget by free-text query.
        forgotten = memory.forget(query="house keys")
        print("forgot:", forgotten)
    finally:
        memory.close()


if __name__ == "__main__":
    main()
