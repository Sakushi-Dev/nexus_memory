"""Composition root + the classic (line-based) frontend.

``main()`` wires the components and picks a frontend:
    --selftest  -> offline self-test (no network)
    --classic   -> the simple line-based console loop in this file
    (default)   -> the full-screen Textual TUI (chatapp/tui.py)

The classic loop is kept because it is the clearest demonstration of the swappable
UI: it and the TUI share the exact same MemoryService, commands, and renderables
— only the presentation differs.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel

from . import commands, trace
from .config import SYSTEM_PROMPT, Settings, load_settings
from .llm import OpenRouterLLM
from .memory import MemoryService
from .tokens import TokenCounter

# Console helpers for the classic line frontend (the TUI does its own rendering).
console = Console()


def _info(message: str) -> None:
    console.print(message)


def _error(message: str) -> None:
    console.print(f"[bold red]{message}[/]")


def _prompt_user() -> str:
    return console.input("[bold blue]you[/] ").strip()


def _banner(model: str, aux_model: str, db_path: str, count, diary_on: bool) -> None:
    console.print(
        Panel.fit(
            f"[bold]Nexus Chat[/] (classic) · chat [cyan]{model}[/] · "
            f"aux [cyan]{aux_model}[/] · [green]{count}[/] facts\n"
            f"memory at [dim]{db_path}[/]\n"
            "pure chat — type [cyan]/help[/] for commands"
            + (" · [magenta]Layer V diary on[/]" if diary_on else ""),
            border_style="bright_blue",
        )
    )


def _now_floor() -> str:
    """UTC ``YYYY-MM-DD HH:MM:SS`` — matches Nexus turn timestamps (for /clear)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def build_system_prompt(directives: list[str], facts: list[dict]) -> str:
    """Compose the system prompt from the base prompt + Nexus directives + facts.

    Directives (Layer IV — behavior) and recalled facts (Layer III — knowledge)
    are what the model should *obey/know*, so they belong in the system prompt.
    The conversation itself rides as native chat messages (see build_messages),
    so we deliberately do NOT inject ``<recent_dialogue>`` here — that would
    duplicate the message history.
    """
    parts = [SYSTEM_PROMPT]
    if directives:
        rules = "\n".join(f"- {d}" for d in directives)
        parts.append("Standing behavioral directives (always follow these):\n" + rules)
    if facts:
        known = "\n".join(f"- {f.get('content', '')}" for f in facts)
        parts.append("Known facts about the user:\n" + known)
    return "\n\n".join(parts)


def build_messages(
    memory: "MemoryService",
    recall: "Recall",
    user_text: str,
    counter: "TokenCounter",
    budget: int,
    floor: str | None = None,
) -> tuple[list[dict], int]:
    """Assemble the message list for one turn, bounded by a TOKEN window.

    Nexus owns the conversation: ``memory.history()`` returns the durable turns,
    **token-trimmed by Nexus using the host's real tokenizer** (``counter``). The
    directives + recalled facts go into the system prompt; the trimmed turns ride
    as native chat messages (no ``<recent_dialogue>`` duplication). ``budget`` is
    the whole-prompt token window; the fixed parts (system + current user) are
    reserved, the remainder funds the history. ``floor`` (set by /clear) hides
    turns at/older than that timestamp. Returns ``(messages, total_tokens)``.
    """
    system_content = build_system_prompt(recall.directives, recall.facts)
    reserve = counter.count_messages(
        [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_text},
        ]
    )
    hist_budget = max(0, budget - reserve)

    # Nexus trims the history to the token budget (real tokenizer passed in).
    turns = memory.history(max_tokens=hist_budget, token_counter=counter.count_text)
    if floor:
        turns = [t for t in turns if t.get("timestamp", "") > floor]
    history = [{"role": t["role"], "content": t["content"]} for t in turns]

    messages = [
        {"role": "system", "content": system_content},
        *history,
        {"role": "user", "content": user_text},
    ]
    return messages, counter.count_messages(messages)


