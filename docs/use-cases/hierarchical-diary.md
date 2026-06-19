# Use Case: Hierarchical Diary via Outbox

This page shows how a host integrates Nexus Memory's optional fifth layer — a self-managing, bounded **diary** that compresses raw dialogue into a time-pyramid of narrative summaries **without the module ever calling an LLM itself**. You enable it with `NexusMemory(diary=True)`, then run a small **drain loop**: `pending_summaries()` yields jobs, your code runs each job's `prompt` on *any* model, and `submit_summary(job_id, text)` folds the result back into the pyramid.

For the layer's internals (tables, trigger state machine, context injection) see the [Diary Layer architecture](../architecture/diary-layer.md). For the config knobs see [Diary Config](../configuration/diary-config.md). This page is the **integration recipe**.

## The core idea: Nexus owns the prompt, the host owns the model

The defining design choice is a **handoff outbox**. When a summary is due, the diary does not call a model — it **enqueues a job** (a `prompt` + `prior_summary` + `input`) into the `summarization_jobs` table. The host drains that outbox whenever it likes, runs the job on whatever model it wants (an OpenRouter call, a local model, even a human), and hands the text back via `submit_summary`.

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
| `update_every` | `N` | interactions between rolling daily updates | `5` |
| `diary_window` | — | turns (×2 rows) re-sent per rolling daily job (overlap) | `20` |
| `max_sentences` | — | upper bound of the entry's `2-N` sentence range | `50` |
| `section_size` | `SECTION_SIZE` | daily diaries folded into one persistent section | `7` |
| `max_sections` | `M` | persistent sections kept (ring; oldest overwritten) | `8` |
| `inject_days` | `K` | finalized daily diaries injected into context | `1` |

When the layer is off, `memory._diary is None`, the two diary actions are unknown actions (a normal validation error), and the convenience wrappers return `{"status": "error", "error": "diary layer not enabled"}`.

## The time-pyramid

The diary maintains three levels of granularity, each coarser and longer-lived than the last:

| Level | Table | What it holds | Cadence |
| :-- | :-- | :-- | :-- |
| **L0** | `episodic_turns` (owned by [Layer II](../architecture/memory-layers.md)) | raw user/assistant turns | every ingest |
| **L1** | `diary_days` | 1 rolling summary per UTC **day** | updated every `N=5` interactions |
| **L2** | `persistent_sections` | 1 summary per `SECTION_SIZE=7` daily diaries | ring of `M=8` slots (≈ 56 days) |

- **L1 — `diary_days`**: one **rolling** narrative per UTC day, written as the assistant's own **first-person prose**. Every `N` interactions a daily job is enqueued whose `prior_summary` is the day's current text and whose `input` is a rolling, **overlapping** window of up to `diary_window=20` turns (both roles), so the entry is *refined in place* (reconciling the overlap) rather than rewritten from scratch.
- **L2 — `persistent_sections`**: one coarser summary per `SECTION_SIZE` finalized daily diaries, held in a **ring of `M` slots**. When the ring is full, the **oldest section is overwritten** — deliberate, bounded *deep forgetting*.

The full trigger state machine (day rollover, daily cadence, section folding, the ring) lives in `DiaryScheduler` (`src/nexus_memory/layers/diary/scheduler.py`) and is documented in the [Diary Layer architecture](../architecture/diary-layer.md). For integration you only need the two host-facing actions below.

## The drain loop

Two surfaces drive the handoff. Both are exposed as `process()` actions **and** as convenience methods on `NexusMemory`.

### `pending_summaries(limit=None)`

Returns the outbox's pending **handoff job objects**, oldest-first. Each job is shaped by `DiaryLayer._to_handoff` (`src/nexus_memory/layers/diary/layer.py`):

| Key | Type | Meaning |
| :-- | :-- | :-- |
| `job_id` | `str` (uuid4) | pass back to `submit_summary` |
| `kind` | `"daily"` \| `"section"` | which level this job summarizes |
| `period` | `str` \| `None` | the `YYYY-MM-DD` day (present for `daily` jobs; `None` for `section`) |
| `prompt` | `str` | the Nexus-owned instruction — **forward this verbatim to your model** |
| `prior_summary` | `str` \| `None` | the current L1/L2 summary — drives **rolling** refinement |
| `input` | `list[dict]` | `daily`: a rolling, **overlapping** window of up to `diary_window` turns `{id, role, content, timestamp}` (both roles); `section`: finalized day summaries `{period, summary}` |

