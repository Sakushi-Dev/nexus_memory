"""Offline self-test — exercises every layer + Layer V with NO network.

It drives the same components as the live app (``MemoryService`` incl. its Layer V
``drain_diary``, the ``commands.build_*`` renderables, ``trace``) but with the
offline deterministic summarizer and a local stand-in "model" for the diary drain,
so it needs no API key. It never imports ``nexus_memory`` — only the facade.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from . import commands, trace
from .memory import MemoryService

console = Console()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _show_trace(title: str) -> None:
    console.print(commands.build_trace(trace.handler().drain(), title))


def _offline_job(prompt: str, rendered_input: str) -> str:
    """A deterministic stand-in for the host model (keeps the self-test offline)."""
    said = [ln for ln in rendered_input.splitlines() if ln.startswith("user:")]
    return "Diary (offline): " + " ".join(said) if said else "Diary (offline)."


def run_selftest() -> int:
    console.rule("[bold]Nexus Chat — offline self-test (4-layer memory + Layer V)")

    tmp = Path(tempfile.mkdtemp()) / "selftest.db"
    # Offline: no summarizer LLM (deterministic MockSummarizer), Layer V ON so the
    # diary outbox is exercised end-to-end via a local drain.
    memory = MemoryService.open(str(tmp), aux_llm=None, diary=True)
    trace.enable(True)
    try:
        turns = [
            ("My name is Sai and I live in Berlin.", "Nice to meet you, Sai!"),
            ("I'm building a local memory library called Nexus.", "Sounds great."),
            ("I prefer Python and I use Windows 11.", "Noted."),
            ("Sprich ab jetzt deutsch mit mir.", "Alles klar, ich antworte ab jetzt auf Deutsch."),
            ("Ich arbeite am liebsten abends.", "Gut zu wissen — abends also."),
        ]
        for q, a in turns:
            memory.remember(q, a)
        memory.flush()
        _show_trace("ingest fan-out across all 4 layers (5 interactions)")

        # --- Layer III (semantic): recall against a related query ------------
        console.print("\n[bold]Query:[/] 'Where does the user live and what are they building?'")
        recall = memory.recall("Where does the user live and what are they building?")
        _show_trace("retrieval (assemble)")
        console.print(commands.build_recalled(recall.facts, recall.directives))
        console.print(
            Panel(recall.context_xml or "[empty]", title="context_xml injected into the prompt", border_style="cyan")
        )

        # --- Layer IV (procedural): the German directive must be recalled -----
        console.print("\n[bold]Procedural directives recalled:[/]")
        console.print(commands.build_rules(memory.rules()))
        assert any("german" in d.lower() for d in recall.directives), (
            f"expected a 'Respond in German.' directive, got {recall.directives!r}"
        )

        # --- Layer I (working): volatile buffer holds the recent turns --------
        console.print("\n[bold]Working buffer (volatile):[/]")
        console.print(commands.build_working(memory.working_rows()))

        # --- Layer V (diary outbox): drain, then show the day narrative + pyramid ---
        folded = memory.drain_diary(_offline_job)
        console.print(f"\n[bold]Layer V diary — drained {folded} outbox job(s):[/]")
        day = memory.diary_day(_today())
        console.print(commands.build_diary(day, _today()))
        console.print(commands.build_pyramid(memory.diary_state(), memory.pending_diary_jobs()))
        assert day and day.get("summary"), "expected a Layer V daily diary after draining"

        # --- Layer II (episodic): raw transcript reconstruction --------------
        console.print("\n[bold]Episodic transcript (reconstructed):[/]")
        console.print(commands.build_transcript(memory.transcript(_today()), _today()))

        # --- Distillation: promote recurring preferences -> procedural -------
        console.print("\n[bold]Distillation (semantic preferences -> procedural):[/]")
        console.print(commands.build_distill(memory.distill()))

        console.print("\n[bold]Full memory:[/]")
        console.print(commands.build_memory(memory.episodic_rows()))
        console.print(commands.build_stats(memory.health()))

        assert recall.facts, "expected to recall at least one fact"
        console.print(
            "\n[bold green]✓ self-test passed[/] — all 4 layers "
            "(working · episodic · semantic · procedural) plus the optional "
            "Layer V diary outbox work end-to-end."
        )
        return 0
    finally:
        memory.close()
