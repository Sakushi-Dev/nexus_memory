"""Configuration for the shared auxiliary-job bus (``AuxConfig``).

The aux subsystem owns its own config dataclass (mirroring
:class:`~nexus_memory.layers.diary.config.DiaryConfig`). It is intentionally NOT
folded into :class:`~nexus_memory.core.config.NexusConfig`, to avoid config
sprawl. The host opts in (or out) explicitly via
``NexusMemory(aux=AuxConfig(...))`` / ``aux=True`` / ``aux=False``.

At 0.6.0 the bus is ALWAYS-ON by default: ``enabled=True`` constructs the
:class:`~nexus_memory.core.auxbus.bus.AuxBus` regardless of the diary, and
``procedural_extraction=True`` routes procedural directive mining through the aux
LLM (via the ``procedural_extract`` job kind) instead of the inline regex. A host
that wants the legacy "no bus / no summarization_jobs, inline-regex procedural"
behavior sets ``aux=False`` (and leaves the diary off).

The module is fully offline and deterministic; it never imports or calls any
network/LLM SDK.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuxConfig:
    """Settings for the shared auxiliary-job bus.

    Attributes:
        enabled: Master switch. When ``True`` (default) the AuxBus + its default
            handlers are constructed regardless of the diary, so a plain host has
            a bus. When ``False`` no aux handlers exist and procedural falls back
            to the inline regex detector (byte-identical to 0.4.2).
        procedural_extraction: When ``True`` (default) procedural directive mining
            rides the aux LLM via the ``procedural_extract`` job kind; the inline
            regex is demoted to the offline fallback + the pre-first-drain bridge.
            When ``False`` procedural always uses the inline regex.
    """

    enabled: bool = True                  # master switch: seam/handlers always present
    procedural_extraction: bool = True    # procedural rides aux by default (diary-decoupled)

    def __post_init__(self) -> None:
        """Validate the two booleans (mirrors the ``raise`` style of DiaryConfig)."""
        for name in ("enabled", "procedural_extraction"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"AuxConfig.{name} must be a bool, got {type(value).__name__}"
                )
