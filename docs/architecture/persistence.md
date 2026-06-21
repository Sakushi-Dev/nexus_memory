# Persistence & Storage

Nexus Memory persists everything to **one SQLite file** (with the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension for vector search):
one shared connection, one re-entrant write lock, WAL journaling. This page
documents the storage model, every table grouped by its owning layer, the
`__DIM__` substitution applied at init, and the `vec0` gotchas that shape the
design.

All SQL described here lives in [`schema.sql`](../../src/nexus_memory/schema.sql),
[`core/db.py`](../../src/nexus_memory/core/db.py), and the per-layer stores under
[`src/nexus_memory/layers/`](../../src/nexus_memory/layers/).

---

## The storage model in one paragraph

There is exactly **one** database file at
[`NexusConfig.db_path`](../../src/nexus_memory/core/config.py) (default
`nexus_memory.db`, plus the `-wal` / `-shm` sidecars created by WAL mode). A
single [`NexusDB`](../../src/nexus_memory/core/db.py) instance owns the
connection lifecycle and **all** of the core (semantic/vector) SQL; the episodic,
procedural, and diary layer stores reuse that same connection and write lock to
manage their own tables. The connection is opened with
`check_same_thread=False` (the writer runs on background threads) and
`row_factory = sqlite3.Row`, the `sqlite-vec` extension is loaded, the schema is
applied, and WAL is enabled — all in [`NexusDB.initialize()`](../../src/nexus_memory/core/db.py).

```python
conn = sqlite3.connect(self.config.db_path, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)

schema_sql = _find_schema().read_text(encoding="utf-8")
schema_sql = schema_sql.replace("__DIM__", str(self.config.dim))
conn.executescript(schema_sql)

conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.commit()
```

### One connection, one lock

Every layer shares the same `sqlite3.Connection` (`db.conn`). Because SQLite
raises on interleaved commits from different threads on one connection, **all
writes are serialized through a single re-entrant lock**,
[`NexusDB.lock`](../../src/nexus_memory/core/db.py) (a `threading.RLock`):

```python
self.lock: threading.RLock = threading.RLock()
```

* **Writes** acquire the lock (`with db.lock:` / `with self.lock:`) around the
  `execute(...)` + `commit()`. The lock is **re-entrant**, so a method that holds
  it may call another that also acquires it.
* **Reads** use the shared connection *without* the lock — safe under WAL, where
  readers do not block the single writer and vice versa.

This is why the layer stores ([`EpisodicStore`](../../src/nexus_memory/layers/episodic/episodic.py),
[`ProceduralStore`](../../src/nexus_memory/layers/procedural/procedural.py),
[`DiaryStore`](../../src/nexus_memory/layers/diary/store.py)) take a `NexusDB` and
never open their own connection: there is exactly one serialized writer across
all layers.

### WAL + `synchronous=NORMAL`

WAL (`journal_mode=WAL`) lets a background writer thread and foreground readers
operate concurrently; `synchronous=NORMAL` trades maximal durability for
throughput on the local-first path. [`vacuum()`](../../src/nexus_memory/core/db.py)
runs `PRAGMA wal_checkpoint(TRUNCATE)` and then `VACUUM` to reclaim space and
collapse the WAL.

---

## The `__DIM__` substitution

