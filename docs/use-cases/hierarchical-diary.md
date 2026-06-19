# Use Case: Hierarchical Diary via Outbox

This page shows how a host integrates Nexus Memory's optional fifth layer — a self-managing, bounded **diary** that compresses raw dialogue into a session-keyed pyramid of narrative summaries **without the module ever calling an LLM itself**. You enable it with `NexusMemory(diary=True)`, then run a small **drain loop**: `pending_summaries()` yields jobs, your code runs each job's `prompt` on *any* model, and `submit_summary(job_id, text)` folds the result back into the pyramid.

> **Session semantics.** The diary is keyed by **session** (one `NexusMemory` process run, identified by `orchestrator.session_id`), not by UTC day. The **current** session's diary is **always injected** into context — so the conversation stays available across session boundaries — and a single growing **persistent summary** accumulates everything older. A *day*-keyed earlier version existed in 0.3.x; the session rework is a breaking change scoped to the diary layer only.

For the layer's internals (tables, trigger state machine, context injection) see the [Diary Layer architecture](../architecture/diary-layer.md). For the config knobs see [Diary Config](../configuration/diary-config.md). This page is the **integration recipe**.

## The core idea: Nexus owns the prompt, the host owns the model

The defining design choice is a **handoff outbox**. When a summary is due, the diary does not call a model — it **enqueues a job** (a `prompt` + `prior_summary` + `input`) into the `summarization_jobs` table. There are two job kinds: `session` (the rolling per-session entry) and `summary` (the single growing persistent summary). The host drains that outbox whenever it likes, runs the job on whatever model it wants (an OpenRouter call, a local model, even a human), and hands the text back via `submit_summary`.

This keeps the module:

- **provider-agnostic** — it never imports an LLM SDK;
- **fully offline-testable** — a job is just data, driven manually;
- **async by construction** — `ingest` only *schedules* (cheap, non-blocking); the LLM work happens out-of-band. A stale outbox merely makes the diary lag, never loses data.

```
ingest ──(due?)──▶ enqueue job ──▶ [ summarization_jobs (outbox) ]
                                         │  host pulls
                       pending_summaries() ──▶ host runs prompt+context on ITS model
                                         ▼
                       submit_summary(job_id, text) ──▶ Nexus persists into L1 / L2
```

## Enabling the layer

The diary is **off by default** and fully additive. With no `DiaryConfig`, the layer is never constructed: no new tables, no new context sections, no new actions. Opt in explicitly:

```python
from nexus_memory import NexusMemory

memory = NexusMemory(db_path="nexus.db", diary=True)
```

[`DiaryConfig`](../configuration/diary-config.md) (`src/nexus_memory/layers/diary/config.py`) is the diary's own dataclass — nothing is added to `NexusConfig`:

| Field | Symbol | Meaning | Default |
| :-- | :-- | :-- | :-- |
| `enabled` | — | master switch; when `False` the layer is never built | `False` |
| `update_every` | `N` | interactions between rolling session updates | `5` |
| `diary_window` | — | turns (×2 rows) re-sent per rolling session job (overlap) | `20` |
| `max_sentences` | — | upper bound of the session entry's `2-N` sentence range | `50` |
| `sessions_per_summary` | — | finalized session diaries folded into the persistent summary per extension | `6` |
| `inject_sessions` | `K` | **additional** previous finalized session diaries injected (the current session is always injected); `0 ≤ K ≤ 6` | `1` |
| `summary_max_sentences` | — | cap of the single growing persistent summary | `300` |

When the layer is off, `memory._diary is None`, the two diary actions are unknown actions (a normal validation error), and the convenience wrappers return `{"status": "error", "error": "diary layer not enabled"}`.

## The session pyramid

The diary maintains three levels of granularity, each coarser and longer-lived than the last:

| Level | Table | What it holds | Cadence |
| :-- | :-- | :-- | :-- |
| **L0** | `episodic_turns` (owned by [Layer II](../architecture/memory-layers.md)) | raw user/assistant turns | every ingest |
| **L1** | `diary_sessions` | 1 rolling summary per **session** | updated every `N=5` interactions |
| **L2** | `persistent_summary` | one **single growing** summary across all folded sessions | extended every `sessions_per_summary=6` finalized sessions |

