"""Slash-command dispatch + the command-output renderables — frontend-agnostic.

A handler takes a small context object and the argument string and returns an
:class:`Outcome`: an optional rich *renderable* to display, an optional one-line
notice, and a quit flag. It never prints — so the same dispatch drives both the
classic console frontend and the Textual TUI. The ``build_*`` functions turn plain
domain data into rich renderables (Panel / Table / Text); both frontends render
the very same objects. ``COMMANDS`` is the single source of truth for the command
list (help text + the TUI's autocomplete).

The context (`ctx`) is duck-typed; both frontends provide:
    ctx.memory             -> MemoryService
    ctx.last_trace         -> list[(label, style, msg)]  (last turn's internals)
    ctx.clear_screen()     -> None
    ctx.token_budget       -> int   (current context token window)
    ctx.last_tokens        -> int   (tokens of the last finished input)
    ctx.set_token_budget(n)-> None
    ctx.language           -> str   (current reply-language code, e.g. "en"/"de")
    ctx.set_language(code) -> None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Single source of truth: (name, description). Drives /help and TUI autocomplete.
COMMANDS: list[tuple[str, str]] = [
    ("/recall", "show recalled facts + directives for a query"),
    ("/memory", "list stored episodic memories"),
    ("/stats", "memory health (count, db size)"),
    ("/diary", "Layer V diary for the current session"),
    ("/pyramid", "Layer V pyramid: session diaries + persistent summary + outbox"),
    ("/transcript", "raw episodic transcript (default: today)"),
    ("/rules", "active procedural directives (Layer IV)"),
    ("/rule", "add a standing directive: /rule <text>"),
    ("/distill", "promote recurring preferences into directives"),
    ("/working", "the volatile working-memory buffer (Layer I)"),
    ("/forget", "delete the fact best matching: /forget <text>"),
    ("/tokens", "show or set the context token window, e.g. /tokens 80000"),
    ("/lang", "show or set the reply language, e.g. /lang de (default en)"),
    ("/trace", "show the last turn's module internals (X-ray)"),
    ("/clear", "clear the on-screen conversation"),
    ("/help", "show this command list"),
    ("/quit", "exit"),
]

COMMAND_NAMES = [name for name, _ in COMMANDS]


@dataclass
class Outcome:
    """Result of handling a line of input."""

    handled: bool
    quit: bool = False
    renderable: RenderableType | None = None
    notice: str | None = None


# --------------------------------------------------------------------------- #
# command-output renderables (UI-framework agnostic; mounted by TUI, printed by classic)
# --------------------------------------------------------------------------- #
def build_recalled(facts: list[dict], directives: list[str]) -> RenderableType:
    """Recalled semantic facts AND active procedural directives."""
    body = Text()
    if not facts and not directives:
        body.append("nothing recalled for this query.", style="dim")
        return Panel(body, title="🧠 memory recall", border_style="cyan", expand=False)
    if directives:
        body.append("active directives (procedural):\n", style="bold yellow")
        for d in directives:
            body.append(f"  ▸ {d}\n", style="yellow")
    if facts:
        if directives:
            body.append("\n")
        body.append("recalled facts (semantic):\n", style="bold cyan")
        for f in facts:
            body.append(f"  • {f.get('content', '')}", style="dim")
            body.append(f"   (score {f.get('score', 0):.2f})\n", style="dim cyan")
    return Panel(body, title="🧠 memory recall", border_style="cyan", expand=False)


def build_trace(rows: list[tuple[str, str, str]], title: str = "last turn") -> RenderableType:
    """The module's internal log events (the X-ray) as ``(label, style, msg)``."""
    body = Text()
    if not rows:
        body.append("no internal events captured.", style="dim")
    for label, style, msg in rows:
        body.append(f"  [{label:>13}] ", style=f"bold {style}")
        body.append(f"{msg}\n", style="white")
    return Panel(body, title=f"🔬 module internals — {title}", border_style="bright_black", expand=False)


def build_memory(rows: list[dict]) -> RenderableType:
    if not rows:
        return Text("Memory is empty.", style="dim")
    table = Table(title="Stored memories (episodic)", expand=True)
    table.add_column("id", justify="right", style="cyan", no_wrap=True)
    table.add_column("timestamp", style="magenta", no_wrap=True)
    table.add_column("role", style="green", no_wrap=True)
    table.add_column("content", style="white")
    for row in rows:
        table.add_row(
            str(row.get("id", "")),
            str(row.get("timestamp", "")),
            str(row.get("role", "")),
            str(row.get("content", "")),
        )
    return table


def build_stats(health: dict) -> RenderableType:
    return Panel(str(health), title="memory health", border_style="green", expand=False)