The vector table's dimension is **fixed at table-creation time** and must match
the active embedder. Rather than hard-code it, [`schema.sql`](../../src/nexus_memory/schema.sql)
carries the literal token `__DIM__`, and `NexusDB` replaces it with
`config.dim` (default **768**, see [`DEFAULT_DIM`](../../src/nexus_memory/core/config.py))
before `executescript`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory USING vec0(
    embedding float[__DIM__] distance_metric=cosine,
    ...
);
```

> **Dimension is frozen.** Changing `config.dim` after the table exists does
> **not** migrate anything — `CREATE ... IF NOT EXISTS` is a no-op against the
> existing file. A different dimension requires a fresh database (or a full
> re-embed/migration), and it must match the embedder you actually run. See
> [Embedders](../usage/embedders.md).

`schema.sql` is located by [`_find_schema()`](../../src/nexus_memory/core/db.py),
which checks the packaged copy next to the module first, then the project root —
so both installed wheels and editable/source checkouts resolve it.

---

## Tables by owning layer

The single file holds tables created by two mechanisms: the **core** tables come
from `schema.sql` at init; the **per-layer** tables are created idempotently with
`CREATE TABLE IF NOT EXISTS` when their store is constructed. The diary tables
exist **only** when the diary layer (Layer V) is enabled.

```
                       nexus_memory.db  (single SQLite + sqlite-vec)
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ CORE   (schema.sql, dim fixed at creation)                                   │
 │   agent_memory   vec0(embedding float[DIM] cosine, +content, +metadata,      │  III Semantic
 │                       +importance, +timestamp)                               │
 │   memory_edges   (source_id, target_id, relation)   PK(all 3)                │  III graph
 │   system_config  (key, value)                                                │  bookkeeping
 ├────────────────────────────────────────────────────────────────────────────┤
 │ EPISODIC   (created by EpisodicStore)                                         │
 │   episodic_turns      (id, session_id, role, content, timestamp, metadata)   │  II
 │   episodic_summaries  (id, period, summary, turn_count, created_at)          │  II
 ├────────────────────────────────────────────────────────────────────────────┤
 │ PROCEDURAL   (created by ProceduralStore)                                     │
 │   procedural_rules    (id, directive UNIQUE, category, priority, active,      │  IV
 │                        source, timestamp)                                     │
 ├────────────────────────────────────────────────────────────────────────────┤
 │ DIARY   (created by DiaryStore — ONLY when Layer V is enabled)                │
 │   diary_sessions      (session_id PK, seq, summary, covered_through, ...)     │  V
 │   persistent_summary  (id PK CHECK(id=1), summary, session_count, ...)        │  V
 │   summarization_jobs  (job_id PK, kind, target, status, prompt, ...)          │  V outbox
 └────────────────────────────────────────────────────────────────────────────┘
   Layer I (Working memory) is RAM-only — it has no tables.
```

For how these layers fit together at the API level, see
[Memory Layers](memory-layers.md) and [the Diary Layer](diary-layer.md).

### Core (`schema.sql`) — Layer III + bookkeeping

#### `agent_memory` (vec0 virtual table)

The semantic store. A `vec0` virtual table whose `embedding` column is the
indexed vector; the `+`-prefixed columns are **auxiliary** (non-indexed) payload.

| Column | Type | Notes |
| --- | --- | --- |
| `embedding` | `float[__DIM__]` | Indexed vector. `distance_metric=cosine` declared at creation so `MATCH` ranks by cosine automatically. |
| `+content` | `TEXT NOT NULL` | The memory text. |
| `+metadata` | `TEXT` | JSON blob; parsed back to a dict on read. |
| `+importance` | `FLOAT DEFAULT 1.0` | Scoring weight (the `DEFAULT` here *is* honored on the SQL path, but inserts always pass it explicitly). |
| `+timestamp` | `TEXT DEFAULT CURRENT_TIMESTAMP` | UTC `YYYY-MM-DD HH:MM:SS`. **The `DEFAULT` is inert** — see [vec0 gotchas](#vec0-gotchas). Set explicitly in code. |

KNN search uses the indexed `vec0` path; `distance` is the implicit cosine
distance column:

```sql
SELECT rowid AS id, content, importance, timestamp, metadata, distance
FROM agent_memory
WHERE embedding MATCH ? AND k = ?
ORDER BY distance
```

The row's `rowid` is the public memory `id`. Vectors are serialized with
`sqlite_vec.serialize_float32()` before binding. See
[Retrieval & Scoring](retrieval-and-scoring.md) for how `distance`, `importance`,
and `timestamp` combine into a final score.

#### `memory_edges` — the 1-hop knowledge graph

```sql
CREATE TABLE IF NOT EXISTS memory_edges (
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT DEFAULT 'related',
    PRIMARY KEY (source_id, target_id, relation)
);
```

A lightweight directed graph of 1-hop relations between memories, supported at
the storage layer (`add_edge()` / `neighbors(memory_id)` in
[`core/db.py`](../../src/nexus_memory/core/db.py)). The table is **not** populated
or consulted by the default ingest/assemble path — it is latent scaffolding for
callers that want to record their own associations, not an advertised retrieval
feature. The read path is pure KNN + re-rank (see
[Retrieval & Scoring](retrieval-and-scoring.md)).

#### `system_config` — bookkeeping

```sql
CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

A simple key/value status table for module-level bookkeeping.

### Episodic (Layer II) — `EpisodicStore`

Created on construction of
[`EpisodicStore`](../../src/nexus_memory/layers/episodic/episodic.py). This is the
durable raw-dialogue log (the volatile Layer I working memory keeps only the last
N turns in RAM; this keeps the full history on disk).