- **L1 — `diary_sessions`**: one **rolling** narrative per session, written as the assistant's own **first-person prose**. Each row carries a monotone `seq` (1, 2, 3 …) that orders sessions (uuids do not sort) and drives the current/previous split and the 6-session fold. Every `N` interactions a `session` job is enqueued whose `prior_summary` is the session's current text and whose `input` is a rolling, **overlapping** window of up to `diary_window=20` turns (both roles), scoped to that session, so the entry is *refined in place* (reconciling the overlap) rather than rewritten from scratch.
- **L2 — `persistent_summary`**: a **single growing** first-person summary held in **one row**. When `sessions_per_summary` finalized session diaries have accumulated, a `summary` job folds them into that one row — **extending** it (never freezing, never a ring), capped at `summary_max_sentences=300` sentences. The earlier day-keyed design used a fixed ring of sections; the session rework replaces it with this one accumulating summary.

The full trigger state machine (session rollover, session cadence, the 6-session fold, the persistent summary) lives in `DiaryScheduler` (`src/nexus_memory/layers/diary/scheduler.py`) and is documented in the [Diary Layer architecture](../architecture/diary-layer.md). For integration you only need the two host-facing actions below.

## The drain loop

Two surfaces drive the handoff. Both are exposed as `process()` actions **and** as convenience methods on `NexusMemory`.

### `pending_summaries(limit=None)`

Returns the outbox's pending **handoff job objects**, oldest-first. Each job is shaped by `DiaryLayer._to_handoff` (`src/nexus_memory/layers/diary/layer.py`):

| Key | Type | Meaning |
| :-- | :-- | :-- |
| `job_id` | `str` (uuid4) | pass back to `submit_summary` |
| `kind` | `"session"` \| `"summary"` | which level this job summarizes |
| `session` | `str` \| `None` | the `session_id` (present for `session` jobs; `None` for `summary`) |
| `prompt` | `str` | the Nexus-owned instruction — **forward this verbatim to your model** |
| `prior_summary` | `str` \| `None` | the current L1/L2 summary — drives **rolling** refinement |
| `input` | `list[dict]` | `session`: a rolling, **overlapping** window of up to `diary_window` turns `{id, role, content, timestamp}` (both roles); `summary`: finalized session summaries `{session_id, summary}` |

As JSON:

```json
{
  "job_id": "uuid",
  "kind": "session",
  "session": "8f1c…-uuid4",
  "prompt": "You are the assistant. Keep a personal diary of this session, written in your own voice in the first person ('I'). ...",
  "prior_summary": "…or null…",
  "input": [
    {"id": 11, "role": "user", "content": "...", "timestamp": "..."},
    {"id": 12, "role": "assistant", "content": "...", "timestamp": "..."}
  ]
}
```

The two prompts Nexus ships (`src/nexus_memory/layers/diary/prompts.py`) are:

- **`SESSION_PROMPT`** (a template — `{max_sentences}` is filled in at enqueue time from `DiaryConfig.max_sentences`) — *"You are the assistant. Keep a personal diary of this session, written in your own voice in the first person ('I'). Given your prior entry and the recent turns of the conversation — both what the user said and what you said in reply — produce an updated entry of 2-{max_sentences} sentences … Write it as flowing prose … never use bullet points, numbered lists, headings, or any categorical structure … The recent turns may include turns already reflected in your prior entry; do not restate them, only incorporate genuinely new developments. When a newer turn corrects or contradicts your prior entry, treat the newer turn as authoritative …"*
- **`SUMMARY_PROMPT`** (a template — `{summary_max_sentences}` is filled in at enqueue time from `DiaryConfig.summary_max_sentences`) — *"You are the assistant, keeping one growing persistent summary of everything across your sessions, written in your own first-person voice. Given your prior persistent summary and these new session entries, extend it into a single coherent first-person prose summary of up to {summary_max_sentences} sentences — never lists or headings. Preserve the throughline across all sessions, weave in the genuinely new developments, and drop redundancy rather than restating what the prior summary already covered."*

> The session window **overlaps** the prior entry (it re-sends up to `diary_window` turns, not a strict delta), so a faithful host must **reconcile** — merge/revise rather than naively append — exactly what the prompt instructs. The example stub below demonstrates this.

The host composes its own model call however it wants — e.g. system message = `prompt`, user message = `prior_summary` followed by a rendering of `input`.

### `submit_summary(job_id, summary)`

Folds a model-produced text back into the pyramid. Returns `{status, applied}`:

