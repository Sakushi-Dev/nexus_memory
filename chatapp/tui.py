"""Full-screen Textual TUI frontend (the default UI).

Layout (top → bottom): a header, a scrollable chat log, a live "streaming" line
for the in-progress answer, a slash-command autocomplete palette, a multi-line
input box, and a status footer. The conversation is **pure chat** — memory/diary/
trace info never auto-appears; you pull it on demand with a slash command (the
palette pops up automatically as you type ``/``; Tab completes it).

The input is a multi-line editor: **Enter sends**, **Shift+Enter** (or **Ctrl+J**)
inserts a newline, so you can compose or paste multi-line messages.

It is just another frontend over the same components: ``MemoryService`` (the Nexus
boundary, incl. the Layer V ``drain_diary``), ``commands.dispatch`` and its
``build_*`` renderables — identical to the classic console loop, only the
presentation differs. The
blocking LLM stream + memory writes run in a worker thread so the UI never
freezes; ``call_from_thread`` marshals updates back to the screen.
"""

from __future__ import annotations

from rich.rule import Rule
from rich.text import Text

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import Header, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from . import commands, trace
from .app import build_messages, _now_floor
from .config import Settings
from .llm import OpenRouterLLM
from .memory import MemoryService
from .tokens import TokenCounter


class ChatInput(TextArea):
    """A multi-line input: Enter submits, Shift+Enter / Ctrl+J inserts a newline."""

    class Submitted(Message):
        """Posted when the user presses Enter to send the composed text."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def _command_prefix(self) -> bool:
        t = self.text
        return t.startswith("/") and " " not in t and "\n" not in t

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
        elif event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
        elif event.key == "tab" and self._command_prefix():
            event.prevent_default()
            event.stop()
            self.app.complete_command()
        elif event.key == "escape":
            event.prevent_default()
            event.stop()
            self.app.hide_palette()
        else:
            await super()._on_key(event)


class NexusTUI(App):
    """The Nexus Chat terminal UI."""

    CSS = """
    Screen { background: $surface; }
    #chat { height: 1fr; padding: 0 1; }
    #streaming { height: auto; padding: 0 1; color: $text-muted; }
    #palette {
        display: none; height: auto; max-height: 8;
        border: round $accent; background: $panel;
    }
    #prompt { height: auto; max-height: 10; border: round $primary; }
    #status { height: 1; color: $text-muted; background: $panel; padding: 0 1; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        settings: Settings,
        llm: OpenRouterLLM,
        memory: MemoryService,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.llm = llm
        self.memory = memory
        # Nexus owns the durable history; only a session "floor" is kept locally
        # so /clear can hide earlier turns without deleting long-term memory.
        self.history_floor: str | None = None
        self.last_trace: list[tuple[str, str, str]] = []
        self._palette_names: list[str] = []
        self.counter = TokenCounter(settings.model)
        self.token_budget = settings.token_window
        self.last_tokens = 0
        # Reply language is mutable at runtime (/lang); seeded from settings.
        self.language = settings.language

    # ------------------------------------------------------------------ #
    # composition / lifecycle
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="chat", wrap=True, markup=False, highlight=False)
        yield Static("", id="streaming")
        yield OptionList(id="palette")
        yield ChatInput(id="prompt", show_line_numbers=False, soft_wrap=True)
        yield Static("", id="status")

    def on_mount(self) -> None:
        self.title = "Nexus Chat"
        self.sub_title = f"chat {self.settings.model} · aux {self.settings.aux_model}"
        trace.enable(self.settings.trace_on)  # --notrace starts with the X-ray off
        self.chat = self.query_one("#chat", RichLog)
        self.chat.write(
            Text(
                "Pure chat with persistent memory. Enter sends · Shift+Enter (or "
                "Ctrl+J) for a newline · type / for commands (Tab completes) · "
                "/help for the list.",
                style="dim",
            )
        )
        self._refresh_status()
        self.query_one("#prompt", ChatInput).focus()

    # ------------------------------------------------------------------ #
    # ctx interface used by commands.dispatch
    # ------------------------------------------------------------------ #
    def clear_screen(self) -> None:
        self.chat.clear()
        self.history_floor = _now_floor()  # hide earlier turns; memory persists

    def set_token_budget(self, n: int) -> None:
        self.token_budget = n
        self._refresh_status()

    def set_language(self, code: str) -> None:
        self.language = code
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # input → autocomplete palette
    # ------------------------------------------------------------------ #
    @on(TextArea.Changed, "#prompt")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        self._update_palette(event.text_area.text)

    def _update_palette(self, value: str) -> None:
        palette = self.query_one("#palette", OptionList)
        single = value.startswith("/") and " " not in value and "\n" not in value
        if single:
            matches = commands.completions(value)
            palette.clear_options()
            self._palette_names = [name for name, _ in matches]
            if matches:
                palette.add_options([Option(f"{name:<12} {desc}") for name, desc in matches])
                palette.display = True
                palette.highlighted = 0
                return
        palette.display = False
        self._palette_names = []

    def complete_command(self) -> None:
        """Tab-complete the input to the top command suggestion."""
        if not self._palette_names:
            return
        inp = self.query_one("#prompt", ChatInput)
        inp.text = self._palette_names[0] + " "
        inp.move_cursor(inp.document.end)
        self.hide_palette()

    def hide_palette(self) -> None:
        self.query_one("#palette", OptionList).display = False

    # ------------------------------------------------------------------ #
    # submit → command or chat turn
    # ------------------------------------------------------------------ #
    @on(ChatInput.Submitted)
    def _on_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        # On a bare command prefix, Enter accepts the top suggestion.
        if (
            text.startswith("/")
            and " " not in text
            and "\n" not in text
            and text not in commands.COMMAND_NAMES
        ):
            matches = commands.completions(text)
            if matches:
                text = matches[0][0]
        self.hide_palette()
        self.query_one("#prompt", ChatInput).text = ""
        if text:
            self._handle(text)

    def _handle(self, text: str) -> None:
        out = commands.dispatch(self, text)
        if out.handled:
            if out.quit:
                self.exit()
                return
            if out.renderable is not None:
                self.chat.write(out.renderable)
            if out.notice:
                self.chat.write(Text(out.notice, style="dim"))
            return
        # normal chat turn
        self.chat.write(self._msg("user", text))
        self.chat.write(Rule(style="grey37"))
        self.query_one("#prompt", ChatInput).disabled = True
        self._run_turn(text)

    # ------------------------------------------------------------------ #
    # the turn (worker thread: recall → stream → remember → diary)
    # ------------------------------------------------------------------ #
    @work(thread=True, exclusive=True, group="turn")
    def _run_turn(self, user_text: str) -> None:
        trace.handler().drain()  # isolate this turn's internals
        recall = self.memory.recall(user_text)
        messages, self.last_tokens = build_messages(
            self.memory, recall, user_text,
            self.counter, self.token_budget, self.history_floor, self.language,
        )
        self.call_from_thread(self._refresh_status)  # show the finished input's token size

        buf: list[str] = []

        def on_delta(token: str) -> None:
            buf.append(token)
            self.call_from_thread(self._set_streaming, "".join(buf))

        try:
            answer = self.llm.stream(messages, on_delta=on_delta)
        except Exception as exc:  # noqa: BLE001 - surface API errors, keep the app alive
            self.call_from_thread(self._finish_error, str(exc))
            return

        self.call_from_thread(self._finish_answer, answer)

        # durable writes happen off the UI thread — Nexus stores the turn, so the
        # next turn's history() includes it (no app-side history kept).
        self.memory.remember(user_text, answer)
        self.memory.flush()
        self.last_trace = trace.handler().drain()
        if self.memory.diary_enabled:
            self.memory.drain_diary()  # runs on the aux model
        self.call_from_thread(self._refresh_status)

    # ------------------------------------------------------------------ #
    # UI-thread update helpers (always via call_from_thread)
    # ------------------------------------------------------------------ #
    def _set_streaming(self, text: str) -> None:
        streaming = self.query_one("#streaming", Static)
        streaming.display = True
        streaming.update(self._msg("nexus", text))

    def _finish_answer(self, answer: str) -> None:
        self.chat.write(self._msg("nexus", answer))
        self.chat.write(Rule(style="grey37"))
        streaming = self.query_one("#streaming", Static)
        streaming.update("")
        streaming.display = False
        inp = self.query_one("#prompt", ChatInput)
        inp.disabled = False
        inp.focus()

    def _finish_error(self, message: str) -> None:
        streaming = self.query_one("#streaming", Static)
        streaming.update("")
        streaming.display = False
        self.chat.write(Text(f"LLM error: {message}", style="bold red"))
        inp = self.query_one("#prompt", ChatInput)
        inp.disabled = False
        inp.focus()

    def _refresh_status(self) -> None:
        health = self.memory.health()
        facts = health.get("count", "?")
        diary = "on" if self.memory.diary_enabled else "off"
        pending = len(self.memory.pending_diary_jobs()) if self.memory.diary_enabled else 0
        approx = "" if self.counter.exact else "~"
        self.query_one("#status", Static).update(
            f" chat {self.settings.model} · aux {self.settings.aux_model} · "
            f"{facts} facts · diary {diary} · {pending} pending · "
            f"lang {self.language} · "
            f"ctx {approx}{self.last_tokens}/{self.token_budget} tok · /help"
        )

    @staticmethod
    def _msg(role: str, text: str) -> Text:
        t = Text()
        if role == "user":
            t.append("user  ", style="bold blue")
        else:
            t.append("nexus ", style="bold green")
        t.append(text)
        return t


def run_tui(settings: Settings) -> int:
    """Build the live application and run the Textual TUI."""
    try:
        llm = OpenRouterLLM(settings)
    except RuntimeError as exc:
        print(f"Config error: {exc}")
        return 2
    aux = OpenRouterLLM(settings, model=settings.aux_model)  # secondary: side tasks
    memory = MemoryService.open(
        settings.db_path, aux_llm=aux, diary=settings.diary_on,
        embedder_backend=settings.embedder_backend,
    )
    try:
        NexusTUI(settings, llm, memory).run()
    finally:
        memory.close()
    return 0