As JSON:

```json
{
  "job_id": "uuid",
  "kind": "daily",
  "period": "2026-06-16",
  "prompt": "You are the assistant. Keep a personal diary of your day, written in your own voice in the first person ('I'). ...",
  "prior_summary": "…or null…",
  "input": [
    {"id": 11, "role": "user", "content": "...", "timestamp": "..."},
    {"id": 12, "role": "assistant", "content": "...", "timestamp": "..."}
  ]
}
```

The two prompts Nexus ships (`src/nexus_memory/layers/diary/prompts.py`) are:

- **`DAILY_PROMPT`** (a template — `{max_sentences}` is filled in at enqueue time from `DiaryConfig.max_sentences`) — *"You are the assistant. Keep a personal diary of your day, written in your own voice in the first person ('I'). Given your prior entry and the recent turns of the conversation — both what the user said and what you said in reply — produce an updated entry of 2-{max_sentences} sentences … Write it as flowing prose … never use bullet points, numbered lists, headings, or any categorical structure … The recent turns may include turns already reflected in your prior entry; do not restate them, only incorporate genuinely new developments …"*
- **`SECTION_PROMPT`** — *"You are the assistant, keeping a rolling multi-day record in your own first-person voice. Given your prior section summary and a new day's diary entry, weave them into a single coherent paragraph of flowing prose — never lists or headings — that preserves the throughline across the period."*

> The daily window **overlaps** the prior entry (it re-sends up to `diary_window` turns, not a strict delta), so a faithful host must **reconcile** — merge/revise rather than naively append — exactly what the prompt instructs. The example stub below demonstrates this.

The host composes its own model call however it wants — e.g. system message = `prompt`, user message = `prior_summary` followed by a rendering of `input`.

### `submit_summary(job_id, summary)`

Folds a model-produced text back into the pyramid. Returns `{status, applied}`:

| Field | Values |
| :-- | :-- |
| `status` | `"success"`, `"superseded"`, or `"not_found"` |
| `applied` | `"daily"` \| `"section"` (the kind applied; present in every response except `not_found`) |

**Apply is idempotent.** Submitting against a `done`, `superseded`, or unknown `job_id` is a safe no-op returning a status note (`success` no-op / `superseded` / `not_found`) — never an error or a raise. This means a host can re-run the drain loop after a crash without corrupting the pyramid.

> Note: `submit_summary` requires a non-empty `summary` — `SubmitSummaryRequest` validates both `job_id` and `summary` with `min_length=1`.

### A minimal drain helper

Straight from [`examples/diary_outbox.py`](../../examples/diary_outbox.py):

```python
def drain_outbox(memory: NexusMemory) -> int:
    """Run every pending summary job on the host's model and submit the result.

    This is the host's responsibility — Nexus only *schedules* the jobs.
    """
    jobs = memory.pending_summaries()
    for job in jobs:
        text = your_model(job["prompt"], job["prior_summary"], job["input"])
        memory.submit_summary(job["job_id"], text)
    return len(jobs)
```

Call `drain_outbox(memory)` whenever convenient — after each turn, on a timer, or as a batch job at shutdown. Because `ingest` only schedules, draining can lag arbitrarily without data loss.

## Offline deterministic walkthrough

The runnable example [`examples/diary_outbox.py`](../../examples/diary_outbox.py) wires the whole loop against a **trivial, deterministic stand-in "model"**, so it runs entirely offline (no network, no API key). A real host swaps `fake_model` for an actual model call — nothing else changes.

The stand-in writes the assistant's own **first-person prose**, folds in **both roles** (what the user said and what I said), and **reconciles** the overlapping window against the prior entry — keeping the prior text and weaving in only the genuinely new developments (proving the rolling behavior):

```python
def fake_model(prompt: str, prior_summary: str | None, turns: list[dict]) -> str:
    prior = prior_summary or ""
    fresh = [t for t in turns if t["content"] not in prior]  # skip already-reflected turns
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
from nexus_memory import NexusMemory

db_path = str(Path(tempfile.mkdtemp()) / "nexus.db")
memory = NexusMemory(db_path=db_path, diary=True)
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

    # 1. The diary scheduled a job; the host drains the outbox.
    applied = drain_outbox(memory)
    print(f"drained {applied} summary job(s) from the outbox")

    # 2. Inspect the time-pyramid: L1 daily diaries + L2 persistent sections.
    state = memory.inspect(type="diary")["data"]
    for day in state["days"]:
        print(f"diary {day['period']}: {day['summary']}")
    print(f"persistent sections: {len(state['sections'])}")
finally:
    memory.close()
```

