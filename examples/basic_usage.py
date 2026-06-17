"""Basic end-to-end usage of the Nexus Memory module.

Runs entirely offline with the default :class:`HashingEmbedder` — no network and
no model download. Demonstrates the full lifecycle through the single
``process()`` entry point: ingest, assemble, inspect, and forget.

Run it::

    python examples/basic_usage.py
"""

from __future__ import annotations

from nexus_memory import NexusMemory


def main() -> None:
    memory = NexusMemory()  # db_path defaults to "nexus_memory.db"
    try:
        # 1. Ingest an interaction (async; wait() for determinism in a script).
        memory.process({
            "action": "ingest",
            "interaction": {
                "query": "where do I keep my keys?",
                "response": "You always keep your house keys in the blue ceramic bowl.",
            },
        })
        memory.wait()

        # 2. Assemble a prompt-ready memory context for a related query.
        result = memory.process({"action": "assemble", "query": "where are my house keys?"})
        print(result["context_xml"])

        # 3. Inspect store health.
        print("health:", memory.inspect(type="health")["data"][0])

        # 4. Forget by free-text query.
        print("forgot:", memory.forget(query="house keys"))
    finally:
        memory.close()


if __name__ == "__main__":
    main()
