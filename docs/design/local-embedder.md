# Design: Local-First Semantic Embedder (0.7.0)

> **Status: SHIPPED in 0.7.0.** The module stays **provider-agnostic**: the
> semantic embedder downloads a model once and then runs fully offline — it is
> NOT an API/vendor embedder. The zero-dependency `HashingEmbedder` remains the
> default; the neural embedder (`FastEmbedEmbedder`, fastembed + bge-base-en-v1.5,
> 768-dim) is an opt-in pip extra (`nexus-memory[local-embeddings]`), selectable
> via `NexusConfig(embedder_backend="fastembed")`, with a dim+provenance guard and
> a `python -m nexus_memory.reindex` re-embed tool. Verified: 237 tests + a live
> demo A/B (the paraphrased "what's my project's name?" recall the lexical default
> missed now resolves). See [the embedders guide](../usage/embedders.md).

## Why

The default `HashingEmbedder` is lexical (signed feature-hashing / bag-of-words):
recall only matches on **shared words**. Proven live — the query *"what is the
exact name of my project and which language?"* did **not** retrieve the stored
fact *"I'm building a Rust CLI tool called Tideglass"* (A/B: that fact ranked
**last, score 0.0** under Hashing vs **~rank 2** under a real semantic embedder).
The chat model only sees what recall injects, so lexical recall makes it "forget"
paraphrased facts. 0.7.0 adds a real **semantic** embedder while keeping the
module local-first and vendor-free.

## Decisions (locked)

- **Stack: `fastembed` (Qdrant) on ONNX Runtime — torch-free.** Chosen over
  sentence-transformers (+torch, ~1.5–2.5 GB, painful Windows wheels) and raw
  ONNX (would force us to hand-roll tokenization/pooling/prefixing/caching).
- **Default model: `BAAI/bge-base-en-v1.5` (dim = 768).** Decisive: 768 == the
  existing `DEFAULT_DIM`/DB vector width, so adopting it needs **NO SQL schema
  change** — migration is a pure re-embed. ~210 MB one-time download, a few ms per
  text on CPU, MIT-family license (verify the Qdrant ONNX repackage license too).
- **`HashingEmbedder` stays the hard default.** A bare `pip install nexus-memory`
  remains zero-extra-dependency and offline. The neural path is opt-in:
  `pip install "nexus-memory[local-embeddings]"`.
- **Scope: fastembed only for 0.7.0.** The ultra-light `model2vec`/potion tier
  (512-dim, pure-numpy) is deferred to a follow-up — it would force a schema/dim
  rebuild, so it is not in 0.7.0.

## Hardening (from adversarial review — must be handled in implementation)

1. **Embedder provenance / no silent space-mixing (critical).** Switching
   Hashing→bge (both 768) is silently accepted by the DB, but old and new vectors
   live in **different spaces** → corrupt search until re-embedded. Record the
   active embedder backend + model + dim in the (currently unused) `system_config`
   table on first write; on construction, if the stored provenance differs from
   the configured embedder, refuse with a clear error that points to the re-index
   tool (do not silently mix).
2. **Dim guard against the REAL DB dim.** The schema dim is fixed at table
   creation (`db.py` substitutes `__DIM__`). Guard `embedder.dim == actual DB dim`
   (read from the existing DB / `system_config`), not just `config.dim` — and
   apply it to **both** the config-built and the injected (`embedder=`) paths.
3. **True offline.** `local_files_only=True` alone is not a guarantee (fastembed
   has a GCS-fallback path; `HF_HUB_OFFLINE` can mis-trigger it). Detect a warm
   cache and pass `local_files_only=True`; raise a clear, actionable error when
   the model is absent **and** offline is forced (tell the host to warm up online
   once). Log a one-line "downloading model (~210 MB) to <cache>" on first use.