**`episodic_turns`** — every user/assistant turn, verbatim:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Turn id. |
| `session_id` | `TEXT` | Optional conversation/session identifier. |
| `role` | `TEXT NOT NULL` | `'user'` or `'assistant'`. |
| `content` | `TEXT NOT NULL` | Turn text. |
| `timestamp` | `TEXT NOT NULL` | UTC `YYYY-MM-DD HH:MM:SS`. |
| `metadata` | `TEXT` | JSON blob. |

Plus `INDEX idx_episodic_turns_ts(timestamp)` for time-range queries.

**`episodic_summaries`** — narrative day summaries:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Summary id. |
| `period` | `TEXT NOT NULL` | A day, `YYYY-MM-DD`. |
| `summary` | `TEXT NOT NULL` | Narrative text. |
| `turn_count` | `INTEGER` | Turns summarized. |
| `created_at` | `TEXT NOT NULL` | UTC timestamp. |

### Procedural (Layer IV) — `ProceduralStore`

Created on construction of
[`ProceduralStore`](../../src/nexus_memory/layers/procedural/procedural.py).
Standing behavioral directives ("Keep answers concise.", "Address the user as Sam.").

**`procedural_rules`**:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Rule id. |
| `directive` | `TEXT NOT NULL` | Imperative rule text. **`UNIQUE`** — the natural key. |
| `category` | `TEXT` | One of `tone` / `format` / `persona` / `other`. |
| `priority` | `INTEGER DEFAULT 5` | 1..10, higher applied first (clamped on write). |
| `active` | `INTEGER DEFAULT 1` | `1` active, `0` deactivated. |
| `source` | `TEXT` | `'manual'` or `'auto'`. |
| `timestamp` | `TEXT NOT NULL` | UTC timestamp. |

Plus `INDEX idx_procedural_active(active, priority DESC)`. Because `directive` is
`UNIQUE`, [`add_rule()`](../../src/nexus_memory/layers/procedural/procedural.py)
upserts via `ON CONFLICT(directive) DO UPDATE`, so a repeated directive yields
exactly one row (re-activated, with refreshed category/priority/source/timestamp).
See [Behavioral Rules](../use-cases/behavioral-rules.md).

### Diary (Layer V) — `DiaryStore` (only when enabled)

Created on construction of
[`DiaryStore`](../../src/nexus_memory/layers/diary/store.py) — and **only** when
the diary layer is active. The DDL lives in the store module (not `schema.sql`),
so a deployment that never enables the diary never creates these tables.

**`diary_sessions`** (L1 — one row per session):

A "session" is one `NexusMemory` process run, identified by
`orchestrator.session_id` (a `uuid4`). Because uuids are not orderable, the store
assigns each new session a monotonic `seq` (1, 2, 3, …) that orders
`current`/`previous` and triggers the fold.

| Column | Type | Notes |
| --- | --- | --- |
| `session_id` | `TEXT PRIMARY KEY` | The `orchestrator.session_id` (uuid4) of the session. |
| `seq` | `INTEGER UNIQUE` | Monotonic order (`1,2,3…`); orders current/previous + the fold. |
| `summary` | `TEXT DEFAULT ''` | The session narrative (rolling). |
| `covered_through` | `INTEGER DEFAULT 0` | Last-applied high-water mark (max `episodic_turns.id` folded in). |
| `interaction_count` | `INTEGER DEFAULT 0` | Interactions seen this session. |
| `finalized` | `INTEGER DEFAULT 0` | `1` once the session is closed (rollover/`close()`). |
| `folded` | `INTEGER DEFAULT 0` | `1` once folded into the persistent summary. |
| `created_at` | `TEXT` | UTC timestamp. |
| `updated_at` | `TEXT` | UTC timestamp. |

**`persistent_summary`** (L2 — a single growing row, not a ring):

One singleton row. The first fold creates it; every subsequent fold **extends**
the same `summary` (no freeze, no ring). It is capped at `summary_max_sentences`
(default 300) by the host's summarizer.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY CHECK (id = 1)` | Singleton row — always `1`. |
| `summary` | `TEXT DEFAULT ''` | The single growing summary. |
| `session_count` | `INTEGER DEFAULT 0` | Sessions folded so far. |
| `first_session` / `last_session` | `TEXT` | Covered range (`session_id`). |
| `updated_at` | `TEXT` | UTC timestamp. |

**`summarization_jobs`** (the outbox — Nexus never calls an LLM itself):

