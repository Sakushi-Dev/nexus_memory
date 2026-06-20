# Multiple independent agents

Every `NexusMemory` instance is bound to **one SQLite file** (`db_path`). To run
several **independent** agents — each with its own facts, dialogue history,
directives, and diary — just give each one its own database. Nothing is shared:
agent A never sees agent B's memories.

```python
from nexus_memory import NexusMemory

alice = NexusMemory(db_path="alice.db")   # one agent, one memory
bob   = NexusMemory(db_path="bob.db")     # a separate agent, fully isolated

alice.process({"action": "ingest", "interaction": {"query": "...", "response": "..."}})
bob.process({"action": "ingest",   "interaction": {"query": "...", "response": "..."}})

alice.process({"action": "assemble", "query": "..."})   # reads ONLY alice's memory
alice.close(); bob.close()
```

## What "independent" means

Each instance owns its **own** SQLite connection, background writer thread, and
read cache, plus its own `agent_memory` vector store and the episodic /
procedural / diary tables inside that one file. There is no cross-talk: a
`forget`, `pin`, `assemble`, ingest, or diary drain on one instance touches only
that instance's `.db`.

## One process or many

Both work, because the isolation is per **file**:

- **One process, many instances** — construct several `NexusMemory` objects with
  distinct `db_path`s. With the default offline `HashingEmbedder` (no model
  download) an instance is cheap, so running a handful of agents in one process
  is fine.
- **One process per agent** — run each agent as its own process pointing at its
  own file. SQLite handles separate files without any coordination.

> **One rule:** never point two instances that should stay independent at the
> **same** `db_path` — they would then share a store (and a writer thread), which
> is exactly *not* what you want for separate agents.

## Why not one shared database?

You *can* tag facts with `metadata` (and writer dedup is namespace-aware on the
`namespace` / `tenant` / `user` / `user_id` / `source` keys), but `assemble` does
not expose per-request scoping — retrieval reads across the whole store. So for
**true** isolation between agents, **one database per agent** is the simplest and
most reliable approach. Reach for shared-store metadata only when you actually
*want* a common memory with soft grouping rather than hard separation.
