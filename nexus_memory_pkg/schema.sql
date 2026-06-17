-- Nexus Memory canonical DDL.
-- The literal token __DIM__ is replaced by NexusDB with config.dim before
-- the script is executed (the vector dimension is fixed at table-creation
-- time and must match the active embedder).

-- Vector store + auxiliary columns. vec0 auxiliary (non-indexed) columns use
-- the `+` prefix. Cosine ranking requires `distance_metric=cosine` declared
-- at creation time; the MATCH query then uses cosine automatically.
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory USING vec0(
    embedding float[__DIM__] distance_metric=cosine,
    +content TEXT NOT NULL,
    +metadata TEXT,
    +importance FLOAT DEFAULT 1.0,
    +timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

-- System status / bookkeeping.
CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Lightweight knowledge graph: 1-hop relations between memories.
CREATE TABLE IF NOT EXISTS memory_edges (
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT DEFAULT 'related',
    PRIMARY KEY (source_id, target_id, relation)
);