def build_diary(row: dict | None, label: str) -> RenderableType:
    """Render a Layer V session-diary row (or 'nothing yet' — no fabricated fallback)."""
    if not row or not row.get("summary"):
        return Text(f"No diary entry yet for {label}.", style="dim")
    seq = row.get("seq", "?")
    flags = []
    if row.get("finalized"):
        flags.append("finalized")
    if row.get("folded"):
        flags.append("folded")
    suffix = f"  [{', '.join(flags)}]" if flags else ""
    body = Text()
    body.append(row.get("summary", ""), style="white")
    body.append(
        f"\n\n({row.get('interaction_count', 0)} interaction(s) · session seq {seq}){suffix}",
        style="dim",
    )
    return Panel(body, title=f"📖 diary · session {seq}", border_style="magenta", expand=False)


def build_pyramid(state: dict | None, pending: list) -> RenderableType:
    if state is None:
        return Text(
            "Layer V diary is not enabled. Start without --no-diary to activate it.",
            style="dim",
        )
    sessions = state.get("sessions", []) or []
    summary = state.get("summary") or None

    body = Text()
    body.append("L1 · session diaries (rolling, updated every N=5 interactions)\n", style="bold magenta")
    if sessions:
        for s in sessions:
            flags = []
            if s.get("finalized"):
                flags.append("finalized")
            if s.get("folded"):
                flags.append("folded")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            body.append(
                f"  • seq {s.get('seq', '?')} · {s.get('interaction_count', 0)} interaction(s){flag_str}\n",
                style="magenta",
            )
            body.append(f"      {s.get('summary', '') or '[pending summary]'}\n", style="white")
    else:
        body.append("  [no session diaries yet]\n", style="dim")

    body.append("\nL2 · persistent summary (one growing entry, folds every 6 sessions)\n", style="bold blue")
    if summary and summary.get("summary"):
        first = (summary.get("first_session") or "?")[:8]
        last = (summary.get("last_session") or "?")[:8]
        body.append(
            f"  • {summary.get('session_count', 0)} session(s) folded · {first}…→{last}…\n",
            style="blue",
        )
        body.append(f"      {summary.get('summary', '')}\n", style="white")
    else:
        body.append("  [no persistent summary yet]\n", style="dim")

    body.append(f"\noutbox · {len(pending)} pending summarization job(s)\n", style="bold yellow")
    for j in pending:
        target = getattr(j, "session", None) or "persistent summary"
        body.append(f"  ▸ {getattr(j, 'kind', '?')} job for {target}\n", style="yellow")

    return Panel(body, title="🪜 Layer V · diary pyramid", border_style="magenta", expand=False)


def build_rules(rules: list[dict]) -> RenderableType:
    if not rules:
        return Text("No procedural directives stored yet.", style="dim")
    table = Table(title="Procedural directives (Layer IV)", expand=True)
    table.add_column("id", justify="right", style="cyan", no_wrap=True)
    table.add_column("prio", justify="right", style="yellow", no_wrap=True)
    table.add_column("category", style="green", no_wrap=True)
    table.add_column("source", style="magenta", no_wrap=True)
    table.add_column("directive", style="white")
    for r in rules:
        table.add_row(
            str(r.get("id", "")),
            str(r.get("priority", "")),
            str(r.get("category", "")),
            str(r.get("source", "")),
            str(r.get("directive", "")),
        )
    return table


def build_rule_added(rule: dict, fallback_text: str) -> RenderableType:
    return Panel(
        f"[white]{rule.get('directive', fallback_text)}[/]\n"
        f"[dim]category={rule.get('category')} priority={rule.get('priority')} "
        f"id={rule.get('id')}[/]",
        title="✓ directive stored",
        border_style="yellow",
        expand=False,
    )


def build_working(rows: list[dict]) -> RenderableType:
    if not rows:
        return Text("Working buffer is empty.", style="dim")
    table = Table(title="Working memory (Layer I · volatile RAM)", expand=True)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("timestamp", style="magenta", no_wrap=True)
    table.add_column("role", style="green", no_wrap=True)
    table.add_column("content", style="white")
    for i, turn in enumerate(rows, 1):
        table.add_row(
            str(i),
            str(turn.get("timestamp", "")),
            str(turn.get("role", "")),
            str(turn.get("content", "")),
        )
    return table


def build_transcript(text: str, day: str | None) -> RenderableType:
    title = f"🗒  episodic transcript · {day}" if day else "🗒  episodic transcript (all)"
    return Panel(text or "[no dialogue recorded]", title=title, border_style="magenta", expand=False)


def build_distill(promoted: list[dict]) -> RenderableType:
    if not promoted:
        return Text("Distill: no recurring preferences found to promote.", style="dim")
    body = Text()
    for r in promoted:
        body.append(f"  ▸ {r.get('directive', r)}\n", style="yellow")
    return Panel(
        body,
        title=f"✓ distilled {len(promoted)} preference(s) into procedural memory",
        border_style="yellow",
        expand=False,
    )


def build_help() -> RenderableType:
    table = Table(title="Commands", expand=False, show_header=False, box=None)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(style="white")
    for name, desc in COMMANDS:
        table.add_row(name, desc)
    return table


