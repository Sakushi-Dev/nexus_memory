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


def build_system_prompt(directives: list[str]) -> str:
    """Compose the system prompt, injecting active procedural directives (Layer IV)."""
    if not directives:
        return SYSTEM_PROMPT
    rules = "\n".join(f"- {d}" for d in directives)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "Standing behavioral directives (always follow these):\n"
        f"{rules}"
    )


def build_messages(
    system_directives: list[str],
    context_xml: str,
    history: list[dict],
    user_text: str,
    counter: "TokenCounter",
    budget: int,
) -> tuple[list[dict], int]:
    """Assemble the message list for one turn, bounded by a TOKEN window.

    The history is **not** capped by message count — only by the token budget:
    the fixed parts (system prompt + memory context + the current user message)
    are always included, then prior turns are added newest-first until adding the
    next one would exceed ``budget``. Returns ``(messages, total_tokens)`` where
    ``total_tokens`` is the count of the *finished* input sent to the provider.
    """
    head = [
        {"role": "system", "content": build_system_prompt(system_directives)},
        {"role": "system", "content": f"Long-term memory:\n{context_xml}"},
    ]
    tail = [{"role": "user", "content": user_text}]

    used = counter.count_messages(head + tail)
    chosen: list[dict] = []
    for msg in reversed(history):  # newest-first
        cost = counter.count_message(msg)
        if used + cost > budget:
            break
        chosen.append(msg)
        used += cost
    chosen.reverse()

    messages = head + chosen + tail
    return messages, counter.count_messages(messages)


class ChatApp:
    """The classic line-based session (also the `ctx` for commands.dispatch)."""

    def __init__(self, settings: Settings, llm: OpenRouterLLM, memory: MemoryService) -> None:
        self.settings = settings
        self.llm = llm
        self.memory = memory
        self.history: list[dict] = []
        self.last_trace: list[tuple[str, str, str]] = []
        self.counter = TokenCounter(settings.model)
        self.token_budget = settings.token_window
        self.last_tokens = 0

    # --- ctx interface used by commands.dispatch ------------------------- #
    def clear_screen(self) -> None:
        self.history.clear()

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
            recall.directives, recall.context_xml, self.history, user_text,
            self.counter, self.token_budget,
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

        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": answer})
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