What happens, step by step:

1. **Ingest × 5** crosses the `N=5` cadence, so on the fifth interaction the scheduler enqueues exactly one `daily` job for today. Its `prior_summary` is empty (no prior entry) and its `input` is the ten turns (five user + five assistant) of the day — the rolling window holds the whole day, well under the `diary_window=20` turn cap.
2. **`memory.wait()`** blocks until the async writer and the diary consolidator have committed — only then is the job durably in the outbox.
3. **`drain_outbox`** pulls the one pending job, runs `fake_model`, and `submit_summary` writes the text into `diary_days` and advances `covered_through`. It returns `1`.
4. **`inspect(type="diary")`** returns `{"days": [...], "sections": [...]}` — one daily diary, zero sections (section folding only kicks in across day boundaries, when a finalized day is folded into the L2 ring).
5. **`memory.close()`** calls the diary's `finalize()`, which marks the current day finalized and enqueues its final daily job. Jobs persist in SQLite for the **next session's** host to drain — the outbox survives reopen.

> Section folding and the `<diary>` / `<persistent_summary>` context injection only appear once you cross day boundaries (the example stays within a single UTC day). To exercise those paths deterministically, inject a `today` callable into `DiaryScheduler` as the test suite does — see the [Diary Layer architecture](../architecture/diary-layer.md).

Run it (once the package is installed):

```bash
python examples/diary_outbox.py
```

## Reading the pyramid back

Two read paths surface the diary:

- **`inspect(type="diary")`** → `{"status": ..., "data": {"days": [...], "sections": [...]}}`. The `days` are L1 daily diaries; `sections` are the L2 persistent ring. This action is served by the diary layer itself (not added to core `InspectRequest`) and errors when the diary is off.
- **`assemble`** (the [request/response](../io/request-response.md) read path) gains additive keys when the layer is active: `"diary": {"day", "summary"} | None` (the previous finalized day, `K=1`) and `"persistent_summary": [{seq, days, summary}, ...]`. These appear inside `<memory_context>` as `<diary>` and `<persistent_summary>` sections, **with no `id="..."` attributes** — preserving the backward-compatible needle invariant. When the layer is off these keys are absent.

```python
res = memory.process({"action": "assemble", "query": "what did I ship?"})
print(res.get("diary"), res.get("persistent_summary"))
```

See [Data Flow](../io/data-flow.md) for where these sections land in the assembled context, and [Transparency](../usage/transparency.md) for the full `inspect` surface.

## Integration checklist

- Enable with `NexusMemory(diary=True)`; tune `update_every`, `diary_window`, `max_sentences`, `section_size`, `max_sections` with an explicit `DiaryConfig` via [Diary Config](../configuration/diary-config.md).
- After ingests (and before relying on the outbox), call `memory.wait()` so scheduling has committed.
- Run a drain loop: `pending_summaries()` → run `job["prompt"]` on your model with `job["prior_summary"]` + `job["input"]` → `submit_summary(job["job_id"], text)`.
- Drain on your own schedule — a lagging outbox is safe; the diary just trails behind.
- Treat `submit_summary` as idempotent: re-draining after a crash is safe (`not_found` / no-op on resolved jobs).
- Let `close()` finalize the current day; pending jobs persist for the next session to drain.

## Related pages

- [Diary Layer architecture](../architecture/diary-layer.md) — tables, trigger state machine, ring/forgetting, context injection.
- [Diary Config](../configuration/diary-config.md) — every knob and its effect.
- [Agent Memory](agent-memory.md) — the broader memory model the diary sits on top of.
- [Request / Response](../io/request-response.md) and [Data Flow](../io/data-flow.md) — how diary sections surface in `assemble`.
- Source: [`layers/diary/scheduler.py`](../../src/nexus_memory/layers/diary/scheduler.py), [`layers/diary/layer.py`](../../src/nexus_memory/layers/diary/layer.py), [`examples/diary_outbox.py`](../../examples/diary_outbox.py).