# --------------------------------------------------------------------------- #
# handlers: (ctx, arg) -> Outcome
# --------------------------------------------------------------------------- #
def _recall(ctx, arg: str) -> Outcome:
    if not arg:
        return Outcome(True, notice="Usage: /recall <query>")
    r = ctx.memory.recall(arg)
    return Outcome(True, renderable=build_recalled(r.facts, r.directives))


def _memory(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_memory(ctx.memory.episodic_rows()))


def _stats(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_stats(ctx.memory.health()))


def _rules(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_rules(ctx.memory.rules()))


def _working(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_working(ctx.memory.working_rows()))


def _distill(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_distill(ctx.memory.distill()))


def _transcript(ctx, arg: str) -> Outcome:
    day = arg or None
    return Outcome(True, renderable=build_transcript(ctx.memory.transcript(day), day))


def _pyramid(ctx, arg: str) -> Outcome:
    return Outcome(
        True,
        renderable=build_pyramid(ctx.memory.diary_state(), ctx.memory.pending_diary_jobs()),
    )


def _diary(ctx, arg: str) -> Outcome:
    if not ctx.memory.diary_enabled:
        return Outcome(True, notice="Layer V diary is off (start without --no-diary) — nothing is summarized.")
    return Outcome(True, renderable=build_diary(ctx.memory.diary_current_session(), "the current session"))


def _rule(ctx, arg: str) -> Outcome:
    if not arg:
        return Outcome(True, notice="Usage: /rule <directive text>")
    rule = ctx.memory.add_rule(arg)
    return Outcome(True, renderable=build_rule_added(rule, arg))


def _forget(ctx, arg: str) -> Outcome:
    if not arg:
        return Outcome(True, notice="Usage: /forget <text>")
    return Outcome(True, notice=f"forget: {ctx.memory.forget(arg)}")


def _tokens(ctx, arg: str) -> Outcome:
    if not arg:
        return Outcome(
            True,
            notice=f"context token window: {ctx.token_budget} · "
            f"last finished input: {ctx.last_tokens} tokens",
        )
    raw = arg.strip().lower().replace("_", "").replace(",", "")
    if raw.endswith("k"):
        raw = raw[:-1] + "000"
    try:
        n = int(raw)
    except ValueError:
        return Outcome(True, notice="Usage: /tokens <number>  (e.g. /tokens 80000 or /tokens 80k)")
    if n < 256:
        return Outcome(True, notice="token window too small (minimum 256).")
    ctx.set_token_budget(n)
    return Outcome(True, notice=f"context token window set to {n}.")


def _lang(ctx, arg: str) -> Outcome:
    from .config import LANGUAGES, resolve_language

    available = ", ".join(f"{c} ({n})" for c, n in LANGUAGES.items())
    if not arg:
        cur = ctx.language
        return Outcome(
            True,
            notice=f"reply language: {cur} ({LANGUAGES.get(cur, '?')}). "
            f"Set with /lang <code>. Available: {available}",
        )
    code = resolve_language(arg)
    if code is None:
        return Outcome(
            True,
            notice=f"Unknown language {arg.strip()!r}. Available: {available}",
        )
    ctx.set_language(code)
    return Outcome(True, notice=f"reply language set to {code} ({LANGUAGES[code]}).")


def _trace(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_trace(list(ctx.last_trace), "last turn"))


def _clear(ctx, arg: str) -> Outcome:
    ctx.clear_screen()
    return Outcome(True, notice="Conversation cleared (long-term memory kept).")


def _help(ctx, arg: str) -> Outcome:
    return Outcome(True, renderable=build_help())


_HANDLERS: dict[str, Callable] = {
    "/recall": _recall,
    "/memory": _memory,
    "/stats": _stats,
    "/rules": _rules,
    "/working": _working,
    "/distill": _distill,
    "/transcript": _transcript,
    "/pyramid": _pyramid,
    "/diary": _diary,
    "/rule": _rule,
    "/forget": _forget,
    "/tokens": _tokens,
    "/lang": _lang,
    "/trace": _trace,
    "/clear": _clear,
    "/help": _help,
}

_QUIT = {"/quit", "/exit"}


def dispatch(ctx, text: str) -> Outcome:
    """Route a line of input. ``handled=False`` means it is a normal chat message."""
    if not text.startswith("/"):
        return Outcome(handled=False)

    cmd, _, arg = text.partition(" ")
    arg = arg.strip()

    if cmd in _QUIT:
        return Outcome(True, quit=True)

    handler = _HANDLERS.get(cmd)
    if handler is None:
        return Outcome(True, notice=f"Unknown command {cmd!r}. Type /help.")
    return handler(ctx, arg)


def completions(prefix: str) -> list[tuple[str, str]]:
    """Commands whose name starts with ``prefix`` (for TUI autocomplete)."""
    p = prefix.strip()
    return [(n, d) for n, d in COMMANDS if n.startswith(p)]