| Field | Values |
| :-- | :-- |
| `status` | `"success"`, `"superseded"`, or `"not_found"` |
| `applied` | `"session"` \| `"summary"` (the kind applied; present in every response except `not_found`) |

**Apply is idempotent.** Submitting against a `done`, `superseded`, or unknown `job_id` is a safe no-op returning a status note (`success` no-op / `superseded` / `not_found`) — never an error or a raise. This means a host can re-run the drain loop after a crash without corrupting the pyramid.

> Note: `submit_summary` requires a non-empty `summary` — `SubmitSummaryRequest` validates both `job_id` and `summary` with `min_length=1`.

### A minimal drain helper

You can drive the outbox by hand — `pending_summaries()` → run each job → `submit_summary` — but Nexus ships a one-call convenience for exactly that loop, [`drain_diary(run_job)`](../usage/api-reference.md). Your `run_job` is a `(job: dict) -> str` callable: it receives a whole handoff job and returns the model's text. Straight from [`examples/diary_outbox.py`](../../examples/diary_outbox.py):

```python
def secondary_model(job: dict) -> str:
    """Stand-in for the host's SECONDARY model. A real host forwards
    job["prompt"] together with job["prior_summary"] and job["input"]
    to its model of choice — Nexus owns the instruction, the host runs it.
    """
    ...  # return the model's first-person prose

applied = memory.drain_diary(secondary_model)  # runs every pending job, folds each result back
```

`drain_diary` calls `run_job` for each pending job and folds any non-empty result back via `submit_summary`, returning the count applied (and `0` when the diary is off). Call it whenever convenient — after each turn, on a timer, or as a batch job at shutdown. Because `ingest` only schedules, draining can lag arbitrarily without data loss.

## Offline deterministic walkthrough

The runnable example [`examples/diary_outbox.py`](../../examples/diary_outbox.py) wires the whole loop against a **trivial, deterministic stand-in "model"**, so it runs entirely offline (no network, no API key). A real host swaps `secondary_model` for an actual model call — nothing else changes.

The stand-in takes a whole handoff `job`, writes the assistant's own **first-person prose**, folds in **both roles** (what the user said and what I said), and **reconciles** the overlapping window against the prior entry — keeping the prior text and weaving in only the genuinely new developments (proving the rolling behavior):

```python
def secondary_model(job: dict) -> str:
    items = job["input"]
    prior = job.get("prior_summary") or ""
    fresh = [t for t in items if t["content"] not in prior]  # skip already-reflected turns
    said = "; ".join(t["content"] for t in fresh if t.get("role") == "user")
    replied = "; ".join(t["content"] for t in fresh if t.get("role") == "assistant")
    new = "; ".join(b for b in (f"the user told me: {said}" if said else "",
                                f"I replied: {replied}" if replied else "") if b)
    if prior:
        return f"{prior} Continuing on, {new}." if new else prior
    return f"Today {new}." if new else "Nothing notable happened today."
```

The driver: opt in to the layer, ingest five interactions to cross the `N=5` cadence, wait for the async writer + diary consolidator, then drain and inspect:

```python
import tempfile
from pathlib import Path
from nexus_memory import DiaryConfig, NexusMemory

db_path = str(Path(tempfile.mkdtemp()) / "nexus.db")
memory = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
try:
    interactions = [
        ("My name is Chris and I'm building a memory library.",
         "Nice to meet you, Chris."),
        ("I prefer Python and my deadline is next Friday.",
         "Noted — Python, and a Friday deadline."),
        ("My favorite color is purple.", "Purple it is."),
        ("I'm using SQLite for storage.", "SQLite is a solid choice."),
        ("The library has five memory layers.", "Five layers — ambitious."),
    ]
    for query, response in interactions:
        memory.process({
            "action": "ingest",
            "interaction": {"query": query, "response": response},
        })
    memory.wait()  # let the async writer + diary consolidator finish

    # 1. The diary scheduled a job; the host drains the outbox on its model.
    applied = memory.drain_diary(secondary_model)
    print(f"drained {applied} summary job(s) from the outbox")

    # 2. Inspect the pyramid: L1 session diaries + the single L2 persistent summary.
    state = memory.inspect(type="diary")["data"]
    for sess in state["sessions"]:
        print(f"diary session {sess['seq']}: {sess['summary']}")
    print(f"persistent summary: {state['summary']}")  # one row or None
finally:
    memory.close()
```

What happens, step by step:

