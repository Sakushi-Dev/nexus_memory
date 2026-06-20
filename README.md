# Nexus Chat — Demo TUI

A console chat client that shows the **Nexus Memory Module** working in a real
setting. It uses **OpenRouter** as the LLM provider and gives the model
persistent long-term memory: every turn first *recalls* relevant facts from
memory and injects them into the prompt, then *ingests* the new exchange back
into memory (where atomic facts are extracted and de-duplicated).

The memory lives on disk (`data/chat_memory.db`, SQLite + sqlite-vec), so it
survives restarts — close the app, reopen it, and it still remembers you.

## Architecture — modular by design

The demo is deliberately split into small, single-responsibility components under
`chatapp/`, and the **entire coupling to the Nexus module lives in one file**
(`chatapp/memory.py`). Swap that one file for a different backend and nothing else
changes — that narrow seam is the whole point of the layout.

```
chat.py                 thin entry point (UTF-8 setup + main)
chatapp/
  config.py             settings: .env + CLI flags        (no Nexus, no LLM)
  llm.py                the LLM provider (OpenRouter)      (no Nexus, no UI)
  tokens.py             token counting for the rolling context window (tiktoken)
  memory.py     ◀────── THE BOUNDARY: the only file importing nexus_memory;
                        a domain facade (MemoryService), incl. the Layer V
                        outbox drain (host runs Nexus's jobs on the LLM)
  trace.py              observability: captures the module's internal logs
  commands.py           slash-command dispatch + the command-output renderables
  tui.py                the full-screen Textual TUI         (default frontend)
  app.py                composition root + the classic line frontend (--classic)
  selftest.py           offline, network-free end-to-end check
```