| Column | Type | Notes |
| --- | --- | --- |
| `job_id` | `TEXT PRIMARY KEY` | `uuid4`. |
| `kind` | `TEXT NOT NULL` | `'session'` or `'summary'`. |
| `target` | `TEXT NOT NULL` | Session: the `session_id`; summary: the constant `'1'` (the singleton). |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | `'pending'` / `'done'` / `'superseded'`. |
| `prompt` | `TEXT NOT NULL` | Nexus-owned instruction (host forwards verbatim). |
| `input_json` | `TEXT NOT NULL` | JSON `{prior_summary, items:[...]}`. |
| `advance_to` | `INTEGER` | Session: `covered_through` to set on apply; summary: `NULL`. |
| `created_at` | `TEXT NOT NULL` | UTC timestamp. |
| `answered_at` | `TEXT` | Set when marked `done`. |

Plus `INDEX idx_jobs_status(status, created_at)`. The one-pending-per-target
invariant is enforced on enqueue (an existing `pending` job for the same
`(kind, target)` is marked `superseded`). The full handoff protocol is documented
in the [Diary Layer](diary-layer.md) and [Data Flow](../io/data-flow.md) pages.

---

## vec0 gotchas

The `vec0` virtual table is powerful but has sharp edges that directly shape the
core SQL. All of these are handled inside
[`core/db.py`](../../src/nexus_memory/core/db.py):

### Fixed dimension at creation

`embedding float[__DIM__]` bakes the dimension into the table. It cannot be
altered later; a re-dimension is a fresh database or a full re-embed. The
embedder you run **must** produce `config.dim`-length vectors.

### Auxiliary-column `DEFAULT` is inert

vec0 auxiliary (`+`) columns do **not** honor a column `DEFAULT`. The
`+timestamp TEXT DEFAULT CURRENT_TIMESTAMP` declaration in `schema.sql` is
effectively dead — `CURRENT_TIMESTAMP` is never applied, and a `NULL` timestamp
would **silently disable time-decay scoring**. So the timestamp is supplied
explicitly on every insert via [`_utc_now_str()`](../../src/nexus_memory/core/db.py):

```python
def _utc_now_str() -> str:
    # UTC 'YYYY-MM-DD HH:MM:SS' — matches CURRENT_TIMESTAMP's format.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
```

This same helper is reused by **every** layer store, so semantic facts, episodic
turns, procedural rules, and diary rows all share one UTC `YYYY-MM-DD HH:MM:SS`
format and interleave chronologically.

### No in-place UPDATE → DELETE + re-INSERT

vec0 supports `INSERT` and `DELETE` but **not** in-place `UPDATE`. So
[`update_memory()`](../../src/nexus_memory/core/db.py) deletes the row and
re-inserts it, **explicitly re-using the same `rowid`** so the public id is
stable:

```python
with self.lock, self.conn:
    self.conn.execute("DELETE FROM agent_memory WHERE rowid = ?", (memory_id,))
    self.conn.execute(
        "INSERT INTO agent_memory (rowid, embedding, content, metadata, importance, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (memory_id, blob, content, meta_json, importance, timestamp),
    )
```

### Explicit, preserved timestamps on edits

Because the timestamp is set in code, an edit can choose what to record:
`update_memory()` **preserves the original creation timestamp** rather than
stamping "now", so correcting a fact does not artificially boost its recency
score. (`insert_memory()`, by contrast, stamps the current time.)

---

## A minimal end-to-end example

```python
from nexus_memory.core.config import NexusConfig
from nexus_memory.core.db import NexusDB

db = NexusDB(NexusConfig(db_path="agent.db", dim=768))

vec = [0.0] * 768            # produced by your embedder
mid = db.insert_memory("The user's name is Sam.", vec, importance=2.0)

hits = db.knn_search(vec, k=5)   # [{id, content, importance, timestamp,
                                 #   metadata, distance}, ...]
db.add_edge(mid, mid, relation="self")
db.vacuum()                  # checkpoint WAL + reclaim space
db.close()
```

In practice you rarely touch `NexusDB` directly — the high-level
[`NexusMemory`](../usage/api-reference.md) facade wires up the embedder, writer,
reader, and all layer stores over this one database.

---

## See also

* [Architecture Overview](overview.md) — how the components fit together.
* [Memory Layers](memory-layers.md) and [Diary Layer](diary-layer.md) — what each layer stores and why.
* [Retrieval & Scoring](retrieval-and-scoring.md) — how `distance`, `importance`, and `timestamp` become a score.
* [Configuration](../configuration/nexus-config.md) — `db_path`, `dim`, and the per-layer `*_enabled` flags.
* [Data Flow](../io/data-flow.md) — the write and read paths across the layers.