1. **Ingest × 5** crosses the `N=5` cadence, so on the fifth interaction the scheduler enqueues exactly one `session` job for the **current** session. Its `prior_summary` is empty (no prior entry) and its `input` is the ten turns (five user + five assistant) of the session — the rolling window holds the whole session, well under the `diary_window=20` turn cap.
2. **`memory.wait()`** blocks until the async writer and the diary consolidator have committed — only then is the job durably in the outbox.
3. **`drain_diary`** pulls the one pending job, runs `secondary_model`, and `submit_summary` writes the text into `diary_sessions` and advances `covered_through`. It returns `1`.
4. **`inspect(type="diary")`** returns `{"sessions": [...], "summary": {...} | None}` — one session diary, and `summary` is `None` (the persistent summary is only created once `sessions_per_summary=6` finalized sessions have accumulated to fold).
5. **`memory.close()`** calls the diary's `finalize()`, which marks the current session finalized and enqueues its final session job. Jobs persist in SQLite for the **next session's** host to drain — the outbox survives reopen.

> The folded `<persistent_summary>` appears only once you accumulate `sessions_per_summary` finalized sessions. The **current** session's `<diary>` and (with `inject_sessions ≥ 1`) the previous session's `<diary>` inject as soon as they have a non-empty summary. To exercise the fold deterministically, drive several sessions — inject a `session` callable into `DiaryScheduler` as the test suite does — see the [Diary Layer architecture](../architecture/diary-layer.md).

Run it (once the package is installed):

```bash
python examples/diary_outbox.py
```

## Reading the pyramid back

Two read paths surface the diary:

- **`inspect(type="diary")`** → `{"status": ..., "data": {"sessions": [...], "summary": {...} | None}}`. The `sessions` are the L1 per-session diaries; `summary` is the single growing L2 persistent summary row (or `None` until the first fold). This action is served by the diary layer itself (not added to core `InspectRequest`) and errors when the diary is off.
- **`assemble`** (the [request/response](../io/request-response.md) read path) gains additive keys when the layer is active: `"diary": [{session, seq, current, summary}, ...]` — the **current** session (`current=True`, always injected if it has a summary) followed by up to `inject_sessions` previous finalized sessions — and `"persistent_summary": {summary, session_count, first_session, last_session} | None` (the single growing row). These appear inside `<memory_context>` as `<diary session="current" seq="...">` / `<diary session="..." seq="...">` and one `<persistent_summary>` section, **with no `id="..."` attributes** — preserving the backward-compatible needle invariant. When the layer is off these keys are absent.

```python
res = memory.process({"action": "assemble", "query": "what did I ship?"})
print(res.get("diary"), res.get("persistent_summary"))
```

See [Data Flow](../io/data-flow.md) for where these sections land in the assembled context, and [Transparency](../usage/transparency.md) for the full `inspect` surface.

## Integration checklist

- Enable with `NexusMemory(diary=True)`; tune `update_every`, `diary_window`, `max_sentences`, `sessions_per_summary`, `inject_sessions`, `summary_max_sentences` with an explicit `DiaryConfig` via [Diary Config](../configuration/diary-config.md).
- After ingests (and before relying on the outbox), call `memory.wait()` so scheduling has committed.
- Run a drain loop: `pending_summaries()` → run `job["prompt"]` on your model with `job["prior_summary"]` + `job["input"]` → `submit_summary(job["job_id"], text)`, or just `drain_diary(run_job)` for the same loop in one call.
- Drain on your own schedule — a lagging outbox is safe; the diary just trails behind.
- Treat `submit_summary` as idempotent: re-draining after a crash is safe (`not_found` / no-op on resolved jobs).
- Let `close()` finalize the current session; pending jobs persist for the next session to drain. The current session's diary is always injected, so the next run resumes with it in context.

## Related pages

- [Diary Layer architecture](../architecture/diary-layer.md) — tables, trigger state machine, session fold, context injection.
- [Diary Config](../configuration/diary-config.md) — every knob and its effect.
- [Agent Memory](agent-memory.md) — the broader memory model the diary sits on top of.
- [Request / Response](../io/request-response.md) and [Data Flow](../io/data-flow.md) — how diary sections surface in `assemble`.
- Source: [`layers/diary/scheduler.py`](../../src/nexus_memory/layers/diary/scheduler.py), [`layers/diary/layer.py`](../../src/nexus_memory/layers/diary/layer.py), [`examples/diary_outbox.py`](../../examples/diary_outbox.py).