class ChatApp:
    """The classic line-based session (also the `ctx` for commands.dispatch)."""

    def __init__(self, settings: Settings, llm: OpenRouterLLM, memory: MemoryService) -> None:
        self.settings = settings
        self.llm = llm
        self.memory = memory
        # Nexus owns the conversation history (durable). The host keeps only an
        # optional session "floor" timestamp so /clear can hide earlier turns
        # from the prompt without deleting long-term memory.
        self.history_floor: str | None = None
        self.last_trace: list[tuple[str, str, str]] = []
        self.counter = TokenCounter(settings.model)
        self.token_budget = settings.token_window
        self.last_tokens = 0

    # --- ctx interface used by commands.dispatch ------------------------- #
    def clear_screen(self) -> None:
        # Nexus owns the durable history; "clear" just sets a session floor so
        # earlier turns are excluded from the prompt (long-term memory is kept).
        self.history_floor = _now_floor()

    def set_token_budget(self, n: int) -> None:
        self.token_budget = n

    # --- loop ------------------------------------------------------------ #
    def run(self) -> int:
        trace.enable(True)  # capture internals so /trace can show them on demand
        _banner(
            self.settings.model,
            self.settings.aux_model,
            self.settings.db_path,
            self.memory.health().get("count", "?"),
            self.memory.diary_enabled,
        )
        try:
            while True:
                try:
                    text = _prompt_user()
                except (EOFError, KeyboardInterrupt):
                    _info("\n[dim]bye![/]")
                    break
                if not text:
                    continue

                out = commands.dispatch(self, text)
                if out.handled:
                    if out.quit:
                        break
                    if out.renderable is not None:
                        console.print(out.renderable)
                    if out.notice:
                        _info(f"[dim]{out.notice}[/]")
                    continue

                self._turn(text)
        finally:
            self.memory.close()
        return 0

    def _turn(self, user_text: str) -> None:
        """Pure chat: recall (silent) → stream answer → remember → diary (silent)."""
        trace.handler().drain()  # isolate this turn
        recall = self.memory.recall(user_text)
        messages, self.last_tokens = build_messages(
            self.memory, recall, user_text,
            self.counter, self.token_budget, self.history_floor,
        )
        _info(f"[dim]· {self.last_tokens}/{self.token_budget} tokens in context[/]")

        console.print("[bold green]assistant[/] ", end="")
        try:
            answer = self.llm.stream(
                messages, on_delta=lambda t: console.print(t, end="", style="white")
            )
        except Exception as exc:  # noqa: BLE001
            console.print()
            _error(f"LLM error: {exc}")
            return
        console.print()

        # Persist the exchange — Nexus stores it, so next turn's history() has it.
        self.memory.remember(user_text, answer)
        self.memory.flush()
        self.last_trace = trace.handler().drain()

        if self.memory.diary_enabled:
            self.memory.drain_diary()  # runs on the aux model; view it with /pyramid


# --------------------------------------------------------------------------- #
# composition root
# --------------------------------------------------------------------------- #
def build_live_app(settings: Settings) -> ChatApp:
    """Wire the live classic application (raises on a missing API key)."""
    llm = OpenRouterLLM(settings)                            # primary: the chat response
    aux = OpenRouterLLM(settings, model=settings.aux_model)  # secondary: side tasks
    memory = MemoryService.open(settings.db_path, aux_llm=aux, diary=settings.diary_on)
    return ChatApp(settings, llm, memory)


def main() -> int:
    if "--selftest" in sys.argv:
        from .selftest import run_selftest

        return run_selftest()

    settings = load_settings()

    if "--classic" in sys.argv:
        try:
            app = build_live_app(settings)
        except RuntimeError as exc:
            _error(f"Config error: {exc}")
            return 2
        return app.run()

    # default: the full-screen Textual TUI
    from .tui import run_tui

    return run_tui(settings)
