# MS1 — Infrastructure Results

Status: complete. Validated by `tests/test_db_setup.py` (12 tests) and `tests/test_schema.py` (12 tests).

## 1.1 Project structure
The `src`-layout package lives at `src/nexus_memory/` with `tests/`, `docs/`, and
`examples/`. Installed editable into the project `.venv` via
`./.venv/Scripts/python.exe -m pip install -e .`.

## 1.2 sqlite-vec integration
`NexusDB.initialize()` (`src/nexus_memory/core/db.py`) loads the vector extension on every
connection:

```python
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
```

The connection is opened with `check_same_thread=False` (the writer runs on background
threads) and `row_factory = sqlite3.Row`. Installed extension version: `sqlite-vec 0.1.9`
on Python 3.13.

## 1.3 Schema
The canonical DDL is `schema.sql`, which stays packaged at `src/nexus_memory/schema.sql`
(alongside `py.typed`); `core/db.py` locates it (packaged copy or project root), reads it,
and substitutes the literal token `__DIM__` with `config.dim` (default 768) before
`executescript`, because a vec0 table's dimension is fixed at creation time. Three objects
are created:

- `agent_memory` — `vec0` virtual table: `embedding float[__DIM__] distance_metric=cosine`,
  plus auxiliary (`+`-prefixed) columns `content TEXT NOT NULL`, `metadata TEXT`,
  `importance FLOAT DEFAULT 1.0`, `timestamp TEXT DEFAULT CURRENT_TIMESTAMP`.
- `system_config (key TEXT PRIMARY KEY, value TEXT)` — bookkeeping.
- `memory_edges (source_id, target_id, relation DEFAULT 'related', PK(source,target,relation))`
  — the 1-hop knowledge graph used by reader graph-expansion.

`timestamp` is stored as ISO-ish UTC text (`YYYY-MM-DD HH:MM:SS`) so the scorer can parse it
for time-decay. **Important:** the `DEFAULT CURRENT_TIMESTAMP` declared on the column is
effectively *inert* — vec0 auxiliary (`+`) columns do **not** honor a column `DEFAULT`, so
`CURRENT_TIMESTAMP` is never applied. The timestamp is therefore set **explicitly** in code
(see §1.5).

## 1.5 Timestamps set explicitly (bug fix)
Because the vec0 `DEFAULT` is ignored, `NexusDB.insert_memory` supplies the timestamp itself
via a `_utc_now_str()` helper (`YYYY-MM-DD HH:MM:SS`, UTC). This was a real bug fix: a `NULL`
timestamp would have silently disabled time-decay scoring. On a content correction,
`update_memory` (DELETE + re-INSERT preserving the `rowid`) **preserves the original
timestamp** rather than stamping "now", so a correction does not reset a memory's recency.
Covered by `tests/test_db_setup.py::test_insert_populates_timestamp` and
`::test_update_preserves_timestamp`.

## 1.4 WAL / performance tuning
`initialize()` sets `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`. WAL enables a
background writer thread and foreground readers to operate concurrently. Tests assert
`PRAGMA journal_mode` returns `wal`.

## SQL ownership and KNN contract
`NexusDB` owns the **connection lifecycle** and the **semantic / vec SQL**. (With the
4-layer v2, the new layer stores — `EpisodicStore` and `ProceduralStore` — own *their own*
tables: `episodic_turns`, `episodic_summaries`, `procedural_rules`, created idempotently
through the shared connection under `with db.lock:`. `NexusDB.lock` is a re-entrant write
lock so all committing writes on the shared connection are serialized — see
`docs/ms7_multilayer.md`.) Vectors are serialized with `sqlite_vec.serialize_float32()`
before binding. KNN search uses the indexed vec0 path:

```sql
SELECT rowid AS id, content, importance, timestamp, metadata, distance
FROM agent_memory
WHERE embedding MATCH ? AND k = ?
ORDER BY distance
```

`distance` is the implicit cosine distance column. vec0 supports INSERT/DELETE but not
in-place UPDATE, so `update_memory` is implemented as DELETE + re-INSERT preserving the
`rowid`. `vacuum()` runs `PRAGMA wal_checkpoint(TRUNCATE)` then `VACUUM`.