4. **Pin `fastembed[cpu]`** in the extra so onnxruntime-gpu is never pulled.
5. **Deterministic cache dir** (a configurable `embedder_cache_dir`, default a
   predictable `nexus_models/` resolvable location, falling back to `HF_HOME`) —
   not an ephemeral temp dir. Mind **Windows MAX_PATH (260)** with the deep HF
   cache layout.
6. **License**: verify the actual downloaded ONNX artifact's license (the Qdrant
   repackage), not only the upstream BAAI model card.
7. **Avoid double L2-normalization** — bge `*-en-v1.5` already returns normalized
   vectors; normalize once.
8. **Transactional re-index** — write to a new file and swap last (retain `.bak`);
   handle SQLite WAL/`-shm` sidecars and Windows file locks; never leave a
   half-migrated live DB.

## API sketch

New lazily-imported adapter in `core/embeddings.py` (fits the `Embedder` ABC):

```python
class FastEmbedEmbedder(Embedder):
    def __init__(self, model_name="BAAI/bge-base-en-v1.5", *, cache_dir=None, offline=False, **kw):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError('FastEmbedEmbedder requires fastembed. '
                              'Install: pip install "nexus-memory[local-embeddings]"') from exc
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir,
                                    local_files_only=offline, **kw)
        self.model_name = model_name
        self.dim = len(next(iter(self._model.embed(["__dim_probe__"]))))  # actual width

    def encode(self, text): return _maybe_normalize(next(iter(self._model.embed([text]))))
    def encode_batch(self, texts): return [_maybe_normalize(v) for v in self._model.embed(texts)]
```

New `NexusConfig` knobs (NOT schema changes): `embedder_backend: str = "hashing"`
(`"hashing"|"fastembed"`), `embedder_model: str | None`, `embedder_cache_dir:
str | None`, `embedder_offline: bool = False`. The orchestrator builds the
embedder from config when no explicit `embedder=` is passed, applies the dim +
provenance guard, and flushes the semantic cache on any embedder change.

## Re-embed / migration

Switching embedder changes the vector space → all stored vectors must be
re-embedded. Ship a first-class, **transactional** `nexus_memory.core.reindex`
(function + `python -m nexus_memory.reindex --db ... --backend fastembed`):
- **Same-dim (Hashing 768 → bge-base 768, the recommended path):** `SELECT id,
  content, metadata`, recompute with `encode_batch`, `UPDATE` each vector; no
  schema change, no approval needed; update `system_config` provenance.
- **Different-dim (future, e.g. the 512-dim static tier):** approval-gated — build
  a fresh DB with the new `__DIM__`, stream re-embedded rows, atomic swap, keep
  `.bak`.

## Phased plan

1. **Scaffolding** — `local-embeddings = ["fastembed[cpu]>=0.8"]` extra; the four
   `NexusConfig` knobs; version 0.6.0 → 0.7.0.
2. **Adapter** — `FastEmbedEmbedder` (lazy import, dim probe, single L2-normalize,
   configurable cache, robust offline).
3. **Wiring + guards** — `_build_embedder(config)`; dim guard vs real DB dim on
   both config-built and injected paths; provenance in `system_config`; flush the
   semantic cache on embedder change.
4. **Migration** — transactional `core/reindex.py` (same-dim re-embed;
   write-and-swap), `python -m nexus_memory.reindex`.
5. **Tests** — A/B recall regression (the Tideglass paraphrase now retrieves the
   fact under fastembed); dim-guard + provenance-mismatch rejection; round-trip
   re-index (count/content preserved, vectors changed); offline-after-download
   with a warm cache. The neural tests are gated/skipped when fastembed is absent
   so the core suite stays dependency-free.
6. **Docs + release** — update docs (extension-points, persistence, api-reference,
   use-cases); document install + one-time download size/cache + offline warm-up;
   `changelog/0.7.0.md`; README; page branch. Verify in main, then extensive demo
   live-test, then ask before commit/push.

## Deferred (not 0.7.0)

- The `model2vec`/potion **ultra-light static tier** (512-dim, pure-numpy) — needs
  the approval-gated different-dim rebuild path.
- A multilingual default model.