Each module depends only on the ones above it, so the graph is acyclic. The chat
app speaks in domain terms (`recall`, `remember`, `pending_diary_jobs`) and never
sees the module's `process({...})` protocol — only `memory.py` translates. **Two
frontends** (`tui.py` and `app.py`'s classic loop) share the exact same facade
(`MemoryService`, including the Layer V `drain_diary`) and `commands.dispatch` with
its `build_*` renderables — only the presentation differs, which is the modularity
the demo showcases.

## How memory is wired in

```
your message ──▶ MemoryService.recall(msg)  ──▶  nexus.process({"assemble", ...})
                                                          │  <memory_context>
            system prompt + <memory_context> + history + message ───┘
                                  │
                                  ▼
                  OpenRouter — PRIMARY model (streamed)   ← chatapp/llm.py
                                  │
                                  ▼
        MemoryService.remember(q, a)  ──▶  nexus.process({"ingest", ...})  (async)
                                  │
                                  ▼  (Layer V, optional)
   MemoryService.drain_diary(): pending_summaries() ─▶ run on the AUX model ─▶ submit_summary()
```

## Setup

```powershell
cd <this-demo-folder>          # the directory containing chat.py
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\nexus_memory_pkg
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env      # then edit .env and paste your OpenRouter key
```

Get an API key at <https://openrouter.ai/keys>. Pick any model id from
<https://openrouter.ai/models> via `OPENROUTER_MODEL` in `.env`
(default `openai/gpt-4o-mini`; a free option is `google/gemini-2.0-flash-exp:free`).

**Two models.** `OPENROUTER_MODEL` is the **primary** model — it writes the chat
reply you see streamed. `OPENROUTER_AUX_MODEL` is a **secondary** model that runs
the **Layer V diary** (the session summaries Nexus enqueues into its outbox) — so
you can point that at something cheap/fast while keeping a stronger model for the
conversation. Leave `OPENROUTER_AUX_MODEL` unset to reuse the primary model. The
status footer shows both: `chat <primary> · aux <secondary>`.

The demo deliberately adds **no** summarization of its own — if the diary is off
(`--no-diary`) or the current session has no entry yet, `/diary` shows nothing.
That keeps the demo an honest mirror of what Nexus actually produced (no fabricated
fallback).

## Run

```powershell
# full-screen TUI (default; needs .env with a key). Layer V diary is ON by default
.\.venv\Scripts\python.exe chat.py

# disable the optional Layer V diary
.\.venv\Scripts\python.exe chat.py --no-diary

# the classic line-based frontend (same memory/commands, no TUI)
.\.venv\Scripts\python.exe chat.py --classic

# offline check — exercises all 4 layers + Layer V, no key needed
.\.venv\Scripts\python.exe chat.py --selftest
```

The TUI keeps the conversation **pure chat** — memory, diary and trace info never
appear on their own; you pull them on demand with a slash command. The input box
is a multi-line editor at the bottom, the answer streams above it, and a status
footer shows `chat <model> · aux <model> · facts · diary · pending · ctx <tokens>/<budget> tok`.
**Keys:** `Enter` sends · `Shift+Enter` (or `Ctrl+J`) inserts a newline
(compose/paste multi-line) · type `/` for the command palette, `Tab` completes ·
`Ctrl+Q` quits.

**Token window.** The chat history is **not** capped by message count — only by a
**token budget** (counted with `tiktoken` over the *finished* prompt actually sent
to the provider: system prompt + recalled `<memory_context>` + history + your
input). Each turn includes as many prior turns as fit; the footer shows the live
token size of that input. Default **50 000** tokens (or `NEXUS_TOKEN_WINDOW`);
change it live with **`/tokens 80000`** (or `/tokens 80k`), and `/tokens` alone
reports the current window + last input size.

## Commands

Type `/` to see them inline; `/help` lists them all.

| Command | Layer | Action |
| :-- | :-- | :-- |
| `/recall <query>` | III·IV | show the facts + directives recalled for a query |
| `/memory` | II | list everything the assistant remembers (episodic) |
| `/stats` | — | memory health (fact count, db size) |
| `/diary` | V | the Layer V diary entry for the current session (nothing if absent/off) |
| `/pyramid` | V | the diary pyramid: session diaries + persistent summary + outbox |
| `/transcript [day]` | II | raw reconstructed dialogue transcript |
| `/rules` | IV | list active procedural directives |
| `/rule <text>` | IV | add a standing procedural directive |
| `/distill` | IV | promote recurring preferences into directives |
| `/working` | I | show the volatile working-memory buffer |
| `/forget <text>` | III | delete the fact best matching `<text>` |
| `/tokens [N]` | — | show or set the context **token window** (e.g. `/tokens 80k`) |
| `/trace` | — | show the **last turn's "module internals"** X-ray |
| `/clear` | — | clear the on-screen conversation (long-term memory stays) |
| `/help` · `/quit` | — | help · exit |

## Seeing inside the module

Every turn, the module narrates its internal steps through its own `logging` output —
the real 4-layer write fan-out (working buffer → fact extraction → semantic write/dedup →
episodic log → procedural directive detection) and the retrieval assembly. The TUI captures
them silently; run **`/trace`** to render the last turn's `🔬 module internals` panel on
demand (and `/recall <query>` to see exactly what memory would inject for a query).

## Try this

1. Tell it a few things: *"My name is Sai, I live in Berlin, I'm building Nexus."*
2. Say *"sprich ab jetzt deutsch mit mir"* — watch the trace detect a **procedural directive**.
3. `/quit`, then restart `chat.py`.
4. Ask *"was weißt du über mich?"* → it recalls across the restart **and** keeps answering in German.
5. `/diary` for the narrative session summary; `/rules` to see the standing directive; `/forget Berlin` to delete a fact.

## Notes

- The module is a **copy** under `nexus_memory_pkg/` (installed editable), so this
  demo is self-contained and independent of the original `../nexus-memory`.
- The default embedder is the dependency-free `HashingEmbedder` (lexical/feature
  hashing) — no model downloads, fully offline. Recall is lexical-semantic; for
  stronger paraphrase matching you can swap in `SentenceTransformerEmbedder` or
  `OpenAIEmbedder` when constructing `NexusMemory`.
